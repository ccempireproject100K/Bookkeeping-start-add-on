"""Monthly category budgets + cash-flow forecast.

Budgets: per-category, per-month (collection `monthly_budgets` — separate from the
annual account-code budgets in routers/reports.py). Variance = budgeted vs actual
direct expenses (db.expenses, by expense_date) in the period. Categories with spend
but no budget show up with budgeted = 0 so nothing hides.

Forecast: projected cash over the next 30/60/90 days =
  starting cash (accounts 1010 + 1020 + 1050)
  + trailing-90-day average daily inflow/outflow from the journal as the baseline
  + scheduled items layered on top: open invoice balances (AR), open bills (AP),
    recurring expense templates, recurring invoice templates.
Directional, not exact — assumptions are returned with the payload and shown in
the UI.

NOTE: "budget:read" / "budget:write" are NEW permissions — they must be added
to the PERMISSIONS map in backend/lib/auth.py (see MANIFEST.md). Suggested:
  "budget:read": {"owner", "accountant", "bookkeeper"},
  "budget:write": {"owner"},
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require, get_current_user
from lib import ledger as L

router = APIRouter(prefix="/budget", tags=["budget"])

CASH_ACCOUNTS = ["1010", "1020", "1050"]  # register + bank checking + stripe clearing


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

@router.get("/forecast")
async def forecast(days: int = Query(90, ge=7, le=365), user: dict = Depends(require("budget:read"))):
    tenant_id = user["tenant_id"]
    today = date.fromisoformat(today_str())
    end = today + timedelta(days=days)
    t_iso, e_iso = today.isoformat(), end.isoformat()

    # starting cash: register + bank checking + stripe clearing
    bals = await L._balances(db, tenant_id, as_of=t_iso)
    start_cash = sum(L._net_balance(c, bals.get(c, {"debit": 0, "credit": 0})["debit"],
                                   bals.get(c, {"debit": 0, "credit": 0})["credit"])
                     for c in CASH_ACCOUNTS)

    # baseline: trailing-90-day average daily inflow/outflow from the journal
    win_start = (today - timedelta(days=90)).isoformat()
    pnl = await L.pnl(db, tenant_id, win_start, t_iso)
    avg_in = pnl["revenue_total"] // 90
    avg_out = pnl["expense_total"] // 90

    sched_in: dict[str, int] = {}
    sched_out: dict[str, int] = {}

    def add(bucket: dict[str, int], when: str, amt: int):
        when = max(when, t_iso)
        if when <= e_iso and amt > 0:
            bucket[when] = bucket.get(when, 0) + amt

    # AR: open invoices land on their due date (overdue -> today)
    ar_total = 0
    async for inv in Scoped(db.invoices, tenant_id).find(
            {"status": {"$in": ["issued", "partial"]}},
            {"_id": 0, "due_date": 1, "total_cents": 1, "paid_cents": 1}):
        bal = (inv.get("total_cents") or 0) - (inv.get("paid_cents") or 0)
        if bal > 0:
            ar_total += bal
            add(sched_in, inv.get("due_date") or t_iso, bal)

    # AP: open bills go out on their due date (overdue -> today)
    bills_total = 0
    async for b in Scoped(db.bills, tenant_id).find(
            {"status": {"$in": ["open", "partial"]}},
            {"_id": 0, "due_date": 1, "amount_cents": 1, "paid_cents": 1}):
        bal = (b.get("amount_cents") or 0) - (b.get("paid_cents") or 0)
        if bal > 0:
            bills_total += bal
            add(sched_out, b.get("due_date") or t_iso, bal)

    # recurring expense templates (amount stored in dollars in this collection)
    rec_out = 0
    async for r in Scoped(db.recurring, tenant_id).find(
            {"active": True}, {"_id": 0, "day_of_month": 1, "amount": 1}):
        amt = to_cents(r.get("amount") or 0)
        for n in range(0, 13):
            d = _add_months(today, n)
            when = date(d.year, d.month, min(int(r.get("day_of_month") or 1), 28)).isoformat()
            if t_iso <= when <= e_iso:
                rec_out += amt
                add(sched_out, when, amt)

    # recurring invoice templates (lines in dollars; monthly on the 1st, weekly every 7d)
    rec_in = 0
    async for r in Scoped(db.recurring_invoices, tenant_id).find(
            {}, {"_id": 0, "frequency": 1, "lines": 1, "tax": 1}):
        total = sum(to_cents(l.get("amount") or 0) for l in r.get("lines", [])) \
            + to_cents(r.get("tax") or 0)
        freq = (r.get("frequency") or "monthly").lower()
        dates = []
        if freq == "weekly":
            d = today
            while d <= end:
                dates.append(d.isoformat())
                d += timedelta(days=7)
        elif freq == "quarterly":
            dates = [_add_months(today, n).replace(day=1).isoformat() for n in (0, 3, 6, 9)]
        else:  # monthly
            dates = [_add_months(today, n).replace(day=1).isoformat() for n in range(0, 13)]
        for when in dates:
            if t_iso <= when <= e_iso:
                rec_in += total
                add(sched_in, when, total)

    points = []
    cash = start_cash
    min_cash, min_date = cash, t_iso
    d = today
    while d <= end:
        key = d.isoformat()
        i, o = avg_in + sched_in.get(key, 0), avg_out + sched_out.get(key, 0)
        cash += i - o
        if cash < min_cash:
            min_cash, min_date = cash, key
        points.append({"date": key, "inflow_cents": i, "outflow_cents": o, "cash_cents": cash})
        d += timedelta(days=1)

    return {
        "start_date": t_iso, "days": days,
        "start_cash_cents": start_cash, "end_cash_cents": cash,
        "min_cash_cents": min_cash, "min_cash_date": min_date,
        "avg_daily_inflow_cents": avg_in, "avg_daily_outflow_cents": avg_out,
        "scheduled": {"ar_cents": ar_total, "bills_cents": bills_total,
                      "recurring_inflow_cents": rec_in, "recurring_outflow_cents": rec_out},
        "points": points,
        "assumptions": [
            "Baseline inflow/outflow = trailing 90-day daily averages from the journal.",
            "Open invoices assumed collected on their due date (overdue -> today).",
            "Open bills assumed paid on their due date (overdue -> today).",
            "Recurring expense templates land on their day-of-month; recurring invoices on the 1st (monthly) or every 7 days (weekly).",
            "Scheduled items layer on top of the baseline — some overlap is expected; treat as directional.",
        ],
    }
