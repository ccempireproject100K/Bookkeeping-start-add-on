"""Time-tracking wiring helpers (pure functions — no DB access).

Connects approved time entries to:
  - payroll: approved hours per staffer for a pay period (pre-fills run hours)
  - jobs: approved billable hours linked to a job (analytical labor cost)

Conservative rules applied everywhere these are used:
  - only entries with status "approved" or "closed" count (closed implies approved)
  - all queries go through tenant-scoped collections (see routers)
  - time-entry labor is ANALYTICAL — it never posts journal entries, so it can
    never double-count wages already posted by payroll or manual job costs.
"""
from decimal import Decimal, ROUND_HALF_UP

APPROVED_STATUSES = ("approved", "closed")


def _in_period(date_str, start, end):
    d = (date_str or "")
    return bool(d) and start <= d <= end


def period_hours(entries, user_id, start, end):
    """Sum hours of approved entries for one user inside [start, end] (YYYY-MM-DD).

    Returns (hours, entry_ids). Payroll counts billable AND non-billable time —
    hours worked are hours paid.
    """
    total = 0.0
    ids = []
    for e in entries or []:
        if e.get("user_id") != user_id:
            continue
        if e.get("status") not in APPROVED_STATUSES:
            continue
        if not _in_period(e.get("date"), start, end):
            continue
        total += e.get("hours") or 0
        ids.append(e.get("id"))
    return round(total, 2), ids


def _labor_cents(hours, rate_cents):
    if not hours or not rate_cents:
        return 0
    return int((Decimal(str(hours)) * Decimal(str(rate_cents))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def job_time_labor(entries, job_id, rates_by_user):
    """Aggregate approved BILLABLE entries linked to a job.

    rates_by_user: {user_id: hourly_rate_cents} — entries whose staffer has no
    rate still count toward hours, with 0 cost.
    Returns {"hours", "cost_cents", "entry_count"}.
    """
    hours = 0.0
    cost = 0
    count = 0
    for e in entries or []:
        if (e.get("job_id") or "") != job_id:
            continue
        if e.get("status") not in APPROVED_STATUSES:
            continue
        if not e.get("billable", True):
            continue
        h = e.get("hours") or 0
        hours += h
        cost += _labor_cents(h, (rates_by_user or {}).get(e.get("user_id")) or 0)
        count += 1
    return {"hours": round(hours, 2), "cost_cents": cost, "entry_count": count}
