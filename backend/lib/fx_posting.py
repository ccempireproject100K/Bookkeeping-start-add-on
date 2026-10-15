"""Realized + unrealized forex gain/loss postings (multi-currency Phase 3).

Clean stopping point: payments post correct realized FX entries and the
owner-triggered month-end revaluation posts unrealized entries. Reporting
stays single-currency (translation is Phase 4).

Accounting model
----------------
* Every money document carries a currency (Phase 1); daily rates live in
  the ``rates`` collection (Phase 2).
* Realized FX: when an invoice/bill in currency X is settled by a payment
  in currency Y on a later date, the base-currency difference between what
  was recognized (X at the document date) and what moved (Y at the pay
  date) posts to 4080 Foreign Exchange Gain / 5960 Foreign Exchange Loss.
* Unrealized FX: the owner-triggered revaluation re-prices open foreign
  invoices/bills/bank balances at the current rate. Per-document state in
  ``fx_reval_state`` makes re-runs idempotent and period deltas correct;
  paying a document reverses its outstanding unrealized amount first so
  nothing is ever double-counted.
* USD-only flows (document, payment and base all USD) post nothing extra:
  every rate is 1.0 and the computed difference is exactly zero.
"""
import logging

from lib import currencies as C
from lib import fx as FX
from lib import posting as P
from lib.base import Scoped, today_str
from lib.coa import FX_GAIN_CODE, FX_LOSS_CODE, ensure_fx_accounts

logger = logging.getLogger(__name__)

AR_CODE = "1200"
AP_CODE = "2010"
BANK_CODES = ("1010", "1020", "1021", "1022", "1023", "1050", "1060")


def _post_entry(db, tenant_id, user, draft, status="posted"):
    """Indirection over lib.ledger.post_journal_entry.

    Imported lazily so this module stays importable without the web stack
    (unit tests); monkeypatch fx_posting._post_entry in tests to capture
    drafts instead of touching Mongo.
    """
    from lib.ledger import post_journal_entry
    return post_journal_entry(db, tenant_id, user, draft, status=status)


# ---------------------------------------------------------------- helpers

async def _base_currency(db, tenant_id):
    tenant = await db.tenants.find_one({"id": tenant_id},
                                       {"_id": 0, "settings": 1})
    return C.get_base_currency((tenant or {}).get("settings"))


async def _doc_rate_date(db, tenant_id, kind, doc):
    """Recognition date whose rate priced the AR/AP: bill_date for bills,
    else the issue journal entry's date, else the document's created date."""
    if kind == "bill" and doc.get("bill_date"):
        return str(doc["bill_date"])[:10]
    issue_no = doc.get("issue_entry")
    if issue_no:
        entry = await db.journal_entries.find_one(
            {"tenant_id": tenant_id, "number": issue_no},
            {"_id": 0, "entry_date": 1})
        if entry and entry.get("entry_date"):
            return str(entry["entry_date"])[:10]
    return str(doc.get("created_at") or today_str())[:10]


def _minor(amount_major, currency):
    return int(round(float(amount_major) * (10 ** C.decimals_for(currency))))


def _fx_memo(direction_label, kind, doc_id, doc_cur, doc_date,
             pay_cur, pay_date, diff_minor, base):
    sign = "gain" if diff_minor > 0 else "loss"
    amt = C.from_minor_units(abs(diff_minor), base)
    return (f"{direction_label} FX {sign} on {kind} {doc_id}: "
            f"{doc_cur} recognized {doc_date} vs {pay_cur} paid {pay_date} "
            f"=> {amt:,.{C.decimals_for(base)}f} {base}")


async def _post_fx_entry(db, tenant_id, user, *, entry_date, base,
                         ar_ap_code, diff_minor, memo, source,
                         source_ref, details):
    """Post a balanced FX JE in the base currency. diff > 0 = gain."""
    await ensure_fx_accounts(Scoped(db.accounts, tenant_id))
    if diff_minor > 0:
        lines = [P.dr(ar_ap_code, diff_minor, "Realized FX gain"
                      if source == "fx_realized" else "Unrealized FX gain"),
                 P.cr(FX_GAIN_CODE, diff_minor, "Foreign exchange gain")]
    else:
        v = -diff_minor
        lines = [P.dr(FX_LOSS_CODE, v, "Realized FX loss"
                      if source == "fx_realized" else "Unrealized FX loss"),
                 P.cr(ar_ap_code, v, "Foreign exchange loss")]
    return await _post_entry(db, tenant_id, user, {
        "entry_date": entry_date,
        "currency": base,
        "source": source,
        "source_ref": source_ref,
        "memo": memo,
        "lines": lines,
        "details": details,
    }, status="posted")


