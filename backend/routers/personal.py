"""Personal finance layer (slice 1) — household entities, net worth,
day-by-day balance history, cash flow, rollover budgets, recurring bills,
savings goals, and manual assets/debts.

Clean-room: modeled on the behavioral spec in docs/PERSONAL_FINANCE_SPEC.md
(public behavior only); all code here is original to this codebase and rides
its existing machinery — `Scoped` tenant isolation, integer cents, the GL
ledger (`lib.ledger._balances`), the base COA seed, and the existing
`bank_transactions` stream written by CSV import / Plaid / manual entry in
routers/banking.py and routers/bankfeed.py. No new import or categorization
code: budgets read the category the existing pipeline set on each bank
transaction (the human category name is `category_name` when a row carries
one, otherwise `NAME_OF[category_code]` — banking.py stores the GL
`category_code` on transactions and derives the display name exactly that
way for its rules, see routers/banking.py and routers/cat_review.py).

Isolation rule: every endpoint except POST /personal/households is gated —
403 unless the caller's ACTIVE tenant is a household (tenant.kind ==
"household"; tenants created before this layer have no kind field and are
businesses). All personal collections are read/written through `Scoped`,
so one household's rows are invisible to every other tenant.

Permissions ride the existing budget:* pair (budget:read for reads,
budget:write for writes) — household plan/entitlement shaping is a later
slice (see the spec).
"""
from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import db
from lib.base import Scoped, new_id, now_iso, today_str
from lib.auth import require
from lib.coa import ACCOUNT_TYPE_OF, NAME_OF, seed_coa
from lib.memberships import memberships
from lib.niches import DEFAULT_NICHE
from lib import ledger as L

router = APIRouter(prefix="/personal", tags=["personal"])

ASSET_KINDS = ("property", "vehicle", "investment", "other_asset")
DEBT_KINDS = ("loan", "other_debt")
CADENCES = ("weekly", "biweekly", "monthly", "quarterly", "yearly")


# ---------------------------------------------------------------- helpers

def _pub(doc: dict) -> dict:
    """Public view of a stored doc: no mongo id, no tenant stamp."""
    return {k: v for k, v in (doc or {}).items() if k not in ("_id", "tenant_id")}


def _month_period(p: str) -> str:
    """Validate a monthly period: YYYY-MM (personal budgets are monthly-only
    this slice)."""
    if (isinstance(p, str) and len(p) == 7 and p[4] == "-"
            and p[:4].isdigit() and p[5:].isdigit() and 1 <= int(p[5:]) <= 12):
        return p
    raise HTTPException(status_code=400, detail="period must be YYYY-MM")


def _month_range(p: str) -> tuple[str, str]:
    """(inclusive start, exclusive end) YYYY-MM-DD dates covered by YYYY-MM."""
    p = _month_period(p)
    start = date(int(p[:4]), int(p[5:]), 1)
    end = date(start.year + (1 if start.month == 12 else 0),
               1 if start.month == 12 else start.month + 1, 1)
    return start.isoformat(), end.isoformat()


def _shift_period(p: str, months: int) -> str:
    y, m = int(p[:4]), int(p[5:])
    m0 = (m - 1) + months
    return f"{y + m0 // 12:04d}-{m0 % 12 + 1:02d}"


def _months_between(first: str, last: str) -> list[str]:
    """Inclusive list of YYYY-MM periods from first to last (first <= last)."""
    out = []
    cur = first
    while cur <= last:
        out.append(cur)
        cur = _shift_period(cur, 1)
    return out


def _to_date(s: str, field: str = "date") -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD")


def _txn_category(t: dict) -> str:
    """Human category for a bank transaction, as set by the categorize
    pipeline: an explicit category_name wins, else the COA name of the
    transaction's category_code, else the raw code, else Uncategorized."""
    return (t.get("category_name")
            or NAME_OF.get(t.get("category_code") or "")
            or t.get("category_code")
            or "Uncategorized")


async def _require_household(user: dict) -> dict:
    """Gate: the active tenant must be a household. Absent kind = business."""
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    if not tenant or tenant.get("kind") != "household":
        raise HTTPException(
            status_code=403,
            detail="Personal finance is only available in a household workspace")
    return tenant


def _scoped(collection_name: str, user: dict) -> Scoped:
    return Scoped(getattr(db, collection_name), user["tenant_id"])


