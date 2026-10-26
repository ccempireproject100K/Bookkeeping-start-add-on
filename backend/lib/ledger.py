"""Double-entry ledger core: posting, period locking, numbering, and report aggregations.

Money passed in `lines` is already integer cents. Invariants I1-I10 enforced here.
"""
from fastapi import HTTPException

from lib.base import Scoped, new_id, now_iso, today_str, period_key_of, fiscal_year_of
from lib.coa import ACCOUNT_TYPE_OF, NORMAL_BALANCE_OF, NAME_OF, TYPE_ORDER
from lib.currencies import normalize as normalize_currency
from lib.currencies import DEFAULT_CURRENCY, decimals_for
from lib import fx as FX
from lib.audit import append_audit


def _scoped(db, tenant_id):
    return {
        "accounts": Scoped(db.accounts, tenant_id),
        "entries": Scoped(db.journal_entries, tenant_id),
        "lines": Scoped(db.journal_lines, tenant_id),
        "periods": Scoped(db.fiscal_periods, tenant_id),
        "audit": Scoped(db.audit_log, tenant_id),
        "counters": db.counters,
    }


async def audit(db, tenant_id, user, action, detail):
    await append_audit(db, {
        "id": new_id(), "tenant_id": tenant_id,
        "actor": (user or {}).get("email", "system"),
        "action": action,
        "detail": detail,
        "at": now_iso(),
    })


async def ensure_period(db, tenant_id, entry_date):
    s = _scoped(db, tenant_id)
    key = period_key_of(entry_date)
    period = await s["periods"].find_one({"key": key})
    if not period:
        y, m = key.split("-")
        # last day of month without calendar import complexity
        import calendar
        last = calendar.monthrange(int(y), int(m))[1]
        period = {
            "id": new_id(), "key": key,
            "start_date": f"{key}-01", "end_date": f"{key}-{last:02d}",
            "status": "open", "locked_by": None, "locked_at": None, "year_closed": False,
            "created_at": now_iso(),
        }
        await s["periods"].insert_one(period)
    return period


def _validate_lines(lines):
    if not lines:
        raise HTTPException(status_code=422, detail="A journal entry needs at least two lines")
    total_debit = 0
    total_credit = 0
    for ln in lines:
        d = int(ln.get("debit", 0) or 0)
        c = int(ln.get("credit", 0) or 0)
        if d < 0 or c < 0:
            raise HTTPException(status_code=422, detail="Line amounts cannot be negative")
        if (d > 0 and c > 0) or (d == 0 and c == 0):
            raise HTTPException(status_code=422, detail="Each line must be debit-only or credit-only (I2)")
        if ln["account_code"] not in ACCOUNT_TYPE_OF:
            raise HTTPException(status_code=422, detail=f"Unknown account {ln['account_code']}")
        total_debit += d
        total_credit += c
    if total_debit != total_credit:
        raise HTTPException(
            status_code=422,
            detail=f"Entry is not balanced (I1): debits {total_debit} != credits {total_credit}",
        )
    return total_debit


async def _assert_accounts_active(s, lines):
    for ln in lines:
        acc = await s["accounts"].find_one({"code": ln["account_code"]})
        if not acc:
            raise HTTPException(status_code=422, detail=f"Account {ln['account_code']} not in chart (I3)")
        if acc.get("archived"):
            raise HTTPException(status_code=422, detail=f"Account {ln['account_code']} is archived (I3)")


async def _assign_number(s, entry_date):
    fy = fiscal_year_of(entry_date)
    seq = await s["entries"].next_seq(s["counters"], f"je:{fy}")
    return f"JE-{fy}-{seq:06d}"


async def _write_lines(s, entry, lines, status):
    docs = []
    for ln in lines:
        docs.append({
            "id": new_id(),
            "entry_id": entry["id"],
            "entry_number": entry["number"],
            "entry_date": entry["entry_date"],
            "period_key": entry["period_key"],
            "account_code": ln["account_code"],
            "account_type": ACCOUNT_TYPE_OF[ln["account_code"]],
            "debit": int(ln.get("debit", 0) or 0),
            "credit": int(ln.get("credit", 0) or 0),
            "memo": ln.get("memo"),
            "status": status,
            "created_at": now_iso(),
        })
    await s["lines"].insert_many(docs)