# ------------------------------------------------------- realized FX

async def _reverse_doc_unrealized(db, tenant_id, user, kind, doc, base):
    """Reverse any outstanding unrealized FX for a document being settled.

    Returns the reversal entry or None. Always runs before the realized
    computation so gains/losses are never double-counted.
    """
    st = await db.fx_reval_state.find_one(
        {"tenant_id": tenant_id, "kind": kind, "doc_id": doc.get("id")})
    if not st or not st.get("last_unrealized_base_minor"):
        return None
    amt = int(st["last_unrealized_base_minor"])
    ar_ap = AR_CODE if kind == "invoice" else AP_CODE
    await ensure_fx_accounts(Scoped(db.accounts, tenant_id))
    if amt > 0:  # prior unrealized gain: dr AR/AP, cr 4080 -> reverse it
        lines = [P.dr(FX_GAIN_CODE, amt, "Reversal of unrealized FX gain"),
                 P.cr(ar_ap, amt, "Reversal of unrealized FX gain")]
    else:        # prior unrealized loss: dr 5960, cr AR/AP -> reverse it
        v = -amt
        lines = [P.dr(ar_ap, v, "Reversal of unrealized FX loss"),
                 P.cr(FX_LOSS_CODE, v, "Reversal of unrealized FX loss")]
    entry = await _post_entry(db, tenant_id, user, {
        "entry_date": today_str(),
        "currency": base,
        "source": "fx_revalue_reversal",
        "source_ref": doc.get("id"),
        "memo": (f"Reversal of unrealized FX on {kind} {doc.get('id')} "
                 f"(settled; realized gain/loss computed separately)"),
        "lines": lines,
        "details": {"kind": "unrealized_reversal", "doc_kind": kind,
                    "doc_id": doc.get("id"),
                    "reversed_base_minor": amt, "base_currency": base},
    }, status="posted")
    await db.fx_reval_state.delete_one(
        {"tenant_id": tenant_id, "kind": kind, "doc_id": doc.get("id")})
    return entry


async def post_realized_fx(*, db, tenant_id, user, kind, doc,
                           pay_currency=None, pay_amount_minor=0,
                           applied_doc_minor=None, pay_date=None):
    """Post realized FX gain/loss for a payment settling an invoice/bill.

    kind: "invoice" (AR, money in) or "bill" (AP, money out).
    doc: the invoice/bill document (must carry currency, total/paid).
    pay_currency: currency the payer actually moved (defaults to doc currency).
    pay_amount_minor: cash portion of this payment, in pay_currency minor units.
    applied_doc_minor: portion of the doc-currency balance this payment
        settles (defaults to converting pay_amount_minor at the pay-date
        rate; callers that already capped/converted pass the exact figure).
    pay_date: "YYYY-MM-DD" (defaults to today).

    Best-effort: returns the posted entry, or None when there is nothing to
    post (same-currency/base flows, zero difference) or when rates are
    unavailable. Never raises for missing FX data — the payment itself must
    not break.
    """
    base = await _base_currency(db, tenant_id)
    doc_cur = C.normalize(doc.get("currency") or "USD")
    pay_cur = C.normalize(pay_currency or doc_cur)
    pay_date = (pay_date or today_str())[:10]
    if doc_cur == base and pay_cur == base:
        return None  # fast path: every rate is 1.0, difference is zero
    direction = 1 if kind == "invoice" else -1
    ar_ap = AR_CODE if kind == "invoice" else AP_CODE

    # Settle any outstanding unrealized first so it isn't double-counted.
    try:
        await _reverse_doc_unrealized(db, tenant_id, user, kind, doc, base)
    except Exception as exc:  # reversal must not break the payment either
        logger.warning("FX unrealized reversal failed for %s %s: %s",
                       kind, doc.get("id"), exc)

    try:
        doc_date = await _doc_rate_date(db, tenant_id, kind, doc)
        if applied_doc_minor is None:
            if pay_cur == doc_cur:
                applied_doc_minor = int(pay_amount_minor)
            else:
                applied_doc_minor = await FX.convert(
                    pay_amount_minor, pay_cur, doc_cur, pay_date, db=db)
        rate_doc_then = await FX.get_rate(doc_cur, base, doc_date, db=db)
        rate_pay_now = await FX.get_rate(pay_cur, base, pay_date, db=db)
    except FX.FXRateError as exc:
        logger.warning("FX realized skipped for %s %s: %s",
                       kind, doc.get("id"), exc)
        return None

    settled_base = (C.from_minor_units(applied_doc_minor, doc_cur)
                    * rate_doc_then)
    received_base = (C.from_minor_units(pay_amount_minor, pay_cur)
                     * rate_pay_now)
    diff_minor = _minor((received_base - settled_base) * direction, base)
    if diff_minor == 0:
        return None

    memo = _fx_memo("Realized", kind, doc.get("id"), doc_cur, doc_date,
                    pay_cur, pay_date, diff_minor, base)
    details = {
        "kind": "realized",
        "doc_kind": kind,
        "doc_id": doc.get("id"),
        "doc_currency": doc_cur,
        "doc_date": doc_date,
        "doc_rate_to_base": rate_doc_then,
        "pay_currency": pay_cur,
        "pay_date": pay_date,
        "pay_rate_to_base": rate_pay_now,
        "pay_amount_minor": int(pay_amount_minor),
        "applied_doc_minor": int(applied_doc_minor),
        "settled_base_minor": _minor(settled_base, base),
        "received_base_minor": _minor(received_base, base),
        "diff_base_minor": diff_minor,
        "base_currency": base,
    }
    return await _post_fx_entry(db, tenant_id, user, entry_date=pay_date,
                                base=base, ar_ap_code=ar_ap,
                                diff_minor=diff_minor, memo=memo,
                                source="fx_realized",
                                source_ref=doc.get("id"), details=details)


