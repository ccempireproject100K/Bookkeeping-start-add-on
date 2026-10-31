"""Dashboard: headline financial tiles derived from the general ledger.
Plus (bake-off #13) a cash-position view in owner language: who owes me,
what do I owe, what's overdue, and where cash lands in the next 30 days."""
from fastapi import APIRouter, Depends
from datetime import datetime, timezone

from db import db
from lib.base import Scoped, today_str, cents_to_dollars
from lib.auth import get_current_user
from lib import ledger as L

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def dashboard(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
    as_of = today_str()
    month_start = as_of[:7] + "-01"

    bals = await L._balances(db, tenant_id, as_of=as_of)

    def bal(code):
        v = bals.get(code, {"debit": 0, "credit": 0})
        return L._net_balance(code, v["debit"], v["credit"])

    cash = bal("1010") + bal("1020") + bal("1050")
    ar = bal("1200")
    ap = bal("2010")
    inventory = bal("1300") + bal("1310")

    pnl = await L.pnl(db, tenant_id, month_start, as_of)

    recent = await Scoped(db.journal_entries, tenant_id).find_list(
        {"status": "posted"}, sort=[("entry_date", -1), ("created_at", -1)], limit=8)

    return {
        "as_of": as_of,
        "tiles": {
            "cash": cents_to_dollars(cash),
            "revenue": cents_to_dollars(pnl["revenue_total"]),
            "expenses": cents_to_dollars(pnl["expense_total"]),
            "net_income": cents_to_dollars(pnl["net_income"]),
            "ar": cents_to_dollars(ar),
            "ap": cents_to_dollars(ap),
            "inventory": cents_to_dollars(inventory),
        },
        "recent_entries": recent,
        "month": as_of[:7],
    }


@router.get("/cash-position")
async def cash_position(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
    as_of = today_str()
    bals = await L._balances(db, tenant_id, as_of=as_of)

    def bal(code):
        v = bals.get(code, {"debit": 0, "credit": 0})
        return L._net_balance(code, v["debit"], v["credit"])

    cash_codes = ["1010", "1020", "1021", "1022", "1023", "1050", "1060"]
    accounts = [{"gl_code": c, "balance_cents": bal(c)} for c in cash_codes if c in bals]
    cash = sum(a["balance_cents"] for a in accounts)

    invs = await Scoped(db.invoices, tenant_id).find_list(
        {"status": {"$in": ["issued", "partial"]}}, limit=5000)
    ar_open = ar_overdue = ar_due_30 = 0
    for inv in invs:
        bal_c = inv["total_cents"] - inv.get("paid_cents", 0)
        if bal_c <= 0:
            continue
        ar_open += bal_c
        due = (inv.get("due_date") or "")[:10]
        if due and due < as_of:
            ar_overdue += bal_c
        if due and as_of <= due <= _plus_days(as_of, 30):
            ar_due_30 += bal_c

    bills = await Scoped(db.bills, tenant_id).find_list(
        {"status": {"$in": ["open", "partial"]}}, limit=5000)
    ap_open = ap_overdue = ap_due_30 = 0
    for b in bills:
        bal_c = b["amount_cents"] - b.get("paid_cents", 0)
        if bal_c <= 0:
            continue
        ap_open += bal_c
        due = (b.get("due_date") or "")[:10]
        if due and due < as_of:
            ap_overdue += bal_c
        if due and as_of <= due <= _plus_days(as_of, 30):
            ap_due_30 += bal_c

    return {
        "as_of": as_of,
        "cash_cents": cash,
        "accounts": accounts,
        "receivables": {"open_cents": ar_open, "overdue_cents": ar_overdue,
                        "count": len(invs)},
        "payables": {"open_cents": ap_open, "overdue_cents": ap_overdue,
                     "count": len(bills)},
        "next_30_days": {"ar_due_cents": ar_due_30, "ap_due_cents": ap_due_30,
                         "projected_cash_cents": cash + ar_due_30 - ap_due_30},
    }


def _plus_days(d: str, n: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


# ---------------- Books health score (standout extra C) ----------------
# Our own scoring design: four 25-point components a shop owner can act
# on — reconciliation freshness, overdue receivables ratio, uncategorized
# transactions, and ledger assertion status (wave-2 health check).

def compute_health_score(metrics: dict) -> dict:
    flags = []
    # 1) reconciliation freshness
    days = metrics.get("days_since_reconciliation")
    if days is None:
        rec = 0
        flags.append("No bank reconciliation on record — reconcile your main account to close the month with confidence.")
    elif days <= 35:
        rec = 25
    elif days <= 65:
        rec = 18
        flags.append(f"Last reconciliation was {days} days ago — a fresh one keeps surprises out of tax time.")
    elif days <= 95:
        rec = 10
        flags.append(f"Reconciliation is {days} days stale — schedule one this week.")
    else:
        rec = 3
        flags.append(f"Reconciliation is {days} days stale — your bank and books may have drifted apart.")
    # 2) overdue receivables ratio
    open_n = metrics.get("ar_open_count", 0)
    overdue_n = metrics.get("ar_overdue_count", 0)
    ratio = (overdue_n / open_n) if open_n else 0.0
    if not open_n:
        ar = 25
    elif ratio <= 0.05:
        ar = 25
    elif ratio <= 0.15:
        ar = 18
    elif ratio <= 0.30:
        ar = 10
        flags.append(f"{overdue_n} of {open_n} open invoices are overdue — a reminder round would help.")
    else:
        ar = 4
        flags.append(f"{overdue_n} of {open_n} open invoices are overdue — collections need attention.")
    # 3) uncategorized transactions
    total_tx = metrics.get("txn_total_90d", 0)
    uncat = metrics.get("txn_uncategorized_90d", 0)
    if not total_tx:
        cat = 15
        flags.append("No bank transactions in the last 90 days — connect the bank feed so the books stay live.")
    else:
        share = uncat / total_tx
        if share <= 0.05:
            cat = 25
        elif share <= 0.20:
            cat = 17
        elif share <= 0.40:
            cat = 9
            flags.append(f"{uncat} bank transactions still need a category — rules can clear most of them automatically.")
        else:
            cat = 3
            flags.append(f"{uncat} of {total_tx} recent bank transactions are uncategorized — categorize before month-end.")
    # 4) assertions / ledger integrity
    failed = metrics.get("assertions_failed_unwaived", 0)
    unbalanced = metrics.get("unbalanced_entries", 0)
    if unbalanced:
        integ = 0
        flags.append(f"{unbalanced} posted journal entr{'y is' if unbalanced == 1 else 'ies are'} out of balance — this needs a bookkeeper's eyes.")
    elif failed == 0:
        integ = 25
    elif failed == 1:
        integ = 12
        flags.append("One bank statement balance doesn't match the books — check the assertion list.")
    else:
        integ = 0
        flags.append(f"{failed} bank statement balances don't match the books — work the assertion list before closing.")
    score = rec + ar + cat + integ
    grade = ("excellent" if score >= 90 else "good" if score >= 75 else
             "fair" if score >= 55 else "needs_attention")
    return {"score": score, "grade": grade,
            "components": {"reconciliation": rec, "receivables": ar,
                           "categorization": cat, "integrity": integ},
            "flags": flags, "metrics": metrics}


def _cleanup_active(doc: dict) -> bool:
    """Same active-record rule the dedupe router applies: merged,
    soft-deleted, and merged-into records are not live records."""
    return not (doc.get("merged_into") or doc.get("deleted_at")
                or doc.get("status") in ("merged", "deleted"))


def _looks_normalized(doc: dict) -> bool:
    """A record counts as cleanup-normalized when it carries a marker
    only the dataclean stage writes (``name_key`` / ``address_parts``)
    or a phone already in E.164 form."""
    if doc.get("name_key") or doc.get("address_parts"):
        return True
    phone = str(doc.get("phone") or "")
    return phone.startswith("+") and phone[1:].replace(" ", "").isdigit()


@router.get("/data-quality")
async def data_quality(user: dict = Depends(get_current_user)):
    """Read-only aggregate for the Cleanup Console's data-quality panel.

    Every number here is counted from the tenant's own stored records —
    nothing is estimated. One metric is deliberately reported as
    unavailable: the guided importer does not persist a per-run history,
    so an honest count of *files* cleaned this month does not exist yet;
    the endpoint returns the imported-*record* count it can prove and
    flags the file count as not tracked instead of inventing it.
    """
    tid = user["tenant_id"]
    month = today_str()[:7]

    suggestions = await Scoped(db.dedupe_suggestions, tid).find_list(limit=5000)
    pending = [s for s in suggestions if s.get("status") == "pending"]
    pending_by_entity = {
        kind: sum(1 for s in pending if s.get("entity") == kind)
        for kind in ("customers", "vendors")}

    merges = await Scoped(db.entity_merges, tid).find_list(limit=5000)
    scan_stamps = [s.get("created_at") for s in suggestions if s.get("created_at")]
    last_scan_at = max(scan_stamps) if scan_stamps else None

    normalized = total = 0
    for coll_name in ("customers", "vendors"):
        rows = await Scoped(getattr(db, coll_name), tid).find_list(limit=10000)
        live = [r for r in rows if _cleanup_active(r)]
        total += len(live)
        normalized += sum(1 for r in live if _looks_normalized(r))

    imported_this_month = 0
    for coll_name in ("customers", "vendors", "products"):
        rows = await Scoped(getattr(db, coll_name), tid).find_list(limit=10000)
        imported_this_month += sum(
            1 for r in rows
            if r.get("imported_via") == "guided_importer"
            and str(r.get("created_at") or "").startswith(month))

    return {
        "as_of": today_str(),
        "duplicates_pending": len(pending),
        "duplicates_pending_by_entity": pending_by_entity,
        "last_scan_at": last_scan_at,
        "normalized_records": normalized,
        "records_total": total,
        "normalized_percent": round(100.0 * normalized / total, 1) if total else 0.0,
        "records_imported_this_month": imported_this_month,
        "files_cleaned_this_month": None,
        "files_cleaned_available": False,
        "merges_active": sum(1 for m in merges if m.get("status") == "active"),
        "merges_reversed": sum(1 for m in merges if m.get("status") == "reversed"),
    }


@router.get("/health-score")
async def health_score(user: dict = Depends(get_current_user)):
    from datetime import date, timedelta
    tid = user["tenant_id"]
    today = date.fromisoformat(today_str())
    recs = await Scoped(db.bank_reconciliations, tid).find_list(
        sort=[("created_at", -1)], limit=1)
    days = None
    if recs:
        stamp = (recs[0].get("period_end") or recs[0].get("created_at") or "")[:10]
        try:
            days = (today - date.fromisoformat(stamp)).days
        except ValueError:
            days = None
    invs = await Scoped(db.invoices, tid).find_list(
        {"status": {"$in": ["issued", "partial"]}}, limit=5000)
    overdue = sum(1 for i in invs
                  if (i.get("due_date") or "")[:10] and i["due_date"][:10] < today_str()
                  and i["total_cents"] - i.get("paid_cents", 0) > 0)
    since = (today - timedelta(days=90)).isoformat()
    txns = await Scoped(db.bank_transactions, tid).find_list(
        {"txn_date": {"$gte": since}}, limit=10000)
    uncat = sum(1 for t in txns if t.get("status") == "imported")
    health = await L.run_health_check(db, tid)
    assertions = await Scoped(db.balance_assertions, tid).find_list(limit=1000)
    failed = 0
    for a in assertions:
        if a.get("waived"):
            continue
        computed = await L.ledger_balance_at(db, tid, a["gl_code"], a["statement_date"])
        if abs(computed - a["statement_cents"]) > a.get("tolerance_cents", 0):
            failed += 1
    metrics = {"days_since_reconciliation": days,
               "ar_open_count": len(invs), "ar_overdue_count": overdue,
               "txn_total_90d": len(txns), "txn_uncategorized_90d": uncat,
               "assertions_failed_unwaived": failed,
               "unbalanced_entries": health["counts"]["unbalanced_entries"]}
    return compute_health_score(metrics)