async def _delete_scoped(collection_name: str, user: dict, doc_id: str, label: str):
    col = _scoped(collection_name, user)
    res = await col.c.delete_one({"tenant_id": user["tenant_id"], "id": doc_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return {"ok": True}


async def _manual_totals(user: dict):
    """(manual asset rows, manual debt rows, assets_cents, debts_cents)."""
    rows = await _scoped("personal_assets", user).find_list(limit=1000)
    assets = [r for r in rows if r.get("kind") in ASSET_KINDS]
    debts = [r for r in rows if r.get("kind") in DEBT_KINDS]
    return (rows, assets, debts,
            sum(int(r.get("value_cents") or 0) for r in assets),
            sum(int(r.get("value_cents") or 0) for r in debts))


# ---------------------------------------------------------------- households

class HouseholdIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)


@router.post("/households")
async def create_household(body: HouseholdIn,
                           user: dict = Depends(require("budget:write"))):
    """Create a household tenant for the current user: tenant doc stamped
    kind="household", base COA seeded (default niche), and an owner
    membership appended to the user's memberships. The caller's active
    tenant does not change — they switch into the household like any other
    workspace."""
    tenant_id = new_id()
    tenant = {
        "id": tenant_id,
        "name": (body.name or "").strip() or "Household",
        "niche_id": DEFAULT_NICHE,
        "kind": "household",
        "plan": None,  # household entitlement shaping is a later slice
        "settings": {},
        "created_at": now_iso(),
    }
    await db.tenants.insert_one(tenant)
    await seed_coa(Scoped(db.accounts, tenant_id), DEFAULT_NICHE)

    fresh = await db.users.find_one({"id": user["id"]}, {"_id": 0}) or user
    ms = memberships(fresh)
    if not any(m.get("tenant_id") == tenant_id for m in ms):
        ms = ms + [{"tenant_id": tenant_id, "role": "owner"}]
        await db.users.update_one({"id": user["id"]},
                                  {"$set": {"memberships": ms}})
    return _pub(tenant)


# ---------------------------------------------------------------- net worth

@router.get("/net-worth")
async def get_net_worth(user: dict = Depends(require("budget:read"))):
    """F1: GL asset/liability balances (posted journal lines, as of today)
    plus manually-tracked assets and debts. Net worth = assets - liabilities,
    integer cents."""
    await _require_household(user)
    as_of = today_str()
    bals = await L._balances(db, user["tenant_id"], as_of=as_of)
    accounts = []
    gl_assets = gl_liabilities = 0
    for code in sorted(bals.keys()):
        atype = ACCOUNT_TYPE_OF.get(code)
        if atype not in ("asset", "liability"):
            continue
        bal = L._net_balance(code, bals[code]["debit"], bals[code]["credit"])
        if atype == "asset":
            gl_assets += bal
        else:
            gl_liabilities += bal
        accounts.append({"code": code, "name": NAME_OF.get(code, code),
                         "type": atype, "balance_cents": bal})
    _, _, _, manual_assets, manual_debts = await _manual_totals(user)
    assets_total = gl_assets + manual_assets
    liabilities_total = gl_liabilities + manual_debts
    return {
        "as_of": as_of,
        "assets_cents": assets_total,
        "liabilities_cents": liabilities_total,
        "net_worth_cents": assets_total - liabilities_total,
        "gl_assets_cents": gl_assets,
        "gl_liabilities_cents": gl_liabilities,
        "manual_assets_cents": manual_assets,
        "manual_liabilities_cents": manual_debts,
        "accounts": accounts,
    }


# ---------------------------------------------------------------- balance history

@router.get("/balances/history")
async def get_balance_history(date_from: Optional[str] = Query(None),
                              date_to: Optional[str] = Query(None),
                              user: dict = Depends(require("budget:read"))):
    """F2: per bank account, replay its bank_transactions (signed cents) on
    top of its stored opening balance/date (personal_account_meta; default
    opening 0 at the earliest transaction date) and emit one closing balance
    per day. The combined series sums the account series and adds manual
    assets - debts held flat at their current values — an honest limit,
    labeled `manual_items_held_flat: true` in the payload."""
    await _require_household(user)
    end = _to_date(date_to, "date_to") if date_to else _to_date(today_str())
    start_floor = _to_date(date_from, "date_from") if date_from else None

    accounts = await _scoped("bank_accounts", user).find_list(sort=[("name", 1)])
    metas = {m["bank_account_id"]: m
             for m in await _scoped("personal_account_meta", user).find_list()}
    _, _, _, manual_assets, manual_debts = await _manual_totals(user)
    manual_net = manual_assets - manual_debts

    out_accounts = []
    per_account_points: list[dict[str, int]] = []
    global_start: Optional[date] = None
    for acct in accounts:
        txns = await _scoped("bank_transactions", user).find_list(
            {"bank_account_id": acct["id"]}, sort=[("txn_date", 1)])
        txns = [t for t in txns if t.get("txn_date")]
        meta = metas.get(acct["id"]) or {}
        opening = int(meta.get("opening_balance_cents") or 0)
        if meta.get("opening_date"):
            start = _to_date(meta["opening_date"], "opening_date")
        elif txns:
            start = _to_date(txns[0]["txn_date"], "txn_date")
        else:
            start = end
        if start_floor and start < start_floor:
            start = start_floor

        by_day: dict[str, int] = {}
        for t in txns:
            by_day[t["txn_date"]] = by_day.get(t["txn_date"], 0) + int(t.get("amount_cents") or 0)

        balance = opening
        # Transactions dated before the series start are folded into the
        # opening balance so the closing totals stay true.
        for dstr, amt in by_day.items():
            if _to_date(dstr) < start:
                balance += amt
        points = []
        cur = start
        while cur <= end:
            balance += by_day.get(cur.isoformat(), 0)
            points.append({"date": cur.isoformat(), "balance_cents": balance})
            cur += timedelta(days=1)
        if not points:
            # Every transaction sits after the requested end date.
            balance = opening + sum(
                amt for dstr, amt in by_day.items() if _to_date(dstr) <= end)
        out_accounts.append({
            "bank_account_id": acct["id"],
            "name": acct.get("name"),
            "kind": acct.get("kind"),
            "gl_code": acct.get("gl_code"),
            "opening_balance_cents": opening,
            "opening_date": meta.get("opening_date") or (txns[0]["txn_date"] if txns else None),
            "points": points,
            "current_balance_cents": points[-1]["balance_cents"] if points else balance,
        })
        per_account_points.append({p["date"]: p["balance_cents"] for p in points})
        if points:
            first = _to_date(points[0]["date"])
            global_start = first if global_start is None else min(global_start, first)

    combined_points = []
    if global_start is not None:
        last = [0] * len(per_account_points)
        cur = global_start
        while cur <= end:
            dstr = cur.isoformat()
            total = 0
            for i, pts in enumerate(per_account_points):
                if dstr in pts:
                    last[i] = pts[dstr]
                total += last[i]
            combined_points.append({
                "date": dstr,
                "accounts_balance_cents": total,
                "net_worth_cents": total + manual_net,
            })
            cur += timedelta(days=1)

    return {
        "date_from": global_start.isoformat() if global_start else end.isoformat(),
        "date_to": end.isoformat(),
        "accounts": out_accounts,
        "combined": {
            "manual_items_held_flat": True,
            "manual_net_cents": manual_net,
            "points": combined_points,
        },
    }


# ---------------------------------------------------------------- cash flow

@router.get("/cash-flow")
async def get_cash_flow(period: str = Query(...),
                        user: dict = Depends(require("budget:read"))):
    """F3: money in / out / net for one month (YYYY-MM) from the household's
    bank_transactions. Transfers between the household's own accounts
    (rows flagged is_transfer) are excluded — moving your own money is
    neither income nor spending."""
    await _require_household(user)
    start, end = _month_range(period)
    txns = await _scoped("bank_transactions", user).find_list(
        {"txn_date": {"$gte": start, "$lt": end}}, limit=10000)
    in_cents = out_cents = count = 0
    for t in txns:
        if t.get("is_transfer"):
            continue
        amt = int(t.get("amount_cents") or 0)
        if amt > 0:
            in_cents += amt
            count += 1
        elif amt < 0:
            out_cents += -amt
            count += 1
    return {"period": period, "in_cents": in_cents, "out_cents": out_cents,
            "net_cents": in_cents - out_cents, "transaction_count": count}


# ---------------------------------------------------------------- budgets (F4)

class PersonalBudgetIn(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    period: str
    amount_cents: int = Field(ge=0)
    rollover: bool = False


@router.post("/budgets")
async def upsert_personal_budget(body: PersonalBudgetIn,
                                 user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    period = _month_period(body.period)
    category = body.category.strip()
    col = _scoped("personal_budgets", user)
    existing = await col.find_one({"category": category, "period": period})
    if existing:
        await col.update_one({"id": existing["id"]}, {"$set": {
            "amount_cents": body.amount_cents, "rollover": bool(body.rollover),
            "updated_at": now_iso()}})
        return _pub({**existing, "amount_cents": body.amount_cents,
                     "rollover": bool(body.rollover)})
    doc = {"id": new_id(), "category": category, "period": period,
           "amount_cents": body.amount_cents, "rollover": bool(body.rollover),
           "created_at": now_iso(), "updated_at": now_iso()}
    await col.insert_one(doc)
    return _pub(doc)


@router.get("/budgets")
async def get_personal_budgets(period: str = Query(...),
                               user: dict = Depends(require("budget:read"))):
    """Per-category budget for YYYY-MM with rollover carry.

    The walk starts at the earliest budget line for the category and moves
    month by month to the requested period: a month's effective budget is
    its set amount plus the leftover carried in — but only when rollover is
    on for that category (the flag of the most recent line at or before
    that month). Leftover = effective - spent, and a negative leftover
    (overspend) carries too. When rollover is off, carried_in is 0.
    Spent is money-out bank_transactions in the category for that month,
    transfers excluded. Categories with spend but no budget line still
    appear with budgeted = 0 — nothing hides."""
    await _require_household(user)
    period = _month_period(period)
    lines = await _scoped("personal_budgets", user).find_list(sort=[("period", 1)])
    by_cat: dict[str, dict[str, dict]] = {}
    for ln in lines:
        by_cat.setdefault(ln["category"], {})[ln["period"]] = ln

    txns = await _scoped("bank_transactions", user).find_list(limit=100000)
    spent_map: dict[tuple[str, str], int] = {}
    for t in txns:
        amt = int(t.get("amount_cents") or 0)
        if amt >= 0 or t.get("is_transfer") or not t.get("txn_date"):
            continue
        key = (_txn_category(t), t["txn_date"][:7])
        spent_map[key] = spent_map.get(key, 0) + (-amt)

    categories = set(by_cat) | {cat for (cat, per) in spent_map if per == period}
    rows = []
    for cat in sorted(categories):
        cat_lines = by_cat.get(cat, {})
        spent_req = spent_map.get((cat, period), 0)
        if not cat_lines or period < min(cat_lines):
            if cat_lines and spent_req == 0 and period not in cat_lines:
                continue  # only future lines and no spend this month
            budgeted = int(cat_lines[period]["amount_cents"]) if period in cat_lines else 0
            rows.append({"category": cat, "budgeted_cents": budgeted,
                         "spent_cents": spent_req,
                         "remaining_cents": budgeted - spent_req,
                         "carried_in_cents": 0,
                         "rollover": bool(cat_lines.get(period, {}).get("rollover"))})
            continue
        carried = 0
        roll = False
        row = None
        for m in _months_between(min(cat_lines), period):
            line = cat_lines.get(m)
            if line is not None:
                roll = bool(line.get("rollover"))
            amount = int(line["amount_cents"]) if line else 0
            carried_in = carried if roll else 0
            effective = amount + carried_in
            spent_m = spent_map.get((cat, m), 0)
            leftover = effective - spent_m
            if m == period:
                row = {"category": cat, "budgeted_cents": effective,
                       "spent_cents": spent_m, "remaining_cents": leftover,
                       "carried_in_cents": carried_in, "rollover": roll}
            carried = leftover
        rows.append(row)
    totals = {
        "budgeted_cents": sum(r["budgeted_cents"] for r in rows),
        "spent_cents": sum(r["spent_cents"] for r in rows),
        "remaining_cents": sum(r["remaining_cents"] for r in rows),
        "carried_in_cents": sum(r["carried_in_cents"] for r in rows),
    }
    return {"period": period, "rows": rows, "totals": totals}


@router.delete("/budgets/{budget_id}")
async def delete_personal_budget(budget_id: str,
                                 user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    return await _delete_scoped("personal_budgets", user, budget_id, "Budget")


# ---------------------------------------------------------------- recurring bills (F5)

class RecurringBillIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, max_length=80)
    amount_cents: int = Field(ge=0)
    cadence: Literal["weekly", "biweekly", "monthly", "quarterly", "yearly"]
    next_due_date: str
    active: bool = True


def _due_state(next_due_date: str, today: str) -> tuple[str, int]:
    days = (_to_date(next_due_date, "next_due_date") - _to_date(today)).days
    if days < 0:
        return "overdue", days
    if days <= 7:
        return "due_soon", days
    return "upcoming", days


@router.post("/recurring-bills")
async def create_recurring_bill(body: RecurringBillIn,
                                user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    _to_date(body.next_due_date, "next_due_date")
    doc = {"id": new_id(), "name": body.name.strip(),
           "category": (body.category or "").strip() or None,
           "amount_cents": body.amount_cents, "cadence": body.cadence,
           "next_due_date": body.next_due_date, "active": bool(body.active),
           "created_at": now_iso(), "updated_at": now_iso()}
    await _scoped("personal_recurring_bills", user).insert_one(doc)
    return _pub(doc)


@router.get("/recurring-bills")
async def list_recurring_bills(user: dict = Depends(require("budget:read"))):
    """Each bill annotated with due_state (overdue | due_soon <= 7 days |
    upcoming) and days_until_due, computed against today."""
    await _require_household(user)
    today = today_str()
    rows = await _scoped("personal_recurring_bills", user).find_list(
        sort=[("next_due_date", 1)])
    out = []
    for r in rows:
        state, days = _due_state(r["next_due_date"], today)
        out.append({**_pub(r), "due_state": state, "days_until_due": days})
    return out


@router.delete("/recurring-bills/{bill_id}")
async def delete_recurring_bill(bill_id: str,
                                user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    return await _delete_scoped("personal_recurring_bills", user, bill_id, "Recurring bill")


# ---------------------------------------------------------------- goals (F6)

class GoalIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_cents: int = Field(gt=0)
    saved_cents: int = Field(default=0, ge=0)
    target_date: Optional[str] = None


@router.post("/goals")
async def create_goal(body: GoalIn, user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    if body.target_date:
        _to_date(body.target_date, "target_date")
    doc = {"id": new_id(), "name": body.name.strip(),
           "target_cents": body.target_cents, "saved_cents": body.saved_cents,
           "target_date": body.target_date,
           "created_at": now_iso(), "updated_at": now_iso()}
    await _scoped("personal_goals", user).insert_one(doc)
    return _pub(doc)


@router.get("/goals")
async def list_goals(user: dict = Depends(require("budget:read"))):
    await _require_household(user)
    rows = await _scoped("personal_goals", user).find_list(sort=[("created_at", 1)])
    out = []
    for r in rows:
        target = int(r.get("target_cents") or 0)
        saved = int(r.get("saved_cents") or 0)
        out.append({**_pub(r),
                    "progress_pct": round(saved / target * 100, 1) if target else 0.0,
                    "remaining_cents": max(target - saved, 0)})
    return out


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str, user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    return await _delete_scoped("personal_goals", user, goal_id, "Goal")


# ---------------------------------------------------------------- manual assets/debts (feeds F1)

class AssetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["property", "vehicle", "investment", "other_asset", "loan", "other_debt"]
    value_cents: int = Field(ge=0)
    as_of_date: Optional[str] = None


@router.post("/assets")
async def create_asset(body: AssetIn, user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    as_of = body.as_of_date or today_str()
    _to_date(as_of, "as_of_date")
    doc = {"id": new_id(), "name": body.name.strip(), "kind": body.kind,
           "value_cents": body.value_cents, "as_of_date": as_of,
           "created_at": now_iso(), "updated_at": now_iso()}
    await _scoped("personal_assets", user).insert_one(doc)
    return _pub(doc)


@router.get("/assets")
async def list_assets(user: dict = Depends(require("budget:read"))):
    await _require_household(user)
    rows = await _scoped("personal_assets", user).find_list(sort=[("name", 1)])
    return [_pub(r) for r in rows]


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str, user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    return await _delete_scoped("personal_assets", user, asset_id, "Asset")


# ---------------------------------------------------------------- account opening meta (feeds F2)

class OpeningIn(BaseModel):
    opening_balance_cents: int
    opening_date: str


@router.post("/accounts/{bank_account_id}/opening")
async def set_account_opening(bank_account_id: str, body: OpeningIn,
                              user: dict = Depends(require("budget:write"))):
    """Set the opening balance/date the balance-history replay starts from
    for one of the household's bank accounts. 404 when the bank account is
    not in this tenant."""
    await _require_household(user)
    _to_date(body.opening_date, "opening_date")
    acct = await _scoped("bank_accounts", user).find_one({"id": bank_account_id})
    if not acct:
        raise HTTPException(status_code=404, detail="Bank account not found")
    col = _scoped("personal_account_meta", user)
    existing = await col.find_one({"bank_account_id": bank_account_id})
    if existing:
        await col.update_one({"id": existing["id"]}, {"$set": {
            "opening_balance_cents": body.opening_balance_cents,
            "opening_date": body.opening_date, "updated_at": now_iso()}})
        return _pub({**existing,
                     "opening_balance_cents": body.opening_balance_cents,
                     "opening_date": body.opening_date})
    doc = {"id": new_id(), "bank_account_id": bank_account_id,
           "opening_balance_cents": body.opening_balance_cents,
           "opening_date": body.opening_date,
           "created_at": now_iso(), "updated_at": now_iso()}
    await col.insert_one(doc)
    return _pub(doc)