# ----------------------------------------------------- unrealized FX

async def _bank_balances_by_currency(db, tenant_id, as_of):
    """{(gl_code, currency): balance_minor} for cash accounts, posted only."""
    rows = await Scoped(db.journal_lines, tenant_id).aggregate([
        {"$match": {"status": "posted", "account_code": {"$in": list(BANK_CODES)},
                    "entry_date": {"$lte": as_of}}},
        {"$lookup": {"from": "journal_entries", "localField": "entry_id",
                     "foreignField": "id", "as": "_e"}},
        {"$unwind": "$_e"},
        {"$match": {"_e.tenant_id": tenant_id, "_e.status": "posted"}},
        {"$group": {"_id": {"code": "$account_code", "currency": "$_e.currency"},
                    "balance": {"$sum": {"$subtract": ["$debit", "$credit"]}}}},
    ])
    out = {}
    for r in rows:
        key = (r["_id"]["code"], (r["_id"].get("currency") or "USD").upper())
        out[key] = int(r["balance"])
    return out


async def _post_unrealized_delta(db, tenant_id, user, *, kind, doc_id,
                                 ar_ap_code, currency, open_minor, doc_date,
                                 as_of, base, direction, state_key):
    """Post the period delta of unrealized FX for one document/balance.

    Returns the posted entry or None. State in fx_reval_state makes
    re-runs idempotent and period deltas correct.
    """
    if open_minor <= 0 or currency == base:
        return None
    try:
        r_now = await FX.get_rate(currency, base, as_of, db=db)
        r_then = await FX.get_rate(currency, base, doc_date, db=db)
    except FX.FXRateError as exc:
        logger.warning("FX revalue skipped for %s %s: %s", kind, doc_id, exc)
        return None
    cumulative = _minor(C.from_minor_units(open_minor, currency)
                        * (r_now - r_then) * direction, base)
    st = await db.fx_reval_state.find_one(
        {"tenant_id": tenant_id, "kind": state_key[0], "doc_id": state_key[1]})
    last = int((st or {}).get("last_unrealized_base_minor") or 0)
    delta = cumulative - last
    await db.fx_reval_state.update_one(
        {"tenant_id": tenant_id, "kind": state_key[0], "doc_id": state_key[1]},
        {"$set": {"last_unrealized_base_minor": cumulative,
                  "last_rate": r_now, "last_date": as_of,
                  "currency": currency, "base_currency": base}},
        upsert=True)
    if delta == 0:
        return None
    memo = _fx_memo("Unrealized", kind, doc_id, currency, doc_date,
                    currency, as_of, delta, base)
    details = {
        "kind": "unrealized",
        "doc_kind": kind,
        "doc_id": doc_id,
        "doc_currency": currency,
        "doc_date": doc_date,
        "doc_rate_to_base": r_then,
        "revalue_date": as_of,
        "revalue_rate_to_base": r_now,
        "open_minor": int(open_minor),
        "cumulative_base_minor": cumulative,
        "delta_base_minor": delta,
        "base_currency": base,
    }
    return await _post_fx_entry(db, tenant_id, user, entry_date=as_of,
                                base=base, ar_ap_code=ar_ap_code,
                                diff_minor=delta, memo=memo,
                                source="fx_revalue",
                                source_ref=f"revalue:{as_of}:{doc_id}",
                                details=details)