async def post_journal_entry(db, tenant_id, user, draft, status="posted", idempotency_key=None):
    """Create a journal entry. status 'posted' -> hits the GL immediately; 'draft' -> does not (I8)."""
    s = _scoped(db, tenant_id)
    lines = draft["lines"]
    total = _validate_lines(lines)
    await _assert_accounts_active(s, lines)

    try:
        currency = normalize_currency(draft.get("currency"))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"Unsupported currency: {draft.get('currency')!r}")

    entry_date = draft.get("entry_date") or today_str()
    if idempotency_key:
        dup = await s["entries"].find_one({"idempotency_key": idempotency_key})
        if dup:
            return dup

    period = await ensure_period(db, tenant_id, entry_date)
    if status == "posted" and period["status"] == "locked":
        raise HTTPException(status_code=423, detail=f"Period {period['key']} is locked")

    number = await _assign_number(s, entry_date) if status == "posted" else None
    entry = {
        "id": new_id(),
        "number": number,
        "entry_date": entry_date,
        "status": status,
        "source": draft.get("source", "manual"),
        "source_ref": draft.get("source_ref"),
        "memo": draft.get("memo"),
        "created_by": (user or {}).get("email", "system"),
        "posted_by": (user or {}).get("email") if status == "posted" else None,
        "posted_at": now_iso() if status == "posted" else None,
        "reverses_entry_id": draft.get("reverses_entry_id"),
        "reversed_by_entry_id": None,
        "period_key": period["key"],
        "idempotency_key": idempotency_key,
        "total_cents": total,
        "currency": currency,
        "created_at": now_iso(),
    }
    # Structured audit detail (e.g. forex postings) rides along when provided;
    # entries posted without it are byte-identical to before.
    if draft.get("details") is not None:
        entry["details"] = draft["details"]
    await s["entries"].insert_one(entry)
    await _write_lines(s, entry, lines, status)
    await audit(db, tenant_id, user, f"journal.{status}", {"number": number, "source": entry["source"], "total_cents": total})
    return entry


