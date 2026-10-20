"""Monthly category budgets + cash-flow forecast.

Budgets: per-category, per-month (collection `monthly_budgets` — separate from the
annual account-code budgets in routers/reports.py). Variance = budgeted vs actual
direct expenses (db.expenses, by expense_date) in the period. Categories with spend
but no budget show up with budgeted = 0 so nothing hides.

Forecast (v2, correctness release): two layers, never blended silently —
  1. a deterministic KNOWN CURVE of dated, identifiable flows (open AR/AP,
     recurring templates on their real cadence, payroll from the last posted
     run, tax due dates, manual events), and
  2. an optional RUN-RATE BAND: trailing-90-day average daily CASH movement
     computed net of the known curve's categories, so nothing double-counts.
Plus: manual money-in/out events, saved scenarios (layer toggles, per-item
exclusions, AR haircut/shift), a 13-week roll-up, expected-pay-date
adjustment from customers' own payment history, and low-cash alerts.
Assumptions are returned with the payload and shown in the UI.

NOTE: "budget:read" / "budget:write" are NEW permissions — they must be added
to the PERMISSIONS map in backend/lib/auth.py (see MANIFEST.md). Suggested:
  "budget:read": {"owner", "accountant", "bookkeeper"},
  "budget:write": {"owner"},
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import alerts as AL

router = APIRouter(prefix="/budget", tags=["budget"])

CASH_ACCOUNTS = ["1010", "1020", "1021", "1022", "1023", "1050"]  # unified with simulator.py


def budgets(user):
    return Scoped(db.monthly_budgets, user["tenant_id"])


def expenses(user):
    return Scoped(db.expenses, user["tenant_id"])


def _period(p: str) -> str:
    """Validate a budget period: monthly YYYY-MM or quarterly YYYY-QN (Q1-Q4)."""
    if len(p) == 7 and p[4] == "-" and p[:4].isdigit() and p[5:].isdigit() and 1 <= int(p[5:]) <= 12:
        return p
    if (len(p) == 7 and p[:4].isdigit() and p[4] == "-" and p[5] == "Q"
            and p[6] in "1234"):
        return p
    raise HTTPException(status_code=400, detail="period must be YYYY-MM or YYYY-QN")


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def _period_range(p: str) -> tuple[str, str]:
    """(inclusive start, exclusive end) YYYY-MM-DD dates covered by a period."""
    p = _period(p)
    if p[5] == "Q":  # YYYY-QN
        year, q = int(p[:4]), int(p[6])
        start = date(year, (q - 1) * 3 + 1, 1)
        end = _add_months(start, 3)
    else:  # YYYY-MM
        start = date(int(p[:4]), int(p[5:]), 1)
        end = _add_months(start, 1)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------- budgets

@router.get("")
async def get_budget(period: str = Query(...), user: dict = Depends(require("budget:read"))):
    """Budget vs actuals for a month (or quarter). Includes unbudgeted categories
    that had spend. variance_cents = budgeted - actual (positive = under / favorable
    for spend); variance_pct is null when nothing was budgeted (no div-by-zero)."""
    period = _period(period)
    start, end = _period_range(period)
    rows = await budgets(user).find_list({"period": period}, limit=500)
    budgeted = {r["category"]: r for r in rows}

    actual: dict[str, int] = {}
    cur = expenses(user).find({"expense_date": {"$gte": start, "$lt": end}},
                              {"_id": 0, "category_name": 1, "category_code": 1, "amount_cents": 1})
    async for e in cur:
        cat = e.get("category_name") or e.get("category_code") or "Uncategorized"
        actual[cat] = actual.get(cat, 0) + (e.get("amount_cents") or 0)

    out = []
    for cat in sorted(set(budgeted) | set(actual)):
        b = budgeted.get(cat, {})
        b_cents = b.get("amount_cents", 0)
        a_cents = actual.get(cat, 0)
        var_cents = b_cents - a_cents
        out.append({
            "id": b.get("id"), "category": cat,
            "budgeted_cents": b_cents, "actual_cents": a_cents,
            "remaining_cents": var_cents,
            "variance_cents": var_cents,
            "variance_pct": round(var_cents / b_cents * 100, 1) if b_cents else None,
            "pct_used": round(a_cents / b_cents * 100, 1) if b_cents else (100.0 if a_cents else 0.0),
        })
    bt = sum(r["budgeted_cents"] for r in out)
    at = sum(r["actual_cents"] for r in out)
    vt = bt - at
    return {"period": period, "granularity": "quarter" if period[5] == "Q" else "month",
            "rows": out,
            "totals": {"budgeted_cents": bt, "actual_cents": at, "remaining_cents": vt,
                       "variance_cents": vt,
                       "variance_pct": round(vt / bt * 100, 1) if bt else None}}


@router.get("/categories")
async def budget_categories(user: dict = Depends(require("budget:read"))):
    """Distinct expense category names from the last 12 months, for the budget form."""
    since = (date.fromisoformat(today_str()) - timedelta(days=365)).isoformat()
    cats = await expenses(user).c.distinct(
        "category_name", {"tenant_id": user["tenant_id"], "expense_date": {"$gte": since}})
    return sorted(c for c in cats if c)


class BudgetIn(BaseModel):
    period: str
    category: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0)  # dollars


@router.post("")
async def upsert_budget(body: BudgetIn, user: dict = Depends(require("budget:write"))):
    period = _period(body.period)
    col = budgets(user).c
    doc = await col.find_one_and_update(
        {"tenant_id": user["tenant_id"], "period": period, "category": body.category.strip()},
        {"$set": {"amount_cents": to_cents(body.amount), "updated_at": now_iso()},
         "$setOnInsert": {"id": new_id(), "period": period, "category": body.category.strip(),
                          "created_at": now_iso()}},
        upsert=True, return_document=True, projection={"_id": 0})
    await L.audit(db, user["tenant_id"], user, "budget.upsert",
                  {"period": period, "category": body.category})
    return doc


@router.delete("/{bid}")
async def delete_budget(bid: str, user: dict = Depends(require("budget:write"))):
    res = await budgets(user).c.delete_one({"tenant_id": user["tenant_id"], "id": bid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Budget not found")
    await L.audit(db, user["tenant_id"], user, "budget.delete", {"id": bid})
    return {"ok": True}


class CopyIn(BaseModel):
    from_period: str
    to_period: str


@router.post("/copy")
async def copy_budget(body: CopyIn, user: dict = Depends(require("budget:write"))):
    src, dst = _period(body.from_period), _period(body.to_period)
    if src == dst:
        raise HTTPException(status_code=400, detail="Pick a different target month")
    col = budgets(user).c
    existing = {d["category"] async for d in col.find(
        {"tenant_id": user["tenant_id"], "period": dst}, {"_id": 0, "category": 1})}
    n = 0
    async for d in col.find({"tenant_id": user["tenant_id"], "period": src}, {"_id": 0}):
        if d["category"] in existing:
            continue
        await col.insert_one({"tenant_id": user["tenant_id"], "id": new_id(), "period": dst,
                              "category": d["category"], "amount_cents": d.get("amount_cents", 0),
                              "created_at": now_iso()})
        n += 1
    await L.audit(db, user["tenant_id"], user, "budget.copy", {"from": src, "to": dst, "copied": n})
    return {"ok": True, "copied": n}


# ---------------------------------------------------------------- forecast
#
# Forecast v2 (correctness release). Two layers, never blended silently:
#   1. KNOWN CURVE — deterministic: starting cash + dated, identifiable
#      flows only (open AR/AP on their dates, recurring templates on their
#      real cadence, payroll from the last posted run, tax due dates,
#      manual events). This is the headline curve.
#   2. RUN-RATE BAND — optional and clearly labeled: trailing-90-day
#      average daily CASH movement (journal lines on the cash accounts),
#      computed NET of the categories already in the known curve (invoice
#      receipts, bill payments and payroll postings are excluded by journal
#      source), on a cash basis — never accrual P&L — so no flow is ever
#      counted twice.
# Manual events (db.forecast_events) are QBO-style money-in/out what-if
# items; they also carry the simulator's hire / one-time / recurring
# shapes. Scenarios (db.forecast_scenarios) bundle layer toggles, per-item
# exclusions, an AR haircut and an AR shift, under a name.
# NOT in v1 (stated plainly): ML prediction of pay dates or income, and
# bank-feed pattern mining.

# Journal sources whose cash movement is already projected in the known
# curve — excluded from the run-rate band (defect: v0 layered scheduled
# items on top of a baseline that already contained them).
BAND_EXCLUDED_SOURCES = {"invoice_paid", "portal_payment", "stripe_portal",
                         "ap_payment", "payroll"}


def _fc_events(user):
    return Scoped(db.forecast_events, user["tenant_id"])


def _fc_scenarios(user):
    return Scoped(db.forecast_scenarios, user["tenant_id"])


def _advance(d: date, frequency: str) -> date:
    """Next occurrence for weekly/biweekly/monthly/quarterly cadences.
    Monthly keeps the day-of-month (clamped by _add_months), matching the
    recurring-invoice and pay-schedule conventions."""
    f = (frequency or "monthly").lower()
    if f == "weekly":
        return d + timedelta(days=7)
    if f == "biweekly":
        return d + timedelta(days=14)
    if f == "quarterly":
        return _add_months(d, 3)
    return _add_months(d, 1)


def _advance_bill(d: date, frequency: str) -> date:
    """Recurring-bill cadence — mirrors bills.py generate-next exactly
    (weekly +7d, monthly +30d, quarterly +90d) so the projection matches
    what materialization will actually create."""
    days = {"weekly": 7, "monthly": 30, "quarterly": 90}.get(
        (frequency or "monthly").lower(), 30)
    return d + timedelta(days=days)


def _item(key, series_key, when, kind, label, direction, amount_cents):
    return {"key": key, "series_key": series_key, "date": when, "kind": kind,
            "label": label, "direction": direction, "amount_cents": amount_cents}


async def _customer_pay_stats(tenant_id):
    """Per-customer average days-late from PAID invoices (only customers
    with >= 3 paid invoices — the honest deterministic substitute for ML
    pay-date prediction). Days-late = last payment date - due date
    (negative means the customer usually pays early)."""
    agg: dict[str, list[int]] = {}
    async for inv in Scoped(db.invoices, tenant_id).find(
            {"status": "paid"},
            {"_id": 0, "customer_id": 1, "client_name": 1, "due_date": 1, "payments": 1}):
        due = inv.get("due_date")
        pays = [p for p in inv.get("payments") or [] if p.get("date")]
        if not due or not pays:
            continue
        try:
            late = (date.fromisoformat(max(p["date"] for p in pays))
                    - date.fromisoformat(due)).days
        except ValueError:
            continue
        key = inv.get("customer_id") or inv.get("client_name") or ""
        if key:
            agg.setdefault(key, []).append(late)
    return {k: {"avg_days_late": int(round(sum(v) / len(v))), "samples": len(v)}
            for k, v in agg.items() if len(v) >= 3}


async def _ar_items(tenant_id, today, end, pay_stats, use_expected,
                    haircut_pct, shift_days):
    items, adjustments, seen = [], [], set()
    async for inv in Scoped(db.invoices, tenant_id).find(
            {"status": {"$in": ["issued", "partial"]}},
            {"_id": 0, "id": 1, "customer_id": 1, "client_name": 1,
             "due_date": 1, "total_cents": 1, "paid_cents": 1}):
        bal = (inv.get("total_cents") or 0) - (inv.get("paid_cents") or 0)
        if bal <= 0:
            continue
        try:
            when = date.fromisoformat(inv.get("due_date") or "")
        except ValueError:
            when = today
        cust_key = inv.get("customer_id") or inv.get("client_name") or ""
        if use_expected and cust_key in pay_stats:
            when += timedelta(days=pay_stats[cust_key]["avg_days_late"])
            if cust_key not in seen:
                seen.add(cust_key)
                adjustments.append({
                    "customer": inv.get("client_name") or cust_key,
                    "avg_days_late": pay_stats[cust_key]["avg_days_late"],
                    "samples": pay_stats[cust_key]["samples"]})
        when += timedelta(days=shift_days)
        if when < today:
            when = today
        if when > end:
            continue
        amt = int(round(bal * (1 - haircut_pct / 100.0))) if haircut_pct else bal
        if amt > 0:
            items.append(_item(f"ar:{inv['id']}", f"ar:{inv['id']}",
                               when.isoformat(), "ar",
                               f"Invoice — {inv.get('client_name') or inv['id']}",
                               "in", amt))
    return items, adjustments


async def _ap_items(tenant_id, today, end):
    items = []
    async for b in Scoped(db.bills, tenant_id).find(
            {"status": {"$in": ["open", "partial"]}},
            {"_id": 0, "id": 1, "vendor": 1, "due_date": 1,
             "amount_cents": 1, "paid_cents": 1}):
        bal = (b.get("amount_cents") or 0) - (b.get("paid_cents") or 0)
        if bal <= 0:
            continue
        try:
            when = date.fromisoformat(b.get("due_date") or "")
        except ValueError:
            when = today
        if when < today:
            when = today
        if when > end:
            continue
        items.append(_item(f"ap:{b['id']}", f"ap:{b['id']}",
                           when.isoformat(), "ap",
                           f"Bill — {b.get('vendor') or b['id']}", "out", bal))
    return items


async def _recurring_invoice_items(tenant_id, today, end, pay_stats,
                                   use_expected, haircut_pct, shift_days):
    """Projected from each template's REAL next_run/active cadence (v0
    invented the 1st-of-month / every-7-days dates and re-counted invoices
    that had already materialized into AR). Occurrences before today are
    assumed materialized — those invoices live in AR and are counted once,
    there. A template behind schedule contributes ONE catch-up occurrence
    today (generate-due materializes one per run)."""
    items = []
    async for r in Scoped(db.recurring_invoices, tenant_id).find(
            {"active": True},
            {"_id": 0, "id": 1, "name": 1, "client_name": 1, "customer_id": 1,
             "frequency": 1, "next_run": 1, "lines": 1, "tax": 1}):
        total = sum(to_cents(l.get("amount") or 0) for l in r.get("lines", [])) \
            + to_cents(r.get("tax") or 0)
        if total <= 0:
            continue
        try:
            d = date.fromisoformat(r.get("next_run") or "")
        except ValueError:
            continue
        freq = (r.get("frequency") or "monthly").lower()
        cust_key = r.get("customer_id") or r.get("client_name") or ""
        lag = pay_stats.get(cust_key, {}).get("avg_days_late", 0) if use_expected else 0
        dates = set()
        if d < today:
            dates.add(today)  # one pending catch-up occurrence
            while d < today:
                d = _advance(d, freq)
        while d <= end:
            dates.add(d)
            d = _advance(d, freq)
        amt = int(round(total * (1 - haircut_pct / 100.0))) if haircut_pct else total
        if amt <= 0:
            continue
        for occ in sorted(dates):
            when = occ + timedelta(days=lag + shift_days)
            if when < today:
                when = today
            if when > end:
                continue
            ws = when.isoformat()
            items.append(_item(f"recin:{r['id']}:{ws}", f"recin:{r['id']}", ws,
                               "recurring_invoice",
                               f"Recurring invoice — {r.get('name') or r.get('client_name') or r['id']}",
                               "in", amt))
    return items


async def _recurring_bill_items(tenant_id, today, end):
    """Recurring bills. SOURCE FINDING: v0 read expense templates from a
    `db.recurring` collection that NO router, seed or UI writes — a phantom.
    The real recurring-bill data lives on bills themselves
    (bills.recurring/frequency, cloned by POST /bills/{id}/generate-next).
    The read is fixed here: project future occurrences from the latest
    bill of each chain (vendor + amount + frequency + expense code). The
    chain head itself, if still open, is counted once in AP — projection
    starts strictly after its bill_date, so nothing double-counts."""
    chains: dict[tuple, dict] = {}
    async for b in Scoped(db.bills, tenant_id).find(
            {"recurring": True},
            {"_id": 0, "id": 1, "vendor": 1, "amount_cents": 1, "bill_date": 1,
             "due_date": 1, "frequency": 1, "expense_code": 1}):
        key = (b.get("vendor"), b.get("amount_cents"),
               (b.get("frequency") or "monthly"), b.get("expense_code"))
        head = chains.get(key)
        if head is None or (b.get("bill_date") or "") > (head.get("bill_date") or ""):
            chains[key] = b
    items = []
    for b in chains.values():
        amt = b.get("amount_cents") or 0
        if amt <= 0:
            continue
        try:
            bd = date.fromisoformat(b["bill_date"])
            offset = (date.fromisoformat(b.get("due_date") or "") - bd).days
        except (KeyError, ValueError):
            continue
        d = bd
        for _ in range(400):
            d = _advance_bill(d, b.get("frequency") or "monthly")
            due = d + timedelta(days=offset)
            if due > end:
                break
            if due >= today:
                ds = due.isoformat()
                items.append(_item(f"recbill:{b['id']}:{ds}", f"recbill:{b['id']}",
                                   ds, "recurring_bill",
                                   f"Recurring bill — {b.get('vendor') or b['id']}",
                                   "out", amt))
    return items


async def _payroll_items(tenant_id, today, end):
    """Payroll layer: project each active schedule's runs at the last
    POSTED run's total cash cost (net pay + employee tax deductions +
    employer payroll taxes, excluding booth rent, which stays in the
    shop), on the schedule's real next_run cadence. Labeled 'based on
    last run'. A schedule with no posted run produces an explicit gap
    note — never a silent zero."""
    items, notes = [], []
    schedules = await Scoped(db.pay_schedules, tenant_id).find_list(
        {"active": True}, limit=200)
    if not schedules:
        notes.append("Payroll is not projected: no active pay schedule is configured.")
        return items, notes
    for s in schedules:
        found = await Scoped(db.payroll_runs, tenant_id).find_list(
            {"schedule_id": s["id"], "status": "posted"},
            sort=[("period_end", -1)], limit=1)
        if not found:
            notes.append(f"Payroll schedule '{s.get('name')}' has no posted run "
                         "yet — its payroll is not projected.")
            continue
        t = found[0].get("totals") or {}
        booth = t.get("booth_rent_cents") or 0
        if t.get("employer_cost_cents") is not None:
            # Post-#149 totals: employer_cost_cents already includes gross
            # plus ALL employer taxes (FICA + FUTA + SUTA + ETT), while
            # employer_tax_cents was narrowed to employer FICA only — the
            # old formula would under-project by FUTA+SUTA+ETT per run.
            cost = t["employer_cost_cents"] - booth
        else:
            # Legacy runs posted before #149: employer_tax_cents was the
            # FULL employer-tax figure and no employer_cost_cents exists.
            cost = (t.get("gross_cents") or 0) - booth \
                + (t.get("employer_tax_cents") or 0)
        if cost <= 0:
            continue
        try:
            d = date.fromisoformat(s.get("next_run") or "")
        except ValueError:
            notes.append(f"Payroll schedule '{s.get('name')}' has no valid "
                         "next_run date — its payroll is not projected.")
            continue
        freq = s.get("frequency") or "monthly"
        while d < today:
            d = _advance(d, freq)
        while d <= end:
            ds = d.isoformat()
            items.append(_item(
                f"payroll:{s['id']}:{ds}", f"payroll:{s['id']}", ds, "payroll",
                f"Payroll — {s.get('name')} (based on last run {found[0].get('period_end')})",
                "out", cost))
            d = _advance(d, freq)
    return items, notes


async def _income_tax_items(user, today, end):
    """Tax layer, income-tax part. ADAPTER over routers/taxforecast.py's
    simplified estimator — swap this one function for PR #147's engine
    schedule (quarters[] with due date + remaining) when that merges.
    Semantics: full-year estimate (annualized from year-to-date) minus
    payments already recorded, spread evenly over the remaining estimate
    due dates of the tax year."""
    from routers import taxforecast as TF
    tenant_id = user["tenant_id"]
    doc = await Scoped(db.tax_forecast_config, tenant_id).find_one({"id": "settings"})
    settings = {"brackets": TF.DEFAULT_BRACKETS,
                "std_deduction": TF.DEFAULT_STD_DEDUCTION, "filing": "single",
                "prior_year_tax_cents": 0, "prior_year_agi_cents": 0,
                "ytd_other_income_cents": 0}
    if doc:
        settings.update({k: v for k, v in doc.items()
                         if k not in ("_id", "id", "tenant_id")})
    year = today.year
    ytd = await L.pnl(db, tenant_id, f"{year}-01-01", today.isoformat())
    doy = today.timetuple().tm_yday
    year_days = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    annualized = int(round(ytd["net_income"] / max(doy, 1) * year_days))
    total_cents = int(round(TF._estimate(annualized, settings)["total"] * 100))
    paid = 0
    async for p in Scoped(db.tax_payments, tenant_id).find(
            {"year": str(year)}, {"_id": 0, "amount_cents": 1}):
        paid += p.get("amount_cents") or 0
    remaining = total_cents - paid
    if remaining <= 0:
        return [], [f"Income-tax estimates for {year} appear fully covered by "
                    "payments recorded — no income-tax payments projected."]
    if year == 2026:
        quarters = TF.QUARTERS_2026
    else:
        quarters = [{"q": "Q1", "due": f"{year}-04-15"},
                    {"q": "Q2", "due": f"{year}-06-15"},
                    {"q": "Q3", "due": f"{year}-09-15"},
                    {"q": "Q4", "due": f"{year + 1}-01-15"}]
    dues = [q for q in quarters if q["due"] >= today.isoformat()]
    if not dues:  # past Jan 15: the schedule belongs to next year's quarters
        year += 1
        dues = [{"q": "Q1", "due": f"{year}-04-15"},
                {"q": "Q2", "due": f"{year}-06-15"},
                {"q": "Q3", "due": f"{year}-09-15"},
                {"q": "Q4", "due": f"{year + 1}-01-15"}]
    share, extra = divmod(remaining, len(dues))
    items = []
    for i, q in enumerate(dues):
        amt = share + (extra if i == len(dues) - 1 else 0)
        if amt > 0 and q["due"] <= end.isoformat():
            items.append(_item(f"tax:{year}:{q['q']}", "tax:income", q["due"],
                               "tax",
                               f"Estimated income tax {year} {q['q']} (simplified estimator)",
                               "out", amt))
    note = (f"Income-tax layer: remaining estimate of ${remaining / 100:,.2f} for "
            f"{year} (annualized year-to-date, simplified estimator) spread over "
            "the remaining due dates. PR #147's engine will replace this source.")
    return items, [note]


def _st_due_dates(freq: str, due_day: int, today: date, end: date) -> list[date]:
    out = []
    if freq == "monthly":
        y, m = today.year, today.month
        for _ in range(16):
            d = date(y, m, due_day)
            if today <= d <= end:
                out.append(d)
            m += 1
            if m > 12:
                m, y = 1, y + 1
    elif freq == "quarterly":  # due in Jan/Apr/Jul/Oct, after each quarter
        y, m = today.year, ((today.month - 1) // 3) * 3 + 1
        for _ in range(8):
            d = date(y, m, due_day)
            if today <= d <= end:
                out.append(d)
            m += 3
            if m > 12:
                m, y = m - 12, y + 1
    else:  # annual — due in January
        for y in (today.year, today.year + 1, today.year + 2):
            d = date(y, 1, due_day)
            if today <= d <= end:
                out.append(d)
    return out


async def _sales_tax_items(tenant_id, today, end):
    """Tax layer, sales-tax part: for jurisdictions with a filing schedule
    (filing_frequency + filing_due_day on the sales-tax jurisdiction),
    project this period's collected tax onto the filing due dates in the
    window. Since PR #150 those fields are ALWAYS stored on a jurisdiction
    (defaults monthly / due day 20), so every current jurisdiction
    projects; only pre-#150 legacy jurisdictions lacking the fields count
    as unconfigured, and if none is configured the layer emits an explicit
    gap note — never a silent zero. Registrations are #150's filing-
    obligation record (its worksheets key on db.sales_tax_registrations):
    a tenant projecting from jurisdiction schedules with NO registration
    on file also gets a note — the dates are schedule-based, not
    registration-confirmed."""
    items, notes = [], []
    jurs = await Scoped(db.tax_jurisdictions, tenant_id).find_list({}, limit=200)
    configured = [j for j in jurs
                  if j.get("filing_frequency") and j.get("filing_due_day")]
    if jurs and not configured:
        notes.append("Sales tax is tracked but no jurisdiction has a filing "
                     "schedule configured — sales-tax payments are not projected.")
        return items, notes
    if configured:
        regs = await Scoped(db.sales_tax_registrations, tenant_id).find_list(
            {}, limit=1)
        if not regs:
            notes.append(
                "Sales tax: no state registration is on file — payment dates "
                "follow each jurisdiction's filing schedule. Add registrations "
                "under Sales Tax settings so due dates reflect your actual "
                "filing obligations.")
    for j in configured:
        freq = j["filing_frequency"]
        if freq == "monthly":
            pstart = today.replace(day=1)
        elif freq == "quarterly":
            pstart = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        else:
            pstart = date(today.year, 1, 1)
        amt = 0
        async for inv in Scoped(db.invoices, tenant_id).find(
                {"tax_jurisdiction_id": j["id"]},
                {"_id": 0, "tax_cents": 1, "created_at": 1}):
            if (inv.get("created_at") or "")[:10] >= pstart.isoformat():
                amt += inv.get("tax_cents") or 0
        if amt <= 0:
            continue
        for d in _st_due_dates(freq, int(j["filing_due_day"]), today, end):
            ds = d.isoformat()
            items.append(_item(f"salestax:{j['id']}:{ds}", f"salestax:{j['id']}",
                               ds, "sales_tax",
                               f"Sales tax — {j.get('name')} (collected this period; estimate)",
                               "out", amt))
    return items, notes


async def _event_items(tenant_id, scenario_id, today, end):
    """Manual money-in/out events. Base events (no scenario_id) always
    apply; a scenario's own events apply only when that scenario is run."""
    items = []
    docs = await Scoped(db.forecast_events, tenant_id).find_list(
        {"active": True}, limit=500)
    for ev in docs:
        if scenario_id is None and ev.get("scenario_id"):
            continue
        if scenario_id is not None \
                and ev.get("scenario_id") not in (None, scenario_id):
            continue
        amt = ev.get("amount_cents") or 0
        if amt <= 0:
            continue
        try:
            d = date.fromisoformat(ev["date"])
        except (KeyError, ValueError):
            continue
        dend = None
        if ev.get("end_date"):
            try:
                dend = date.fromisoformat(ev["end_date"])
            except ValueError:
                dend = None
        freq = ev.get("frequency")
        n = 0
        while d <= end and n < 400:
            if dend is None or d <= dend:
                when = max(d, today) if not freq else d
                if when >= today:
                    ws = when.isoformat()
                    items.append(_item(f"event:{ev['id']}:{ws}", f"event:{ev['id']}",
                                       ws, "event",
                                       ev.get("name") or "Manual event",
                                       ev.get("direction") or "out", amt))
            if not freq:
                break
            d = _advance(d, freq)
            n += 1
    return items