async def revalue_open_balances(db, tenant_id, user, as_of_date=None):
    """Owner-triggered month-end revaluation. Idempotent per date.

    Re-prices open foreign-currency invoices, bills and bank balances at
    the as-of rate and posts unrealized gain/loss deltas. Re-running for
    the same date is a no-op (returns already_done).
    """
    as_of = (as_of_date or today_str())[:10]
    base = await _base_currency(db, tenant_id)
    run = await db.fx_reval_state.find_one(
        {"tenant_id": tenant_id, "kind": "run", "doc_id": as_of})
    if run:
        return {"status": "already_done", "as_of_date": as_of,
                "base_currency": base, "entries_posted": 0, "entries": []}
    await ensure_fx_accounts(Scoped(db.accounts, tenant_id))
    posted = []

    invoices = await Scoped(db.invoices, tenant_id).find_list(
        {"status": {"$in": ["issued", "partial"]}})
    for inv in invoices:
        cur = C.normalize(inv.get("currency") or "USD")
        remaining = int(inv.get("total_cents") or 0) - int(inv.get("paid_cents") or 0)
        if cur == base or remaining <= 0:
            continue
        doc_date = await _doc_rate_date(db, tenant_id, "invoice", inv)
        e = await _post_unrealized_delta(
            db, tenant_id, user, kind="invoice", doc_id=inv["id"],
            ar_ap_code=AR_CODE, currency=cur, open_minor=remaining,
            doc_date=doc_date, as_of=as_of, base=base, direction=1,
            state_key=("invoice", inv["id"]))
        if e:
            posted.append(e["number"])

    bills = await Scoped(db.bills, tenant_id).find_list(
        {"status": {"$in": ["open", "partial"]}})
    for bill in bills:
        cur = C.normalize(bill.get("currency") or "USD")
        remaining = int(bill.get("amount_cents") or 0) - int(bill.get("paid_cents") or 0)
        if cur == base or remaining <= 0:
            continue
        doc_date = await _doc_rate_date(db, tenant_id, "bill", bill)
        e = await _post_unrealized_delta(
            db, tenant_id, user, kind="bill", doc_id=bill["id"],
            ar_ap_code=AP_CODE, currency=cur, open_minor=remaining,
            doc_date=doc_date, as_of=as_of, base=base, direction=-1,
            state_key=("bill", bill["id"]))
        if e:
            posted.append(e["number"])

    for (code, cur), balance in (await _bank_balances_by_currency(
            db, tenant_id, as_of)).items():
        if cur == base or balance == 0:
            continue
        # Banks have no single recognition date: measure against the last
        # revaluation rate (first run seeds it, posting nothing).
        st = await db.fx_reval_state.find_one(
            {"tenant_id": tenant_id, "kind": "bank", "doc_id": code})
        try:
            r_now = await FX.get_rate(cur, base, as_of, db=db)
        except FX.FXRateError as exc:
            logger.warning("FX revalue skipped for bank %s: %s", code, exc)
            continue
        last_rate = (st or {}).get("last_rate") or r_now
        delta = _minor(C.from_minor_units(balance, cur)
                       * (r_now - last_rate), base)
        await db.fx_reval_state.update_one(
            {"tenant_id": tenant_id, "kind": "bank", "doc_id": code},
            {"$set": {"last_unrealized_base_minor": 0, "last_rate": r_now,
                      "last_date": as_of, "currency": cur,
                      "base_currency": base}},
            upsert=True)
        if delta == 0:
            continue
        memo = _fx_memo("Unrealized", "bank balance", code, cur,
                        (st or {}).get("last_date") or as_of,
                        cur, as_of, delta, base)
        e = await _post_fx_entry(
            db, tenant_id, user, entry_date=as_of, base=base,
            ar_ap_code=code, diff_minor=delta, memo=memo,
            source="fx_revalue", source_ref=f"revalue:{as_of}:bank:{code}",
            details={"kind": "unrealized", "doc_kind": "bank",
                     "doc_id": code, "doc_currency": cur,
                     "doc_rate_to_base": last_rate,
                     "revalue_date": as_of, "revalue_rate_to_base": r_now,
                     "open_minor": int(balance),
                     "cumulative_base_minor": None,
                     "delta_base_minor": delta, "base_currency": base})
        posted.append(e["number"])

    await db.fx_reval_state.insert_one(
        {"tenant_id": tenant_id, "kind": "run", "doc_id": as_of,
         "entries": posted, "base_currency": base})
    return {"status": "posted", "as_of_date": as_of, "base_currency": base,
            "entries_posted": len(posted), "entries": posted}