async def post_draft(db, tenant_id, user, entry_id):
    s = _scoped(db, tenant_id)
    entry = await s["entries"].find_one({"id": entry_id})
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    if entry["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"Entry is already {entry['status']}")
    lines = await s["lines"].find_list({"entry_id": entry_id})
    _validate_lines(lines)
    await _assert_accounts_active(s, lines)
    period = await ensure_period(db, tenant_id, entry["entry_date"])
    if period["status"] == "locked":
        raise HTTPException(status_code=423, detail=f"Period {period['key']} is locked")
    number = await _assign_number(s, entry["entry_date"])
    await s["entries"].update_one({"id": entry_id}, {"$set": {
        "status": "posted", "number": number,
        "posted_by": user.get("email"), "posted_at": now_iso(),
    }})
    await s["lines"].c.update_many(
        {"tenant_id": tenant_id, "entry_id": entry_id},
        {"$set": {"status": "posted", "entry_number": number}},
    )
    await audit(db, tenant_id, user, "journal.posted", {"number": number})
    entry["status"] = "posted"
    entry["number"] = number
    return entry


async def reverse_entry(db, tenant_id, user, entry_id):
    s = _scoped(db, tenant_id)
    orig = await s["entries"].find_one({"id": entry_id})
    if not orig:
        raise HTTPException(status_code=404, detail="Entry not found")
    if orig["status"] != "posted":
        raise HTTPException(status_code=409, detail="Only posted entries can be reversed")
    if orig.get("reversed_by_entry_id"):
        raise HTTPException(status_code=409, detail="Entry already reversed")
    lines = await s["lines"].find_list({"entry_id": entry_id})
    swapped = [{
        "account_code": ln["account_code"],
        "debit": ln["credit"],
        "credit": ln["debit"],
        "memo": f"Reversal of {orig['number']}",
    } for ln in lines]
    rev = await post_journal_entry(db, tenant_id, user, {
        "entry_date": today_str(),
        "source": "reversal",
        "source_ref": orig["id"],
        "memo": f"Reversal of {orig['number']}: {orig.get('memo') or ''}".strip(),
        "reverses_entry_id": orig["id"],
        "lines": swapped,
    }, status="posted")
    await s["entries"].update_one({"id": entry_id}, {"$set": {
        "status": "reversed", "reversed_by_entry_id": rev["id"],
    }})
    await audit(db, tenant_id, user, "journal.reversed", {"number": orig["number"], "reversal": rev["number"]})
    return rev


# ---------------- Report aggregations ----------------
async def _balances(db, tenant_id, as_of=None, date_from=None):
    """Return {code: {debit, credit}} summed over posted lines within optional date window."""
    s = _scoped(db, tenant_id)
    match = {"status": "posted"}
    if as_of:
        match.setdefault("entry_date", {})["$lte"] = as_of
    if date_from:
        match.setdefault("entry_date", {})["$gte"] = date_from
    rows = await s["lines"].aggregate([
        {"$match": match},
        {"$group": {"_id": "$account_code", "debit": {"$sum": "$debit"}, "credit": {"$sum": "$credit"}}},
    ])
    return {r["_id"]: {"debit": r["debit"], "credit": r["credit"]} for r in rows}


def _net_balance(code, debit, credit):
    """Signed balance in the account's normal direction."""
    if NORMAL_BALANCE_OF.get(code, "debit") == "debit":
        return debit - credit
    return credit - debit


# ---------------- Multi-currency report translation (Phase 4) ----------------
def _entry_match(as_of=None, date_from=None):
    match = {"status": "posted"}
    if as_of:
        match.setdefault("entry_date", {})["$lte"] = as_of
    if date_from:
        match.setdefault("entry_date", {})["$gte"] = date_from
    return match


async def _needs_translation(db, tenant_id, target_currency, as_of=None, date_from=None):
    """True when any posted line in range sits on an entry whose currency
    differs from the target. Lets USD-only books keep the original code path."""
    s = _scoped(db, tenant_id)
    match = _entry_match(as_of, date_from)
    q = {"tenant_id": tenant_id, **match}
    async for doc in s["entries"].c.find(q, {"currency": 1}):
        if (doc.get("currency") or DEFAULT_CURRENCY) != target_currency:
            return True
    return False


async def _translated_balances(db, tenant_id, target_currency, as_of=None, date_from=None):
    """Like _balances, but every line is converted to ``target_currency`` at
    its own entry-date rate via the FX service.

    Stored line amounts are major-units x 100 (the Phase 1 convention), so we
    convert major -> major at the entry date, round to the target's decimals,
    and return x100 integers — the same convention the reports already use.
    """
    s = _scoped(db, tenant_id)
    match = _entry_match(as_of, date_from)
    rows = await s["lines"].aggregate([
        {"$match": match},
        {"$lookup": {
            "from": "journal_entries",
            "let": {"eid": "$entry_id", "tid": "$tenant_id"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$id", "$$eid"]},
                    {"$eq": ["$tenant_id", "$$tid"]},
                ]}}},
                {"$project": {"_id": 0, "currency": 1, "entry_date": 1}},
            ],
            "as": "_entry",
        }},
        {"$unwind": {"path": "$_entry", "preserveNullAndEmptyArrays": True}},
        {"$project": {"_id": 0, "account_code": 1, "debit": 1, "credit": 1,
                      "currency": "$_entry.currency", "entry_date": "$_entry.entry_date"}},
    ])
    places = decimals_for(target_currency)
    rate_cache = {}

    async def _get_rate(frm, date):
        key = (frm, date)
        if key not in rate_cache:
            rate_cache[key] = await FX.get_rate(frm, target_currency, date, db=db)
        return rate_cache[key]

    # group converted amounts per account
    out = {}
    for r in rows:
        frm = (r.get("currency") or DEFAULT_CURRENCY)
        # orphaned lines (no entry found) fall back to USD / today
        date = str(r.get("entry_date") or "")[:10] or today_str()
        for side in ("debit", "credit"):
            stored = int(r.get(side) or 0)
            if not stored:
                continue
            if frm == target_currency:
                conv_x100 = stored
            else:
                major = stored / 100.0
                rate = await _get_rate(frm, date)
                conv_x100 = int(round(round(major * rate, places) * 100))
            code = r["account_code"]
            bucket = out.setdefault(code, {"debit": 0, "credit": 0})
            bucket[side] += conv_x100
    return out


async def _report_balances(db, tenant_id, target_currency, as_of=None, date_from=None):
    """Return (balances, translated_bool). Uses the original aggregation when
    nothing needs translating so single-currency books are untouched."""
    if target_currency and await _needs_translation(
            db, tenant_id, target_currency, as_of=as_of, date_from=date_from):
        return await _translated_balances(
            db, tenant_id, target_currency, as_of=as_of, date_from=date_from), True
    return await _balances(db, tenant_id, as_of=as_of, date_from=date_from), False


async def trial_balance(db, tenant_id, as_of, target_currency=None):
    target = normalize_currency(target_currency) if target_currency else DEFAULT_CURRENCY
    bals, translated = await _report_balances(db, tenant_id, target, as_of=as_of)
    rows = []
    total_debit = 0
    total_credit = 0
    for code in sorted(bals.keys()):
        d = bals[code]["debit"]
        c = bals[code]["credit"]
        net = d - c
        debit_col = net if net > 0 else 0
        credit_col = -net if net < 0 else 0
        total_debit += debit_col
        total_credit += credit_col
        rows.append({
            "code": code, "name": NAME_OF.get(code, code),
            "type": ACCOUNT_TYPE_OF.get(code),
            "debit": debit_col, "credit": credit_col,
        })
    return {
        "as_of": as_of, "rows": rows,
        "total_debit": total_debit, "total_credit": total_credit,
        "balanced": total_debit == total_credit,
        "currency": target, "translated": translated,
    }