async def _run_rate_band(tenant_id, today):
    """Trailing-90-day average daily CASH movement: posted journal lines
    on the cash accounts, excluding the journal sources already projected
    in the known curve (invoice receipts, portal receipts, bill payments,
    payroll postings) — cash basis, net of scheduled categories."""
    win_start = (today - timedelta(days=90)).isoformat()
    t_iso = today.isoformat()
    excluded = set()
    async for e in Scoped(db.journal_entries, tenant_id).find(
            {"status": "posted",
             "entry_date": {"$gte": win_start, "$lt": t_iso},
             "source": {"$in": sorted(BAND_EXCLUDED_SOURCES)}},
            {"_id": 0, "id": 1}):
        excluded.add(e["id"])
    tot_in = tot_out = 0
    async for ln in Scoped(db.journal_lines, tenant_id).find(
            {"status": "posted",
             "entry_date": {"$gte": win_start, "$lt": t_iso},
             "account_code": {"$in": CASH_ACCOUNTS}},
            {"_id": 0, "entry_id": 1, "debit": 1, "credit": 1}):
        if ln.get("entry_id") in excluded:
            continue
        tot_in += ln.get("debit") or 0
        tot_out += ln.get("credit") or 0
    return {"daily_inflow_cents": tot_in // 90,
            "daily_outflow_cents": tot_out // 90,
            "window_days": 90, "basis": "cash",
            "excluded_sources": sorted(BAND_EXCLUDED_SOURCES)}