async def pnl(db, tenant_id, date_from, date_to, target_currency=None):
    target = normalize_currency(target_currency) if target_currency else DEFAULT_CURRENCY
    bals, translated = await _report_balances(
        db, tenant_id, target, as_of=date_to, date_from=date_from)
    revenue, expense = [], []
    rev_total = exp_total = 0
    for code, v in bals.items():
        t = ACCOUNT_TYPE_OF.get(code)
        if t == "revenue":
            amt = _net_balance(code, v["debit"], v["credit"])
            rev_total += amt
            revenue.append({"code": code, "name": NAME_OF.get(code), "amount": amt})
        elif t == "expense":
            amt = _net_balance(code, v["debit"], v["credit"])
            exp_total += amt
            expense.append({"code": code, "name": NAME_OF.get(code), "amount": amt})
    revenue.sort(key=lambda x: x["code"])
    expense.sort(key=lambda x: x["code"])
    return {
        "date_from": date_from, "date_to": date_to,
        "revenue": revenue, "expense": expense,
        "revenue_total": rev_total, "expense_total": exp_total,
        "net_income": rev_total - exp_total,
        "currency": target, "translated": translated,
    }


async def balance_sheet(db, tenant_id, as_of, target_currency=None):
    target = normalize_currency(target_currency) if target_currency else DEFAULT_CURRENCY
    bals, translated = await _report_balances(db, tenant_id, target, as_of=as_of)
    assets, liabilities, equity = [], [], []
    a_total = l_total = e_total = 0
    rev_total = exp_total = 0
    for code, v in bals.items():
        t = ACCOUNT_TYPE_OF.get(code)
        bal = _net_balance(code, v["debit"], v["credit"])
        if t == "asset":
            a_total += bal
            assets.append({"code": code, "name": NAME_OF.get(code), "amount": bal})
        elif t == "liability":
            l_total += bal
            liabilities.append({"code": code, "name": NAME_OF.get(code), "amount": bal})
        elif t == "equity":
            e_total += bal
            equity.append({"code": code, "name": NAME_OF.get(code), "amount": bal})
        elif t == "revenue":
            rev_total += bal
        elif t == "expense":
            exp_total += bal
    net_income = rev_total - exp_total
    equity.append({"code": "NI", "name": "Current Period Net Income", "amount": net_income})
    e_total += net_income
    for lst in (assets, liabilities, equity):
        lst.sort(key=lambda x: x["code"])
    return {
        "as_of": as_of,
        "assets": assets, "liabilities": liabilities, "equity": equity,
        "assets_total": a_total,
        "liabilities_total": l_total,
        "equity_total": e_total,
        "liabilities_equity_total": l_total + e_total,
        "balanced": a_total == (l_total + e_total),
        "currency": target, "translated": translated,
    }


async def general_ledger(db, tenant_id, account_code, date_from, date_to):
    s = _scoped(db, tenant_id)
    # opening balance before date_from
    opening = 0
    if date_from:
        pre = await _balances(db, tenant_id, as_of=_prev_day(date_from))
        if account_code in pre:
            opening = _net_balance(account_code, pre[account_code]["debit"], pre[account_code]["credit"])
    q = {"status": "posted", "account_code": account_code}
    if date_from:
        q.setdefault("entry_date", {})["$gte"] = date_from
    if date_to:
        q.setdefault("entry_date", {})["$lte"] = date_to
    lines = await s["lines"].find_list(q, sort=[("entry_date", 1), ("created_at", 1)])
    running = opening
    out = []
    debit_normal = NORMAL_BALANCE_OF.get(account_code, "debit") == "debit"
    for ln in lines:
        delta = (ln["debit"] - ln["credit"]) if debit_normal else (ln["credit"] - ln["debit"])
        running += delta
        out.append({
            "entry_number": ln["entry_number"], "entry_date": ln["entry_date"],
            "memo": ln.get("memo"), "debit": ln["debit"], "credit": ln["credit"],
            "balance": running,
        })
    return {
        "account_code": account_code, "account_name": NAME_OF.get(account_code, account_code),
        "opening": opening, "lines": out, "closing": running,
    }


def _prev_day(d):
    from datetime import date, timedelta
    y, m, day = map(int, d.split("-"))
    return (date(y, m, day) - timedelta(days=1)).isoformat()