# ---------------------------------------------------------- forecast CRUD

class ForecastEventIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    direction: str = Field(pattern=r"^(in|out)$")
    amount: float = Field(default=0, ge=0)          # dollars (ignored for hire)
    date: str = Field(min_length=10, max_length=10)  # first occurrence
    frequency: Optional[str] = Field(default=None, pattern=r"^(weekly|monthly|quarterly)$")
    end_date: Optional[str] = None
    scenario_id: Optional[str] = None
    shape: str = Field(default="custom", pattern=r"^(custom|hire|one_time|recurring)$")
    annual_salary: Optional[float] = Field(default=None, ge=0)  # hire shape
    burden_percent: float = Field(default=25.0, ge=0, le=100)   # hire shape


@router.get("/forecast/events")
async def list_forecast_events(scenario_id: Optional[str] = None,
                               user: dict = Depends(require("budget:read"))):
    docs = await _fc_events(user).find_list({"active": True},
                                            sort=[("date", 1)], limit=500)
    if scenario_id is not None:
        docs = [d for d in docs if d.get("scenario_id") in (None, scenario_id)]
    else:
        docs = [d for d in docs if not d.get("scenario_id")]
    return docs


@router.post("/forecast/events")
async def create_forecast_event(body: ForecastEventIn,
                                user: dict = Depends(require("budget:write"))):
    try:
        date.fromisoformat(body.date)
        if body.end_date:
            date.fromisoformat(body.end_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date/end_date must be YYYY-MM-DD")
    freq = body.frequency
    if body.shape == "hire":
        if not body.annual_salary:
            raise HTTPException(status_code=422,
                                detail="hire events need annual_salary")
        monthly = body.annual_salary / 12 * (1 + body.burden_percent / 100)
        amount_cents = to_cents(monthly)
        freq = "monthly"
    elif body.shape == "one_time":
        amount_cents = to_cents(body.amount)
        freq = None
    else:
        amount_cents = to_cents(body.amount)
    if amount_cents <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    if body.scenario_id:
        sc = await _fc_scenarios(user).find_one({"id": body.scenario_id})
        if not sc:
            raise HTTPException(status_code=404, detail="Scenario not found")
    doc = {"id": new_id(), "name": body.name.strip(), "direction": body.direction,
           "amount_cents": amount_cents, "date": body.date, "frequency": freq,
           "end_date": body.end_date, "scenario_id": body.scenario_id,
           "shape": body.shape, "active": True, "created_at": now_iso()}
    await _fc_events(user).insert_one(doc)
    await L.audit(db, user["tenant_id"], user, "forecast.event.create",
                  {"name": doc["name"], "amount_cents": amount_cents})
    return doc


@router.delete("/forecast/events/{eid}")
async def delete_forecast_event(eid: str, user: dict = Depends(require("budget:write"))):
    res = await _fc_events(user).c.delete_one(
        {"tenant_id": user["tenant_id"], "id": eid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Event not found")
    await L.audit(db, user["tenant_id"], user, "forecast.event.delete", {"id": eid})
    return {"ok": True}


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    run_rate: bool = False
    expected_pay_dates: bool = False
    payroll: bool = True
    tax: bool = True
    excluded_keys: list[str] = []
    ar_haircut_pct: float = Field(default=0, ge=0, le=100)
    ar_shift_days: int = Field(default=0, ge=-90, le=365)


@router.get("/forecast/scenarios")
async def list_scenarios(user: dict = Depends(require("budget:read"))):
    return await _fc_scenarios(user).find_list(sort=[("name", 1)], limit=200)


@router.post("/forecast/scenarios")
async def create_scenario(body: ScenarioIn, user: dict = Depends(require("budget:write"))):
    doc = {"id": new_id(), **body.model_dump(), "name": body.name.strip(),
           "created_at": now_iso(), "updated_at": now_iso()}
    await _fc_scenarios(user).insert_one(doc)
    await L.audit(db, user["tenant_id"], user, "forecast.scenario.create",
                  {"name": doc["name"]})
    return doc


@router.put("/forecast/scenarios/{sid}")
async def update_scenario(sid: str, body: ScenarioIn,
                          user: dict = Depends(require("budget:write"))):
    found = await _fc_scenarios(user).find_one({"id": sid})
    if not found:
        raise HTTPException(status_code=404, detail="Scenario not found")
    patch = {**body.model_dump(), "name": body.name.strip(), "updated_at": now_iso()}
    await _fc_scenarios(user).update_one({"id": sid}, {"$set": patch})
    return await _fc_scenarios(user).find_one({"id": sid})


@router.delete("/forecast/scenarios/{sid}")
async def delete_scenario(sid: str, user: dict = Depends(require("budget:write"))):
    res = await _fc_scenarios(user).c.delete_one(
        {"tenant_id": user["tenant_id"], "id": sid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Scenario not found")
    await L.audit(db, user["tenant_id"], user, "forecast.scenario.delete", {"id": sid})
    return {"ok": True}


# ---------------------------------------------------------- forecast v2

@router.get("/forecast")
async def forecast(days: int = Query(90, ge=7, le=365),
                   scenario_id: Optional[str] = None,
                   run_rate: Optional[bool] = None,
                   expected_pay_dates: Optional[bool] = None,
                   ar_haircut_pct: Optional[float] = None,
                   ar_shift_days: Optional[int] = None,
                   alert_threshold_cents: int = 0,
                   user: dict = Depends(require("budget:read"))):
    tenant_id = user["tenant_id"]
    today = date.fromisoformat(today_str())
    end = today + timedelta(days=days)
    t_iso, e_iso = today.isoformat(), end.isoformat()

    # ---- configuration: defaults -> scenario -> explicit query overrides
    cfg = {"run_rate": False, "expected_pay_dates": False, "payroll": True,
           "tax": True, "excluded_keys": [], "ar_haircut_pct": 0.0,
           "ar_shift_days": 0}
    scenario = None
    if scenario_id:
        scenario = await _fc_scenarios(user).find_one({"id": scenario_id})
        if not scenario:
            raise HTTPException(status_code=404, detail="Scenario not found")
        for k in cfg:
            if k in scenario:
                cfg[k] = scenario[k]
    if run_rate is not None:
        cfg["run_rate"] = run_rate
    if expected_pay_dates is not None:
        cfg["expected_pay_dates"] = expected_pay_dates
    if ar_haircut_pct is not None:
        cfg["ar_haircut_pct"] = ar_haircut_pct
    if ar_shift_days is not None:
        cfg["ar_shift_days"] = ar_shift_days
    excluded = set(cfg["excluded_keys"] or [])

    # ---- starting cash (posted GL, unified cash-account set)
    bals = await L._balances(db, tenant_id, as_of=t_iso)
    start_cash = sum(L._net_balance(c, bals.get(c, {"debit": 0, "credit": 0})["debit"],
                                    bals.get(c, {"debit": 0, "credit": 0})["credit"])
                     for c in CASH_ACCOUNTS)

    # ---- known-curve layers
    notes: list[str] = []
    pay_stats = await _customer_pay_stats(tenant_id) if cfg["expected_pay_dates"] else {}
    items: list[dict] = []
    ar_items, adjustments = await _ar_items(
        tenant_id, today, end, pay_stats, cfg["expected_pay_dates"],
        cfg["ar_haircut_pct"], cfg["ar_shift_days"])
    items += ar_items
    items += await _ap_items(tenant_id, today, end)
    items += await _recurring_invoice_items(
        tenant_id, today, end, pay_stats, cfg["expected_pay_dates"],
        cfg["ar_haircut_pct"], cfg["ar_shift_days"])
    items += await _recurring_bill_items(tenant_id, today, end)
    payroll_total = 0
    if cfg["payroll"]:
        p_items, p_notes = await _payroll_items(tenant_id, today, end)
        items += p_items
        notes += p_notes
        payroll_total = sum(i["amount_cents"] for i in p_items)
    tax_total = sales_tax_total = 0
    if cfg["tax"]:
        t_items, t_notes = await _income_tax_items(user, today, end)
        items += t_items
        notes += t_notes
        tax_total = sum(i["amount_cents"] for i in t_items)
        st_items, st_notes = await _sales_tax_items(tenant_id, today, end)
        items += st_items
        notes += st_notes
        sales_tax_total = sum(i["amount_cents"] for i in st_items)
    items += await _event_items(tenant_id, scenario_id, today, end)

    items = [i for i in items
             if i["key"] not in excluded and i["series_key"] not in excluded]
    items.sort(key=lambda i: (i["date"], i["kind"], i["label"]))

    # ---- run-rate band (computed net of the known curve's categories)
    band = await _run_rate_band(tenant_id, today)
    band_in = band["daily_inflow_cents"] if cfg["run_rate"] else 0
    band_out = band["daily_outflow_cents"] if cfg["run_rate"] else 0

    known_in: dict[str, int] = {}
    known_out: dict[str, int] = {}
    for i in items:
        bucket = known_in if i["direction"] == "in" else known_out
        bucket[i["date"]] = bucket.get(i["date"], 0) + i["amount_cents"]

    points = []
    cash = start_cash
    min_cash, min_date = cash, t_iso
    first_below = t_iso if cash < alert_threshold_cents else None
    d = today
    while d <= end:
        key = d.isoformat()
        ki, ko = known_in.get(key, 0), known_out.get(key, 0)
        cash += (ki + band_in) - (ko + band_out)
        if cash < min_cash:
            min_cash, min_date = cash, key
        if first_below is None and cash < alert_threshold_cents:
            first_below = key
        points.append({"date": key, "known_in_cents": ki, "known_out_cents": ko,
                       "inflow_cents": ki + band_in, "outflow_cents": ko + band_out,
                       "cash_cents": cash})
        d += timedelta(days=1)

    # ---- 13-week weekly roll-up of the headline curve
    weekly = []
    for w in range(13):
        chunk = points[w * 7:(w + 1) * 7]
        if not chunk:
            break
        weekly.append({
            "week": w + 1, "start_date": chunk[0]["date"], "end_date": chunk[-1]["date"],
            "inflow_cents": sum(p["inflow_cents"] for p in chunk),
            "outflow_cents": sum(p["outflow_cents"] for p in chunk),
            "net_cents": sum(p["inflow_cents"] - p["outflow_cents"] for p in chunk),
            "end_cash_cents": chunk[-1]["cash_cents"]})

    # ---- low-cash alerting (lib/alerts.py; deduped on the exact detail)
    alert_info = {"threshold_cents": alert_threshold_cents,
                  "first_below_date": first_below, "alert_raised": False}
    if first_below:
        detail = (f"Cash-flow forecast: cash dips to ${min_cash / 100:,.2f} on "
                  f"{min_date}, below the ${alert_threshold_cents / 100:,.2f} threshold.")
        existing = await db.security_alerts.find_one(
            {"tenant_id": tenant_id, "type": "low_cash_forecast",
             "detail": detail, "acknowledged_at": None})
        if not existing:
            await AL.raise_alert(tenant_id, user.get("id"), "low_cash_forecast",
                                 "Low cash projected", detail)
            alert_info["alert_raised"] = True

    def _sum(kind, direction=None):
        return sum(i["amount_cents"] for i in items
                   if i["kind"] == kind
                   and (direction is None or i["direction"] == direction))

    assumptions = [
        "Headline curve is the KNOWN curve: starting cash plus dated, identifiable flows only — no averages are blended in.",
        "Open invoices land on their due date (overdue -> today); open bills are paid on their due date (overdue -> today).",
        "Recurring invoice templates project from their real next_run date; occurrences already materialized are open invoices in AR and are counted once, there.",
        "Recurring bills project from the latest bill of each recurring chain; the chain head, if still open, is counted once in AP.",
        "Payroll is projected from each active schedule's last posted run (labeled 'based on last run'); a schedule with no posted run is called out in notes, never silently zero.",
    ]
    if cfg["run_rate"]:
        assumptions.append(
            "Run-rate band is ON: trailing-90-day average daily CASH movement, excluding invoice receipts, portal receipts, bill payments and payroll postings (the categories already in the known curve), so nothing is counted twice.")
    else:
        assumptions.append(
            "Run-rate band is OFF: the headline is known items only. Turn it on to layer trailing-90-day average daily cash movement (computed net of everything in the known curve).")
    if cfg["expected_pay_dates"]:
        assumptions.append(
            "Expected pay dates are ON: customers with >= 3 paid invoices shift by their historical average days-late (an average of their own history — not an ML prediction).")

    return {
        "start_date": t_iso, "days": days,
        "scenario": ({"id": scenario["id"], "name": scenario.get("name")}
                     if scenario else None),
        "start_cash_cents": start_cash, "end_cash_cents": cash,
        "min_cash_cents": min_cash, "min_cash_date": min_date,
        "avg_daily_inflow_cents": band["daily_inflow_cents"],
        "avg_daily_outflow_cents": band["daily_outflow_cents"],
        "scheduled": {"ar_cents": _sum("ar"), "bills_cents": _sum("ap"),
                      "recurring_inflow_cents": _sum("recurring_invoice"),
                      "recurring_outflow_cents": _sum("recurring_bill"),
                      "payroll_cents": payroll_total, "tax_cents": tax_total,
                      "sales_tax_cents": sales_tax_total,
                      "events_in_cents": _sum("event", "in"),
                      "events_out_cents": _sum("event", "out")},
        "layers": {
            "known_curve": {"item_count": len(items)},
            "run_rate": {"enabled": cfg["run_rate"], **band},
            "payroll": {"enabled": cfg["payroll"], "projected_cents": payroll_total},
            "tax": {"enabled": cfg["tax"], "income_tax_cents": tax_total,
                    "sales_tax_cents": sales_tax_total},
            "expected_pay_dates": {"enabled": cfg["expected_pay_dates"],
                                   "adjustments": adjustments},
        },
        "items": items,
        "weekly": weekly,
        "alerts": alert_info,
        "notes": notes,
        "points": points,
        "assumptions": assumptions,
    }
