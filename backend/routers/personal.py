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
    tenant = await _require_household(user)
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
    # Slice 2: manual items are converted into the household base currency
    # when they carry an fx_rate_micro; foreign items without a rate are
    # counted at face value and flagged (fx_pending) — never silently mixed.
    base_currency = ((tenant.get("settings") or {}).get("base_currency")
                     or "USD").upper()
    manual_rows, _, _, _, _ = await _manual_totals(user)
    manual_items = []
    manual_assets = manual_debts = 0
    fx_pending = 0
    for r in manual_rows:
        cur = (r.get("currency") or base_currency).upper()
        value = int(r.get("value_cents") or 0)
        rate = r.get("fx_rate_micro")
        if cur != base_currency and rate:
            converted = round(value * int(rate) / 1_000_000)
            fx_applied = True
        else:
            converted = value
            fx_applied = cur == base_currency
            if cur != base_currency:
                fx_pending += 1
        if r.get("kind") in ASSET_KINDS:
            manual_assets += converted
        elif r.get("kind") in DEBT_KINDS:
            manual_debts += converted
        manual_items.append({**_pub(r), "currency": cur,
                             "converted_cents": converted,
                             "fx_applied": fx_applied})
    assets_total = gl_assets + manual_assets
    liabilities_total = gl_liabilities + manual_debts
    return {
        "as_of": as_of,
        "base_currency": base_currency,
        "assets_cents": assets_total,
        "liabilities_cents": liabilities_total,
        "net_worth_cents": assets_total - liabilities_total,
        "gl_assets_cents": gl_assets,
        "gl_liabilities_cents": gl_liabilities,
        "manual_assets_cents": manual_assets,
        "manual_liabilities_cents": manual_debts,
        "fx_pending_count": fx_pending,
        "manual_items": manual_items,
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
            "currency": meta.get("currency"),
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
    # Slice 2: "detected" when the customer confirmed an auto-detection
    # suggestion; "manual" otherwise.
    source: Literal["manual", "detected"] = "manual"


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
           "source": body.source,
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
    # Slice 2: optional self-funding plan — a scheduled allocation recorded
    # against the goal (see run_goal_funding_for_user). The money itself
    # stays in the funding account; the plan advances saved_cents on cadence.
    funding_account_id: Optional[str] = None
    # Slice 2 (completion pass): when BOTH a funding (source) account and
    # a destination account are named, each funded period also writes a
    # real transfer pair into bank_transactions — money moves from the
    # source household account to the destination household account,
    # flagged is_transfer so cash flow and budgets ignore it, exactly
    # like any other move between the household's own accounts. With no
    # destination named, funding stays allocation-only (the money never
    # leaves the funding account; saved_cents simply advances).
    funding_destination_account_id: Optional[str] = None
    funding_amount_cents: Optional[int] = Field(default=None, gt=0)
    funding_cadence: Optional[Literal["weekly", "biweekly", "monthly"]] = None
    funding_start_date: Optional[str] = None
    auto_fund: bool = False
    # Slice 3: a locked goal's savings count as a vault lock in the
    # safe-to-spend view — the money is spoken for and hands off.
    locked: bool = False


def _goal_funding_view(goal: dict) -> dict:
    """Public funding-plan fields for a goal doc (slice 2)."""
    return {
        "funding_account_id": goal.get("funding_account_id"),
        "funding_destination_account_id": goal.get(
            "funding_destination_account_id"),
        "funding_amount_cents": goal.get("funding_amount_cents"),
        "funding_cadence": goal.get("funding_cadence"),
        "funding_start_date": goal.get("funding_start_date"),
        "auto_fund": bool(goal.get("auto_fund")),
        "last_funded_date": goal.get("last_funded_date"),
    }


@router.post("/goals")
async def create_goal(body: GoalIn, user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    if body.target_date:
        _to_date(body.target_date, "target_date")
    if body.auto_fund:
        if not body.funding_amount_cents or not body.funding_cadence:
            raise HTTPException(
                status_code=400,
                detail="auto_fund needs funding_amount_cents and funding_cadence")
        if body.funding_start_date:
            _to_date(body.funding_start_date, "funding_start_date")
    # Named funding accounts must be this household's own bank accounts —
    # a goal can never move money into or out of anyone else's account.
    for field, acct_id in (("funding_account_id", body.funding_account_id),
                           ("funding_destination_account_id",
                            body.funding_destination_account_id)):
        if acct_id:
            acct = await _scoped("bank_accounts", user).find_one(
                {"id": acct_id})
            if not acct:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field} is not one of this household's accounts")
    if (body.funding_account_id and body.funding_destination_account_id
            and body.funding_account_id == body.funding_destination_account_id):
        raise HTTPException(
            status_code=400,
            detail="funding source and destination accounts must differ")
    doc = {"id": new_id(), "name": body.name.strip(),
           "target_cents": body.target_cents, "saved_cents": body.saved_cents,
           "target_date": body.target_date,
           "funding_account_id": body.funding_account_id,
           "funding_destination_account_id":
               body.funding_destination_account_id,
           "funding_amount_cents": body.funding_amount_cents,
           "funding_cadence": body.funding_cadence,
           "funding_start_date": body.funding_start_date,
           "auto_fund": bool(body.auto_fund),
           "locked": bool(body.locked),
           "last_funded_date": None,
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
        out.append({**_pub(r), **_goal_funding_view(r),
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
    # Slice 2: multi-currency modeling. currency is the item's own ISO 4217
    # code (default = the household base currency). fx_rate_micro, when set,
    # is the rate to the base currency x 1_000_000 (integer, no floats in
    # storage); it can be stamped from the house FX table via
    # POST /assets/{id}/fx-refresh.
    currency: Optional[str] = None  # validated in the endpoint (clean 400)
    fx_rate_micro: Optional[int] = Field(default=None, gt=0)
    # Slice 2: loan terms (kind == "loan") for the amortization schedule.
    principal_cents: Optional[int] = Field(default=None, ge=0)
    apr_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    term_months: Optional[int] = Field(default=None, gt=0, le=600)
    payment_cents: Optional[int] = Field(default=None, gt=0)
    start_date: Optional[str] = None


@router.post("/assets")
async def create_asset(body: AssetIn, user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    as_of = body.as_of_date or today_str()
    _to_date(as_of, "as_of_date")
    if body.start_date:
        _to_date(body.start_date, "start_date")
    currency = (body.currency or "").strip().upper() or None
    if currency and (len(currency) != 3 or not currency.isalpha()):
        raise HTTPException(status_code=400, detail="currency must be ISO 4217 (3 letters)")
    doc = {"id": new_id(), "name": body.name.strip(), "kind": body.kind,
           "value_cents": body.value_cents, "as_of_date": as_of,
           "currency": currency, "fx_rate_micro": body.fx_rate_micro,
           "principal_cents": body.principal_cents, "apr_bps": body.apr_bps,
           "term_months": body.term_months, "payment_cents": body.payment_cents,
           "start_date": body.start_date,
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
    # Slice 2: the account's own currency (ISO 4217); defaults to the
    # household base currency when unset. Display/modeling only — the GL
    # ledger itself stays in the base currency. (Validated in the endpoint
    # so a bad code returns a clean 400.)
    currency: Optional[str] = None


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
    currency = (body.currency or "").strip().upper() or None
    if currency and (len(currency) != 3 or not currency.isalpha()):
        raise HTTPException(status_code=400, detail="currency must be ISO 4217 (3 letters)")
    col = _scoped("personal_account_meta", user)
    existing = await col.find_one({"bank_account_id": bank_account_id})
    if existing:
        patch = {"opening_balance_cents": body.opening_balance_cents,
                 "opening_date": body.opening_date, "updated_at": now_iso()}
        if currency:
            patch["currency"] = currency
        await col.update_one({"id": existing["id"]}, {"$set": patch})
        return _pub({**existing, **patch})
    doc = {"id": new_id(), "bank_account_id": bank_account_id,
           "opening_balance_cents": body.opening_balance_cents,
           "opening_date": body.opening_date, "currency": currency,
           "created_at": now_iso(), "updated_at": now_iso()}
    await col.insert_one(doc)
    return _pub(doc)


# ================================================================ slice 2
# Household UI support (summary + settings), recurring-bill auto-detection
# (suggest-and-confirm), goal self-funding, loan amortization schedules,
# and an insights surface — all computed from the collections slice 1
# already maintains. Clean-room: behavior modeled on the public feature
# shapes of Sure / Firefly III / Actual Budget (see
# references/actual-budget/REFERENCE.md); all code is original.

import re
from statistics import median


def _advance_date(d: date, cadence: str) -> date:
    """Next occurrence of a cadence after date d (day clamped on month steps)."""
    if cadence == "weekly":
        return d + timedelta(days=7)
    if cadence == "biweekly":
        return d + timedelta(days=14)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}.get(cadence)
    if months is None:
        raise HTTPException(status_code=400, detail=f"unknown cadence {cadence}")
    m0 = (d.month - 1) + months
    y, m = d.year + m0 // 12, m0 % 12 + 1
    # clamp the day to the target month's length
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    last_day = (nxt - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))


def _merchant_key(description: str) -> str:
    """Normalize a transaction description into a merchant key: uppercase,
    punctuation/digits stripped, first four words. 'NETFLIX.COM 866-579-7172'
    and 'Netflix.com' both land on 'NETFLIX COM'."""
    words = re.sub(r"[^A-Za-z ]+", " ", (description or "").upper()).split()
    return " ".join(words[:4]) or "UNKNOWN"


def _classify_cadence(median_days: float) -> Optional[str]:
    for name, lo, hi in (("weekly", 5, 9), ("biweekly", 12, 16),
                         ("monthly", 25, 35), ("quarterly", 80, 100),
                         ("yearly", 340, 380)):
        if lo <= median_days <= hi:
            return name
    return None


# ---------------------------------------------------------------- household settings

class HouseholdSettingsIn(BaseModel):
    base_currency: str = Field(min_length=3, max_length=3)
    # Slice 3: the household's payday rhythm. The safe-to-spend view
    # reserves bills due before the next payday; it derives that date
    # from the last payday + cadence. Both optional — when unset, the
    # view falls back to a labeled 14-day estimate.
    last_payday_date: Optional[str] = None
    payday_cadence: Optional[Literal["weekly", "biweekly", "semimonthly",
                                     "monthly"]] = None


@router.put("/settings")
async def update_household_settings(body: HouseholdSettingsIn,
                                    user: dict = Depends(require("budget:write"))):
    """Set the household's base currency (ISO 4217). Net worth is reported
    in this currency; foreign manual items convert via their fx_rate_micro.
    Payday fields ride along when provided (slice 3)."""
    tenant = await _require_household(user)
    cur = body.base_currency.strip().upper()
    if not cur.isalpha():
        raise HTTPException(status_code=400, detail="base_currency must be ISO 4217")
    if body.last_payday_date:
        _to_date(body.last_payday_date, "last_payday_date")
    settings = dict(tenant.get("settings") or {})
    settings["base_currency"] = cur
    if body.last_payday_date is not None:
        settings["last_payday_date"] = body.last_payday_date
    if body.payday_cadence is not None:
        settings["payday_cadence"] = body.payday_cadence
    await db.tenants.update_one({"id": user["tenant_id"]},
                                {"$set": {"settings": settings}})
    return {"base_currency": cur,
            "last_payday_date": settings.get("last_payday_date"),
            "payday_cadence": settings.get("payday_cadence")}


# ---------------------------------------------------------------- recurring detection (F5, slice 2)

@router.get("/recurring-bills/detect")
async def detect_recurring_bills(user: dict = Depends(require("budget:read"))):
    """Scan the household's money-out bank transactions for recurring
    charges: same merchant + same amount, >= 3 occurrences, with intervals
    clustering on a known cadence. Returns SUGGESTIONS ONLY — nothing is
    written; the customer confirms each one via POST /recurring-bills
    (which stamps source='detected' when the confirm payload says so).
    Groups already covered by an active bill (same merchant key, or same
    amount + cadence) are excluded."""
    await _require_household(user)
    txns = await _scoped("bank_transactions", user).find_list(limit=100000)
    groups: dict[tuple[str, int], list[dict]] = {}
    for t in txns:
        amt = int(t.get("amount_cents") or 0)
        if amt >= 0 or t.get("is_transfer") or not t.get("txn_date"):
            continue
        key = (_merchant_key(t.get("description") or t.get("payee") or ""), amt)
        groups.setdefault(key, []).append(t)

    bills = await _scoped("personal_recurring_bills", user).find_list()
    covered_keys = {_merchant_key(b.get("name") or "")
                    for b in bills if b.get("active", True)}
    covered_pairs = {(int(b.get("amount_cents") or 0), b.get("cadence"))
                     for b in bills if b.get("active", True)}

    suggestions = []
    for (mkey, amt), rows in groups.items():
        if len(rows) < 3 or mkey in covered_keys:
            continue
        dates = sorted(_to_date(r["txn_date"]) for r in rows)
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        med = median(intervals)
        cadence = _classify_cadence(med)
        if cadence is None:
            continue
        tol = max(3.0, med * 0.4)
        if any(abs(iv - med) > tol for iv in intervals):
            continue  # irregular — not a bill, just a habit
        if (abs(amt), cadence) in covered_pairs:
            continue
        last = dates[-1]
        latest = max(rows, key=lambda r: r["txn_date"])
        confidence = 0.6 if len(rows) == 3 else 0.75 if len(rows) == 4 else 0.9
        suggestions.append({
            "merchant_key": mkey,
            "suggested_name": (latest.get("description") or mkey.title()).strip()[:120],
            "category": _txn_category(latest),
            "amount_cents": abs(amt),
            "cadence": cadence,
            "occurrences": len(rows),
            "first_date": dates[0].isoformat(),
            "last_date": last.isoformat(),
            "suggested_next_due_date": _advance_date(last, cadence).isoformat(),
            "confidence": confidence,
        })
    suggestions.sort(key=lambda s: (-s["confidence"], -s["occurrences"]))
    return {"suggestions": suggestions, "scanned_transactions": len(txns)}


# ---------------------------------------------------------------- goal self-funding (F6, slice 2)

async def _fund_goal(user: dict, goal: dict, today: date) -> dict:
    """Apply every due, not-yet-funded period of one goal's funding plan.
    Idempotent: each funded period is recorded in personal_goal_funding_events
    keyed by (goal_id, for_date), and a period is never funded twice.
    Funding advances saved_cents. When the goal names BOTH a source
    (funding) account and a destination account, each funded period ALSO
    writes a transfer pair into bank_transactions (out of the source,
    into the destination, is_transfer=True, one shared transfer_group_id
    = the funding event id), so the balance-history replay moves the
    money between the household's own accounts while cash flow and
    budgets ignore the move. Without a destination, funding is an
    allocation only — no money moves between accounts."""
    amount = int(goal.get("funding_amount_cents") or 0)
    cadence = goal.get("funding_cadence")
    start_s = goal.get("funding_start_date") or goal.get("created_at", "")[:10]
    if not amount or not cadence or not start_s:
        return {"goal_id": goal["id"], "funded_events": 0, "added_cents": 0}
    events_col = _scoped("personal_goal_funding_events", user)
    done = {e["for_date"] for e in await events_col.find_list(
        {"goal_id": goal["id"]}, limit=10000)}
    target = int(goal.get("target_cents") or 0)
    saved = int(goal.get("saved_cents") or 0)
    funded = added = 0
    last_funded = goal.get("last_funded_date")
    d = _to_date(start_s, "funding_start_date")
    while d <= today:
        dstr = d.isoformat()
        if dstr not in done and saved < target:
            step = min(amount, target - saved)
            event_id = new_id()
            await events_col.insert_one({
                "id": event_id, "goal_id": goal["id"], "for_date": dstr,
                "amount_cents": step,
                "account_id": goal.get("funding_account_id"),
                "created_at": now_iso()})
            src = goal.get("funding_account_id")
            dst = goal.get("funding_destination_account_id")
            if src and dst and src != dst:
                txns = _scoped("bank_transactions", user)
                desc = f"Goal funding: {goal.get('name') or 'Savings goal'}"
                await txns.insert_one({
                    "id": new_id(), "bank_account_id": src,
                    "txn_date": dstr, "amount_cents": -step,
                    "description": desc, "is_transfer": True,
                    "transfer_group_id": event_id, "goal_id": goal["id"],
                    "source": "goal_funding", "created_at": now_iso()})
                await txns.insert_one({
                    "id": new_id(), "bank_account_id": dst,
                    "txn_date": dstr, "amount_cents": step,
                    "description": desc, "is_transfer": True,
                    "transfer_group_id": event_id, "goal_id": goal["id"],
                    "source": "goal_funding", "created_at": now_iso()})
            saved += step
            funded += 1
            added += step
            last_funded = dstr
        d = _advance_date(d, cadence)
    if funded:
        await _scoped("personal_goals", user).update_one(
            {"id": goal["id"]}, {"$set": {
                "saved_cents": saved, "last_funded_date": last_funded,
                "updated_at": now_iso()}})
    return {"goal_id": goal["id"], "funded_events": funded,
            "added_cents": added, "saved_cents": saved}


async def run_goal_funding_for_user(user: dict) -> dict:
    """Catch up self-funding for every auto_fund goal in the caller's
    household. Same code path as POST /goals/fund-due; the app scheduler
    calls this per household tenant (see lib/scheduler.py). Safe to re-run."""
    await _require_household(user)
    today = _to_date(today_str())
    goals = await _scoped("personal_goals", user).find_list(limit=1000)
    results = []
    for g in goals:
        if g.get("auto_fund"):
            results.append(await _fund_goal(user, g, today))
    return {"goals_checked": len(results),
            "funded_events": sum(r["funded_events"] for r in results),
            "added_cents": sum(r["added_cents"] for r in results),
            "results": results}


@router.post("/goals/fund-due")
async def fund_due_goals(user: dict = Depends(require("budget:write"))):
    return await run_goal_funding_for_user(user)


@router.post("/goals/{goal_id}/fund-due")
async def fund_due_goal(goal_id: str, user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    goal = await _scoped("personal_goals", user).find_one({"id": goal_id})
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    if not goal.get("auto_fund"):
        raise HTTPException(status_code=400, detail="Goal has no auto-funding plan")
    return await _fund_goal(user, goal, _to_date(today_str()))


@router.get("/goals/{goal_id}/funding-events")
async def list_goal_funding_events(goal_id: str,
                                   user: dict = Depends(require("budget:read"))):
    await _require_household(user)
    goal = await _scoped("personal_goals", user).find_one({"id": goal_id})
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    rows = await _scoped("personal_goal_funding_events", user).find_list(
        {"goal_id": goal_id}, sort=[("for_date", 1)], limit=10000)
    return [_pub(r) for r in rows]


# ---------------------------------------------------------------- loan amortization (slice 2)

def _amortization(principal_cents: int, apr_bps: int, term_months: int,
                  start: date, payment_override: Optional[int] = None) -> dict:
    """Standard fixed-rate amortization, integer cents. Monthly rate =
    apr_bps / 10000 / 12. The final payment absorbs rounding so the
    balance lands exactly on zero."""
    r = (apr_bps / 10000.0) / 12.0
    if payment_override:
        pmt = int(payment_override)
    elif r > 0:
        pmt = round(principal_cents * r / (1 - (1 + r) ** -term_months))
    else:
        pmt = round(principal_cents / term_months)
    rows = []
    balance = int(principal_cents)
    total_interest = 0
    d = start
    for i in range(1, term_months + 1):
        d = _advance_date(d, "monthly")
        interest = round(balance * r)
        principal_part = pmt - interest
        if i == term_months or principal_part >= balance:
            principal_part = balance
        pay = principal_part + interest
        balance -= principal_part
        total_interest += interest
        rows.append({"month": i, "date": d.isoformat(), "payment_cents": pay,
                     "principal_cents": principal_part,
                     "interest_cents": interest, "balance_cents": balance})
        if balance <= 0:
            break
    return {"payment_cents": pmt, "months": len(rows),
            "total_interest_cents": total_interest,
            "total_paid_cents": int(principal_cents) + total_interest,
            "payoff_date": rows[-1]["date"] if rows else start.isoformat(),
            "rows": rows}


@router.get("/assets/{asset_id}/schedule")
async def get_loan_schedule(asset_id: str,
                            user: dict = Depends(require("budget:read"))):
    """Amortization schedule for a manual loan carrying terms
    (principal_cents, apr_bps, term_months). 400 when terms are missing —
    the schedule is computed, never stored, so it is always current."""
    await _require_household(user)
    asset = await _scoped("personal_assets", user).find_one({"id": asset_id})
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.get("kind") != "loan":
        raise HTTPException(status_code=400, detail="Schedules are for loans")
    principal = asset.get("principal_cents")
    if principal is None:
        principal = asset.get("value_cents")
    if principal is None or asset.get("term_months") is None:
        raise HTTPException(
            status_code=400,
            detail="Loan needs principal_cents and term_months for a schedule")
    start = _to_date(asset.get("start_date") or asset.get("as_of_date")
                     or today_str(), "start_date")
    sched = _amortization(int(principal), int(asset.get("apr_bps") or 0),
                          int(asset["term_months"]), start,
                          asset.get("payment_cents"))
    return {"asset_id": asset_id, "name": asset.get("name"),
            "principal_cents": int(principal),
            "apr_bps": int(asset.get("apr_bps") or 0), **sched}


@router.post("/assets/{asset_id}/fx-refresh")
async def refresh_asset_fx(asset_id: str,
                           user: dict = Depends(require("budget:write"))):
    """Stamp a manual item's fx_rate_micro from the house FX table
    (lib.fx, ECB daily rates) so net worth can convert it into the
    household base currency. No-op (fx_applied: false) when the item is
    already in the base currency or no rate is on file."""
    tenant = await _require_household(user)
    asset = await _scoped("personal_assets", user).find_one({"id": asset_id})
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    base = ((tenant.get("settings") or {}).get("base_currency") or "USD").upper()
    cur = (asset.get("currency") or base).upper()
    if cur == base:
        return {"asset_id": asset_id, "fx_applied": False,
                "reason": "already in base currency", "base_currency": base}
    from lib import fx as FX
    rate = await FX.get_rate(cur, base, today_str(), db=db)
    if not rate:
        return {"asset_id": asset_id, "fx_applied": False,
                "reason": "no FX rate on file", "base_currency": base}
    micro = round(float(rate) * 1_000_000)
    await _scoped("personal_assets", user).update_one(
        {"id": asset_id}, {"$set": {"fx_rate_micro": micro,
                                    "updated_at": now_iso()}})
    return {"asset_id": asset_id, "fx_applied": True, "currency": cur,
            "base_currency": base, "fx_rate_micro": micro}


# ---------------------------------------------------------------- insights (slice 2)

@router.get("/insights")
async def get_insights(period: str = Query(...),
                       user: dict = Depends(require("budget:read"))):
    """Insights computed from data the layer already maintains: cash-flow
    vs last month, top spending categories, budget pressure, bills due,
    goal progress, largest expense, savings rate, and net-worth change
    over the trailing 90 days. Each insight is a display-ready card."""
    await _require_household(user)
    period = _month_period(period)
    prev = _shift_period(period, -1)
    cf = await get_cash_flow(period, user)
    cf_prev = await get_cash_flow(prev, user)
    budgets = await get_personal_budgets(period, user)
    bills = await list_recurring_bills(user)
    goals = await list_goals(user)

    start, end = _month_range(period)
    txns = await _scoped("bank_transactions", user).find_list(
        {"txn_date": {"$gte": start, "$lt": end}}, limit=10000)
    cat_spend: dict[str, int] = {}
    largest = None
    for t in txns:
        amt = int(t.get("amount_cents") or 0)
        if amt >= 0 or t.get("is_transfer"):
            continue
        cat_spend[_txn_category(t)] = cat_spend.get(_txn_category(t), 0) + (-amt)
        if largest is None or amt < int(largest.get("amount_cents") or 0):
            largest = t
    top_categories = sorted(
        ({"category": c, "spent_cents": v} for c, v in cat_spend.items()),
        key=lambda r: -r["spent_cents"])[:5]
    total_out = cf["out_cents"]
    for row in top_categories:
        row["share_pct"] = round(row["spent_cents"] / total_out * 100, 1) if total_out else 0.0

    cards = []
    if cf_prev["out_cents"]:
        delta = cf["out_cents"] - cf_prev["out_cents"]
        cards.append({
            "kind": "spending_vs_last_month",
            "title": "Spending vs last month",
            "detail": (f"${cf['out_cents']/100:,.2f} out this month vs "
                       f"${cf_prev['out_cents']/100:,.2f} in {prev} "
                       f"({'up' if delta >= 0 else 'down'} ${abs(delta)/100:,.2f})."),
            "tone": "warn" if delta > 0 else "good"})
    if total_out and cf["in_cents"]:
        rate = round(cf["net_cents"] / cf["in_cents"] * 100, 1)
        cards.append({
            "kind": "savings_rate", "title": "Savings rate",
            "detail": f"You kept {rate}% of money in this month.",
            "tone": "good" if rate >= 0 else "warn"})
    for row in budgets["rows"]:
        if row["budgeted_cents"] > 0 and row["spent_cents"] >= row["budgeted_cents"]:
            cards.append({
                "kind": "budget_over", "title": f"{row['category']} over budget",
                "detail": (f"Spent ${row['spent_cents']/100:,.2f} of "
                           f"${row['budgeted_cents']/100:,.2f} budgeted."),
                "tone": "warn"})
        elif row["budgeted_cents"] > 0 and row["spent_cents"] >= 0.8 * row["budgeted_cents"]:
            cards.append({
                "kind": "budget_pressure", "title": f"{row['category']} nearing budget",
                "detail": (f"Spent ${row['spent_cents']/100:,.2f} of "
                           f"${row['budgeted_cents']/100:,.2f} budgeted."),
                "tone": "info"})
    overdue = [b for b in bills if b["due_state"] == "overdue"]
    due_soon = [b for b in bills if b["due_state"] == "due_soon"]
    if overdue:
        cards.append({"kind": "bills_overdue", "title": "Bills overdue",
                      "detail": f"{len(overdue)} bill(s) past due: "
                                + ", ".join(b["name"] for b in overdue[:3]) + ".",
                      "tone": "warn"})
    if due_soon:
        cards.append({"kind": "bills_due_soon", "title": "Bills due this week",
                      "detail": f"{len(due_soon)} bill(s) due within 7 days, "
                                f"totaling ${sum(b['amount_cents'] for b in due_soon)/100:,.2f}.",
                      "tone": "info"})
    active_goals = [g for g in goals if g["remaining_cents"] > 0]
    if active_goals:
        best = max(active_goals, key=lambda g: g["progress_pct"])
        cards.append({"kind": "goal_progress", "title": "Closest goal",
                      "detail": f"{best['name']} is {best['progress_pct']}% funded "
                                f"(${best['remaining_cents']/100:,.2f} to go).",
                      "tone": "good"})
    if largest is not None:
        cards.append({"kind": "largest_expense", "title": "Largest expense",
                      "detail": f"${abs(int(largest['amount_cents']))/100:,.2f} — "
                                f"{(largest.get('description') or _txn_category(largest))} "
                                f"on {largest['txn_date']}.",
                      "tone": "info"})

    nw_change = None
    hist = await get_balance_history(
        date_from=(date.today() - timedelta(days=90)).isoformat(),
        date_to=today_str(), user=user)
    pts = hist["combined"]["points"]
    if len(pts) >= 2:
        nw_change = pts[-1]["net_worth_cents"] - pts[0]["net_worth_cents"]
        cards.append({"kind": "net_worth_change", "title": "Net worth, last 90 days",
                      "detail": f"Net worth {'rose' if nw_change >= 0 else 'fell'} "
                                f"by ${abs(nw_change)/100:,.2f} over the last 90 days.",
                      "tone": "good" if nw_change >= 0 else "warn"})

    return {"period": period, "cards": cards,
            "cash_flow": cf, "cash_flow_prev": cf_prev,
            "top_categories": top_categories,
            "net_worth_change_90d_cents": nw_change}


# ---------------------------------------------------------------- summary (household UI, one fetch)

@router.get("/summary")
async def get_summary(user: dict = Depends(require("budget:read"))):
    """Everything the household home screen renders, in one call: net
    worth, account balances, this month's cash flow + budgets, bills with
    due states, goals, and insight cards. Business tenants get the same
    403 as every other personal endpoint — the wall is absolute."""
    await _require_household(user)
    period = today_str()[:7]
    net_worth = await get_net_worth(user)
    history = await get_balance_history(date_from=None, date_to=today_str(),
                                        user=user)
    accounts = [{"bank_account_id": a["bank_account_id"], "name": a["name"],
                 "kind": a["kind"], "currency": a.get("currency"),
                 "current_balance_cents": a["current_balance_cents"]}
                for a in history["accounts"]]
    return {
        "period": period,
        "net_worth": net_worth,
        "accounts": accounts,
        "balance_history": history["combined"],
        "cash_flow": await get_cash_flow(period, user),
        "budgets": await get_personal_budgets(period, user),
        "bills": await list_recurring_bills(user),
        "goals": await list_goals(user),
        "insights": (await get_insights(period, user))["cards"],
    }


# ================================================================ slice 3
# Budget-app fold-in (owner ruling, voice call 2026-11-18 ~14:38 CST):
# the standalone budget app STAYS standalone; its strengths are folded
# into this household layer — true envelope budgeting (per-envelope
# cycles, green/yellow/red health, rollover of unspent money, payday
# allocation suggestions), the family safe-to-spend view (balance -
# bills due before payday - envelope obligations - vault locks, with
# the formula in plain words), the savings-methods library (day-of-
# month stack, 52-week, 100-envelope, round-ups, $5 rule, no-spend
# days, payday escalator), timed savings vaults, a bills + envelopes
# cash-flow forecast, and the FIFO age-of-money counter.
#
# Behavior modeled on our own budget app (buildout/budget-app: BRIEF
# F1/F6/F8/F9/F13/F14/F15, Wave 2 methods library, Wave 6 forecast,
# Wave 10 safe-to-spend answer, P11 timed vault). All code here is
# original to this codebase and rides its machinery — Scoped tenant
# isolation, integer cents, the bank_transactions stream slices 1-2
# already read. Household tenants only: every endpoint below passes
# the same _require_household gate, and no business collection is
# read or written. Design record: docs/PERSONAL_FINANCE_FOLDIN.md.

from collections import deque
from datetime import datetime, timezone


# ---------------------------------------------------------------- envelope math

def _envelope_state(allocated_cents: int, spent_cents: int):
    """Envelope health: green while more than 20% of the cycle's
    effective allocation remains, yellow at 20% or less, red when
    nothing (or less than nothing) remains."""
    remaining = int(allocated_cents) - int(spent_cents)
    if allocated_cents <= 0:
        return ("red" if remaining <= 0 else "green"), remaining
    pct = remaining / allocated_cents * 100.0
    if remaining <= 0:
        return "red", remaining
    if pct <= 20:
        return "yellow", remaining
    return "green", remaining


def _cycle_window(on: date, start_day: int) -> tuple[date, date]:
    """The envelope cycle containing `on` for an envelope whose cycle
    starts on `start_day` of the month (clamped to 1..28 so the day
    exists in every month). Returns (start, end), end exclusive."""
    sd = min(max(int(start_day or 1), 1), 28)
    if on.day >= sd:
        start = on.replace(day=sd)
    else:
        prev_last = on.replace(day=1) - timedelta(days=1)
        start = prev_last.replace(day=sd)
    if start.month == 12:
        end = date(start.year + 1, 1, sd)
    else:
        end = date(start.year, start.month + 1, sd)
    return start, end


async def _category_spend(user: dict) -> dict[str, list[tuple[str, int]]]:
    """category -> sorted [(txn_date, out_cents)] over the household's
    money-out, non-transfer bank transactions — the same stream the
    budgets read; envelope spending is measured from it."""
    txns = await _scoped("bank_transactions", user).find_list(limit=100000)
    series: dict[str, list[tuple[str, int]]] = {}
    for t in txns:
        amt = int(t.get("amount_cents") or 0)
        if amt >= 0 or t.get("is_transfer") or not t.get("txn_date"):
            continue
        series.setdefault(_txn_category(t), []).append((t["txn_date"], -amt))
    for rows in series.values():
        rows.sort()
    return series


def _spent_between(rows: list[tuple[str, int]], start: date, end: date) -> int:
    lo, hi = start.isoformat(), end.isoformat()
    return sum(amt for dstr, amt in rows if lo <= dstr < hi)


def _envelope_view(env: dict, series: dict, today: date) -> dict:
    """Current-cycle view of one envelope: effective allocation (base
    allocation + rollover carried in), spent, remaining, health state.
    With rollover on, each finished cycle's leftover — overspend
    included — carries into the next cycle's allocation; the walk runs
    from the envelope's first cycle to the current one."""
    allocated = int(env.get("allocated_cents") or 0)
    rollover = bool(env.get("rollover", True))
    rows = series.get(env.get("category") or "", [])
    sd = env.get("cycle_start_day") or 1
    created = _to_date((env.get("created_at") or today.isoformat())[:10],
                       "created_at")
    start, end = _cycle_window(created, sd)
    cur_start, cur_end = _cycle_window(today, sd)
    carried = 0
    spent = _spent_between(rows, start, end)
    guard = 0
    while start < cur_start and guard < 240:
        carried = (allocated + (carried if rollover else 0)) - spent
        start, end = end, _cycle_window(end, sd)[1]
        spent = _spent_between(rows, start, end)
        guard += 1
    carried_in = carried if rollover else 0
    effective = allocated + carried_in
    state, remaining = _envelope_state(effective, spent)
    pct = (round(remaining / effective * 100, 1) if effective > 0
           else (0.0 if remaining <= 0 else 100.0))
    return {**_pub(env), "allocated_cents": allocated,
            "effective_allocated_cents": effective,
            "carried_in_cents": carried_in,
            "spent_cents": spent, "remaining_cents": remaining,
            "remaining_pct": pct, "state": state,
            "cycle_start": cur_start.isoformat(),
            "cycle_end": cur_end.isoformat()}


async def _envelope_views(user: dict, today: date) -> list[dict]:
    series = await _category_spend(user)
    rows = await _scoped("personal_envelopes", user).find_list(sort=[("name", 1)])
    return [_envelope_view(r, series, today) for r in rows]


# ---------------------------------------------------------------- envelopes

class EnvelopeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=80)
    allocated_cents: int = Field(ge=0)
    cycle_start_day: int = Field(default=1, ge=1, le=28)
    rollover: bool = True
    members: list[str] = Field(default_factory=list)
    visibility: Literal["shared", "private"] = "shared"
    active: bool = True


def _envelope_doc(body: EnvelopeIn) -> dict:
    return {"name": body.name.strip(),
            "category": body.category.strip(),
            "allocated_cents": body.allocated_cents,
            "cycle_start_day": body.cycle_start_day,
            "rollover": bool(body.rollover),
            "members": [str(m).strip()[:60] for m in body.members[:20]
                        if str(m).strip()],
            "visibility": body.visibility,
            "active": bool(body.active)}


@router.post("/envelopes")
async def create_envelope(body: EnvelopeIn,
                          user: dict = Depends(require("budget:write"))):
    """An envelope is a persistent pot for one spending category: a
    per-cycle allocation, its own cycle start day, and a health state.
    Spending is measured from the household's categorized bank
    transactions in the envelope's category during the current cycle —
    nothing is entered twice."""
    await _require_household(user)
    doc = {"id": new_id(), **_envelope_doc(body),
           "created_at": now_iso(), "updated_at": now_iso()}
    await _scoped("personal_envelopes", user).insert_one(doc)
    series = await _category_spend(user)
    return _envelope_view(doc, series, _to_date(today_str()))


@router.get("/envelopes")
async def list_envelopes(user: dict = Depends(require("budget:read"))):
    await _require_household(user)
    return await _envelope_views(user, _to_date(today_str()))


@router.put("/envelopes/{envelope_id}")
async def update_envelope(envelope_id: str, body: EnvelopeIn,
                          user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    col = _scoped("personal_envelopes", user)
    existing = await col.find_one({"id": envelope_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Envelope not found")
    patch = {**_envelope_doc(body), "updated_at": now_iso()}
    await col.update_one({"id": envelope_id}, {"$set": patch})
    series = await _category_spend(user)
    return _envelope_view({**existing, **patch}, series, _to_date(today_str()))


@router.delete("/envelopes/{envelope_id}")
async def delete_envelope(envelope_id: str,
                          user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    return await _delete_scoped("personal_envelopes", user, envelope_id, "Envelope")


@router.get("/envelopes/suggestions")
async def envelope_suggestions(paycheck_cents: int = Query(..., gt=0),
                               user: dict = Depends(require("budget:read"))):
    """Payday pulse: suggest envelope allocations from the household's
    last three months of spending — each category's share of the total,
    applied to the paycheck. Suggestions only; nothing is created
    until the customer saves an envelope from a suggestion."""
    await _require_household(user)
    first = _shift_period(today_str()[:7], -2)
    start, _ = _month_range(first)
    txns = await _scoped("bank_transactions", user).find_list(
        {"txn_date": {"$gte": start}}, limit=100000)
    totals: dict[str, int] = {}
    for t in txns:
        amt = int(t.get("amount_cents") or 0)
        if amt >= 0 or t.get("is_transfer"):
            continue
        cat = _txn_category(t)
        totals[cat] = totals.get(cat, 0) + (-amt)
    grand = sum(totals.values())
    rows = []
    if grand > 0:
        for cat, spent in sorted(totals.items(), key=lambda kv: -kv[1]):
            rows.append({
                "category": cat,
                "spent_last_3_months_cents": spent,
                "share_pct": round(spent / grand * 100, 1),
                "suggested_cents": round(paycheck_cents * spent / grand)})
    return {"paycheck_cents": paycheck_cents, "window_start": start,
            "suggestion_only": True, "rows": rows}


# ---------------------------------------------------------------- payday + bills helpers

def _next_semimonthly(d: date) -> date:
    """Semimonthly paydays fall on the 15th and the last day of the
    month: the first such date strictly after d."""
    if d.day < 15:
        return d.replace(day=15)
    if d.month == 12:
        first_next = date(d.year + 1, 1, 1)
    else:
        first_next = date(d.year, d.month + 1, 1)
    last_day = (first_next - timedelta(days=1)).day
    if d.day < last_day:
        return d.replace(day=last_day)
    return first_next.replace(day=15)


def _next_payday(settings: dict, today: date) -> Optional[date]:
    """First payday strictly after today, from the household's stored
    last_payday_date + payday_cadence (settings, slice 3). None when
    the household has not set its payday rhythm."""
    last_s = (settings or {}).get("last_payday_date")
    cadence = (settings or {}).get("payday_cadence")
    if not last_s or not cadence:
        return None
    d = _to_date(last_s, "last_payday_date")
    guard = 0
    while d <= today and guard < 600:
        d = (_next_semimonthly(d) if cadence == "semimonthly"
             else _advance_date(d, cadence))
        guard += 1
    return d


def _bill_occurrences(bill: dict, start: date, end: date,
                      count_overdue_once: bool = False) -> list[date]:
    """Occurrences of a recurring bill falling in [start, end],
    walking its cadence forward from next_due_date. With
    count_overdue_once, a bill whose due date has already passed also
    counts once at its (past) due date — it is due now — and its
    following in-window occurrences count too."""
    due = _to_date(bill["next_due_date"], "next_due_date")
    out: list[date] = []
    if due < start:
        if count_overdue_once:
            out.append(due)
        guard = 0
        while due < start and guard < 600:
            due = _advance_date(due, bill["cadence"])
            guard += 1
    guard = 0
    while due <= end and guard < 600:
        out.append(due)
        due = _advance_date(due, bill["cadence"])
        guard += 1
    return out


async def _account_balances(user: dict, today: date) -> list[dict]:
    """Current balance per household bank account: the stored opening
    balance plus every transaction dated on or before today — the
    balance-history replay without the day-by-day series."""
    accounts = await _scoped("bank_accounts", user).find_list(sort=[("name", 1)])
    metas = {m["bank_account_id"]: m
             for m in await _scoped("personal_account_meta", user).find_list()}
    txns = await _scoped("bank_transactions", user).find_list(limit=100000)
    sums: dict[str, int] = {}
    today_s = today.isoformat()
    for t in txns:
        if t.get("txn_date") and t["txn_date"] <= today_s:
            aid = t.get("bank_account_id")
            sums[aid] = sums.get(aid, 0) + int(t.get("amount_cents") or 0)
    out = []
    for a in accounts:
        opening = int((metas.get(a["id"]) or {}).get("opening_balance_cents") or 0)
        out.append({"bank_account_id": a["id"], "name": a.get("name"),
                    "balance_cents": opening + sums.get(a["id"], 0)})
    return out


# ---------------------------------------------------------------- safe-to-spend

@router.get("/safe-to-spend")
async def get_safe_to_spend(user: dict = Depends(require("budget:read"))):
    """The family number: what the household can spend today without
    touching money that is already spoken for.

        safe = account balances
               - bills due before the next payday
               - envelope obligations (unspent envelope money)
               - vault locks (vault balances + locked goals' savings)

    Every part is returned alongside the number, with the formula in
    plain words, so anyone in the family can see why it is what it
    is. When the household has not set its payday rhythm, the horizon
    is today + 14 days and the payload says so (payday_estimated)."""
    tenant = await _require_household(user)
    today = _to_date(today_str())
    payday = _next_payday(tenant.get("settings") or {}, today)
    estimated = payday is None
    horizon = payday or (today + timedelta(days=14))

    accounts = await _account_balances(user, today)
    balance = sum(a["balance_cents"] for a in accounts)

    bills = [b for b in await _scoped("personal_recurring_bills", user).find_list()
             if b.get("active", True)]
    considered: list[dict] = []
    bills_total = 0
    for b in bills:
        for d in _bill_occurrences(b, today, horizon, count_overdue_once=True):
            amt = int(b.get("amount_cents") or 0)
            bills_total += amt
            considered.append({"bill_id": b["id"], "name": b["name"],
                               "amount_cents": amt,
                               "due_date": d.isoformat(),
                               "overdue": d < today})
    considered.sort(key=lambda r: r["due_date"])

    env_views = await _envelope_views(user, today)
    obligations = sum(max(0, v["remaining_cents"]) for v in env_views)

    vaults = await _scoped("personal_vaults", user).find_list()
    vault_locks = sum(int(v.get("balance_cents") or 0) for v in vaults)
    goals = await _scoped("personal_goals", user).find_list()
    goal_locks = sum(int(g.get("saved_cents") or 0)
                     for g in goals if g.get("locked"))
    locks = vault_locks + goal_locks

    raw = balance - bills_total - obligations - locks
    largest = max(env_views, key=lambda v: v["remaining_cents"], default=None)
    return {
        "as_of": today.isoformat(),
        "next_payday_date": horizon.isoformat(),
        "payday_estimated": estimated,
        "safe_to_spend_cents": max(raw, 0),
        "raw_cents": raw,
        "parts": {
            "account_balance_cents": balance,
            "upcoming_bills_before_payday_cents": bills_total,
            "envelope_obligations_cents": obligations,
            "vault_locks_cents": locks,
            "vault_balances_cents": vault_locks,
            "locked_goals_cents": goal_locks,
        },
        "plain_words": ("Your balance, minus bills due before payday, minus "
                        "money promised to envelopes, minus locked vault "
                        "money. What is left is safe to spend."),
        "accounts": accounts,
        "bills_considered": considered,
        "envelopes": [{"envelope_id": v["id"], "name": v["name"],
                       "remaining_cents": v["remaining_cents"],
                       "state": v["state"]} for v in env_views],
        "largest_envelope": ({"name": largest["name"],
                              "remaining_cents": largest["remaining_cents"]}
                             if largest else None),
    }


# ---------------------------------------------------------------- vaults

class VaultIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    balance_cents: int = Field(default=0, ge=0)
    withdrawal_delay_hours: int = Field(default=48, ge=0, le=24 * 30)


class VaultAmountIn(BaseModel):
    amount_cents: int = Field(gt=0)


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _vault_view(v: dict) -> dict:
    out = _pub(v)
    pending = v.get("pending_withdrawal")
    if pending:
        ready = datetime.now(timezone.utc) >= _parse_dt(pending["available_at"])
        out["pending_withdrawal"] = {**pending, "ready": ready}
    return out


async def _get_vault(user: dict, vault_id: str) -> dict:
    vault = await _scoped("personal_vaults", user).find_one({"id": vault_id})
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")
    return vault


@router.post("/vaults")
async def create_vault(body: VaultIn,
                       user: dict = Depends(require("budget:write"))):
    """A savings vault: money set aside on purpose, behind a
    withdrawal timer. The balance is a tracked lock (the money stays
    in the household's accounts); vault balances count as locks in
    the safe-to-spend view."""
    await _require_household(user)
    doc = {"id": new_id(), "name": body.name.strip(),
           "balance_cents": body.balance_cents,
           "withdrawal_delay_hours": body.withdrawal_delay_hours,
           "pending_withdrawal": None,
           "created_at": now_iso(), "updated_at": now_iso()}
    await _scoped("personal_vaults", user).insert_one(doc)
    return _vault_view(doc)


@router.get("/vaults")
async def list_vaults(user: dict = Depends(require("budget:read"))):
    await _require_household(user)
    rows = await _scoped("personal_vaults", user).find_list(sort=[("name", 1)])
    return [_vault_view(r) for r in rows]


@router.post("/vaults/{vault_id}/deposit")
async def deposit_vault(vault_id: str, body: VaultAmountIn,
                        user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    vault = await _get_vault(user, vault_id)
    balance = int(vault.get("balance_cents") or 0) + body.amount_cents
    await _scoped("personal_vaults", user).update_one(
        {"id": vault_id}, {"$set": {"balance_cents": balance,
                                    "updated_at": now_iso()}})
    return _vault_view({**vault, "balance_cents": balance})


@router.post("/vaults/{vault_id}/request-withdrawal")
async def request_vault_withdrawal(vault_id: str, body: VaultAmountIn,
                                   user: dict = Depends(require("budget:write"))):
    """Start the withdrawal timer. The money stays locked until the
    delay elapses — that pause is the point of a vault."""
    await _require_household(user)
    vault = await _get_vault(user, vault_id)
    if vault.get("pending_withdrawal"):
        raise HTTPException(status_code=400,
                            detail="A withdrawal is already counting down")
    if body.amount_cents > int(vault.get("balance_cents") or 0):
        raise HTTPException(status_code=400,
                            detail="Withdrawal exceeds the vault balance")
    now = datetime.now(timezone.utc)
    pending = {"amount_cents": body.amount_cents,
               "requested_at": now.isoformat(),
               "available_at": (now + timedelta(
                   hours=int(vault.get("withdrawal_delay_hours") or 0))).isoformat()}
    await _scoped("personal_vaults", user).update_one(
        {"id": vault_id}, {"$set": {"pending_withdrawal": pending,
                                    "updated_at": now_iso()}})
    return _vault_view({**vault, "pending_withdrawal": pending})


@router.post("/vaults/{vault_id}/withdraw")
async def withdraw_vault(vault_id: str,
                         user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    vault = await _get_vault(user, vault_id)
    pending = vault.get("pending_withdrawal")
    if not pending:
        raise HTTPException(status_code=400, detail="No withdrawal was requested")
    if datetime.now(timezone.utc) < _parse_dt(pending["available_at"]):
        raise HTTPException(
            status_code=400,
            detail=f"Withdrawal timer has not elapsed; available at "
                   f"{pending['available_at']}")
    balance = int(vault.get("balance_cents") or 0) - int(pending["amount_cents"])
    await _scoped("personal_vaults", user).update_one(
        {"id": vault_id}, {"$set": {"balance_cents": balance,
                                    "pending_withdrawal": None,
                                    "updated_at": now_iso()}})
    return _vault_view({**vault, "balance_cents": balance,
                        "pending_withdrawal": None})


@router.delete("/vaults/{vault_id}")
async def delete_vault(vault_id: str,
                       user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    return await _delete_scoped("personal_vaults", user, vault_id, "Vault")


# ---------------------------------------------------------------- savings methods library

SAVINGS_METHODS = [
    {"key": "day_of_month", "name": "Day-of-Month Stack", "cadence": "daily",
     "description": "Save $1 on day 1, $2 on day 2, up to $31 on day 31 — "
                    "$496 a month, $5,952 a year."},
    {"key": "fifty_two_week", "name": "52-Week Challenge", "cadence": "weekly",
     "description": "Save $1 in week 1, $2 in week 2, rising $1 a week to "
                    "$52 in week 52 — $1,378 by year's end. The reverse "
                    "variant starts at $52 and steps down."},
    {"key": "hundred_envelope", "name": "100-Envelope Challenge",
     "cadence": "daily",
     "description": "Envelopes numbered 1 to 100. Draw one a day and save "
                    "that amount; when the box is empty you have saved "
                    "$5,050."},
    {"key": "round_ups", "name": "Round-Ups", "cadence": "per purchase",
     "description": "Every purchase rounds up to the next dollar and the "
                    "spare change sweeps to savings."},
    {"key": "five_dollar_rule", "name": "The $5 Rule",
     "cadence": "whenever a $5 bill lands",
     "description": "Every $5 bill that passes through your hands goes to "
                    "savings instead of being spent."},
    {"key": "no_spend_days", "name": "No-Spend Days", "cadence": "weekly",
     "description": "Pick no-spend days each week. Each day you keep sweeps "
                    "your average daily discretionary spend."},
    {"key": "payday_escalator", "name": "Payday Escalator",
     "cadence": "per payday",
     "description": "Save a percent of each payday that rises by 1 point on "
                    "a schedule you set. Every rise is a suggestion — you "
                    "approve it."},
]
_METHOD_BY_KEY = {m["key"]: m for m in SAVINGS_METHODS}
_DISCRETIONARY_CATEGORIES = {"coffee", "dining", "fun", "subscriptions",
                             "entertainment", "eating out", "restaurants"}


def _fifty_two_week_amounts(variant: str) -> list[int]:
    amounts = [i * 100 for i in range(1, 53)]
    if variant == "reverse":
        amounts.reverse()
    return amounts


def _round_up_cents(purchase_cents: int) -> int:
    """Spare change to the next whole dollar (whole dollars sweep 0)."""
    return (100 - (int(purchase_cents) % 100)) % 100


async def _avg_daily_discretionary_cents(user: dict) -> int:
    """Average daily discretionary spend from the household's history
    (discretionary categories when any exist, else all spending)."""
    txns = await _scoped("bank_transactions", user).find_list(limit=100000)
    by_day: dict[str, int] = {}
    fallback: dict[str, int] = {}
    for t in txns:
        amt = int(t.get("amount_cents") or 0)
        if amt >= 0 or t.get("is_transfer") or not t.get("txn_date"):
            continue
        fallback[t["txn_date"]] = fallback.get(t["txn_date"], 0) + (-amt)
        if _txn_category(t).strip().lower() in _DISCRETIONARY_CATEGORIES:
            by_day[t["txn_date"]] = by_day.get(t["txn_date"], 0) + (-amt)
    use = by_day or fallback
    if not use:
        return 0
    return round(sum(use.values()) / len(use))


async def _roundup_projection(user: dict) -> dict:
    """Yearly round-up pace from the household's own purchase history
    (ESTIMATE, basis shown)."""
    txns = await _scoped("bank_transactions", user).find_list(limit=100000)
    ups: list[int] = []
    dates: list[str] = []
    for t in txns:
        amt = int(t.get("amount_cents") or 0)
        if amt >= 0 or t.get("is_transfer") or not t.get("txn_date"):
            continue
        ups.append(_round_up_cents(-amt))
        dates.append(t["txn_date"])
    if not ups:
        return {"amount_cents": 0, "label": "ESTIMATE",
                "basis": "no purchases on record yet"}
    avg = round(sum(ups) / len(ups))
    span_days = max(7, (_to_date(max(dates)) - _to_date(min(dates))).days + 1)
    per_week = len(ups) / span_days * 7
    return {"amount_cents": round(avg * per_week * 52), "label": "ESTIMATE",
            "basis": (f"your average round-up of ${avg / 100:.2f} across "
                      f"{per_week:.1f} purchases a week, times 52 weeks")}


def _method_defaults(method_key: str, avg_discretionary: int) -> dict:
    if method_key == "fifty_two_week":
        return {"week_index": 0}
    if method_key == "hundred_envelope":
        return {"remaining": list(range(1, 101)), "drawn": []}
    if method_key == "round_ups":
        return {"swept_txn_ids": []}
    if method_key == "no_spend_days":
        return {"avg_daily_discretionary_cents": avg_discretionary,
                "weekly_target": 2}
    if method_key == "payday_escalator":
        return {"payday_amount_cents": 200000, "paydays_per_year": 26,
                "percent": 5, "rise_every_paydays": 2, "cap_percent": 20,
                "payday_count": 0, "pending_rise": False}
    return {}


def _savings_method_view(method: dict, enrollment: Optional[dict],
                         events: list[dict], today: date,
                         roundup_projection: Optional[dict] = None) -> dict:
    key = method["key"]
    cfg = dict((enrollment or {}).get("config") or {})
    variant = (enrollment or {}).get("variant") or "standard"
    saved = sum(int(e.get("amount_cents") or 0) for e in events)
    out = {**method,
           "enrolled": bool(enrollment and enrollment.get("active", True)),
           "variant": variant, "config": cfg,
           "saved_total_cents": saved, "actions_taken": len(events),
           "recent_events": [_pub(e) for e in events[-5:]],
           "cycle_goal_cents": 0, "yearly_pace": None,
           "next_action": None, "progress_pct": 0.0, "complete": False}
    if key == "day_of_month":
        month = today.isoformat()[:7]
        saved_month = sum(int(e.get("amount_cents") or 0) for e in events
                          if (e.get("date") or "")[:7] == month)
        expected = sum(range(1, today.day + 1)) * 100
        done_today = any(e.get("date") == today.isoformat() for e in events)
        out.update({
            "cycle_goal_cents": 49600, "saved_this_cycle_cents": saved_month,
            "expected_by_today_cents": expected,
            "pace": "on_pace" if saved_month >= expected else "behind",
            "yearly_pace": {"amount_cents": 595200, "label": "EXACT",
                            "basis": "$1 on day 1 rising to $31 on day 31 "
                                     "sums to $496 a month, $5,952 a year"},
            "next_action": (None if done_today else {
                "action": "save_day",
                "label": f"Save ${today.day} for day {today.day}"}),
            "progress_pct": round(saved_month / 49600 * 100, 1)})
    elif key == "fifty_two_week":
        amounts = _fifty_two_week_amounts(variant)
        idx = int(cfg.get("week_index") or 0)
        out.update({
            "cycle_goal_cents": 137800,
            "yearly_pace": {"amount_cents": 137800, "label": "EXACT",
                            "basis": "$1 in week 1 rising $1 a week to $52 "
                                     "sums to $1,378"},
            "next_action": ({"action": "save_week",
                             "label": f"Save ${amounts[idx] // 100} "
                                      f"for week {idx + 1}"}
                            if idx < 52 else None),
            "progress_pct": round(saved / 137800 * 100, 1),
            "complete": idx >= 52})
    elif key == "hundred_envelope":
        remaining = cfg.get("remaining")
        if remaining is None:
            remaining = list(range(1, 101))
        out.update({
            "cycle_goal_cents": 505000,
            "yearly_pace": {"amount_cents": 505000, "label": "EXACT",
                            "basis": "envelopes numbered 1 to 100 sum "
                                     "to $5,050"},
            "next_action": ({"action": "draw_envelope",
                             "label": "Draw today's envelope"}
                            if remaining else None),
            "progress_pct": round(saved / 505000 * 100, 1),
            "complete": not remaining})
    elif key == "round_ups":
        proj = roundup_projection or {"amount_cents": 0, "label": "ESTIMATE",
                                      "basis": "no purchases on record yet"}
        out.update({"cycle_goal_cents": proj["amount_cents"],
                    "yearly_pace": proj,
                    "next_action": {"action": "sweep_roundups",
                                    "label": "Sweep new round-ups"}})
    elif key == "five_dollar_rule":
        out.update({
            "cycle_goal_cents": 26000,
            "yearly_pace": {"amount_cents": 26000, "label": "ESTIMATE",
                            "basis": "one $5 bill a week for 52 weeks; "
                                     "log more and the pace is yours"},
            "next_action": {"action": "log_five",
                            "label": "Log a $5 bill saved"},
            "progress_pct": round(saved / 26000 * 100, 1)})
    elif key == "no_spend_days":
        avg = int(cfg.get("avg_daily_discretionary_cents") or 0)
        target = int(cfg.get("weekly_target") or 2)
        pace = avg * target * 52
        out.update({
            "cycle_goal_cents": pace,
            "yearly_pace": {"amount_cents": pace, "label": "ESTIMATE",
                            "basis": f"your average daily discretionary "
                                     f"spend of ${avg / 100:,.2f} times "
                                     f"{target} kept days a week, times "
                                     f"52 weeks"},
            "next_action": {"action": "keep_day",
                            "label": "Mark today a kept no-spend day"},
            "progress_pct": round(saved / pace * 100, 1) if pace else 0.0})
    elif key == "payday_escalator":
        pct = int(cfg.get("percent") or 5)
        payday_amt = int(cfg.get("payday_amount_cents") or 0)
        per_year = int(cfg.get("paydays_per_year") or 26)
        pace = round(payday_amt * pct / 100) * per_year
        if cfg.get("pending_rise"):
            nxt = {"action": "approve_rise",
                   "label": f"Approve the suggested rise to {pct + 1}% "
                            f"(or decline it)"}
        else:
            nxt = {"action": "record_payday",
                   "label": f"Record a payday at {pct}%"}
        out.update({
            "cycle_goal_cents": pace,
            "yearly_pace": {"amount_cents": pace, "label": "PROJECTION",
                            "basis": f"at today's {pct}% of a "
                                     f"${payday_amt / 100:,.2f} payday, "
                                     f"before any approved rise"},
            "next_action": nxt,
            "progress_pct": round(saved / pace * 100, 1) if pace else 0.0})
    return out


async def _method_view_for(user: dict, method_key: str,
                           enrollment: Optional[dict]) -> dict:
    events: list[dict] = []
    if enrollment:
        events = await _scoped("personal_savings_events", user).find_list(
            {"enrollment_id": enrollment["id"]}, sort=[("date", 1)],
            limit=10000)
    proj = (await _roundup_projection(user)
            if method_key == "round_ups" else None)
    return _savings_method_view(_METHOD_BY_KEY[method_key], enrollment,
                                events, _to_date(today_str()), proj)


@router.get("/savings-methods")
async def list_savings_methods(user: dict = Depends(require("budget:read"))):
    """The methods library: every method with the household's
    enrollment status, running total, yearly pace (labeled EXACT /
    ESTIMATE / PROJECTION with its basis), and its next action.
    Several methods may run at once, each on its own cycle."""
    await _require_household(user)
    enrollments = {e["method_key"]: e for e in
                   await _scoped("personal_savings_methods", user).find_list()}
    return [await _method_view_for(user, m["key"], enrollments.get(m["key"]))
            for m in SAVINGS_METHODS]


class SavingsEnrollIn(BaseModel):
    variant: str = "standard"
    config: dict = Field(default_factory=dict)


@router.post("/savings-methods/{method_key}/enroll")
async def enroll_savings_method(method_key: str, body: SavingsEnrollIn,
                                user: dict = Depends(require("budget:write"))):
    await _require_household(user)
    if method_key not in _METHOD_BY_KEY:
        raise HTTPException(status_code=404, detail="Unknown savings method")
    if method_key == "fifty_two_week" and body.variant not in (
            "standard", "forward", "reverse"):
        raise HTTPException(status_code=400,
                            detail="variant must be forward or reverse")
    variant = ("reverse" if method_key == "fifty_two_week"
               and body.variant == "reverse" else "standard")
    avg = (await _avg_daily_discretionary_cents(user)
           if method_key == "no_spend_days" else 0)
    col = _scoped("personal_savings_methods", user)
    existing = await col.find_one({"method_key": method_key})
    if existing:
        cfg = {**_method_defaults(method_key, avg),
               **(existing.get("config") or {}), **body.config}
        await col.update_one({"id": existing["id"]}, {"$set": {
            "active": True, "variant": variant, "config": cfg,
            "updated_at": now_iso()}})
        return await _method_view_for(
            user, method_key, {**existing, "active": True,
                               "variant": variant, "config": cfg})
    doc = {"id": new_id(), "method_key": method_key, "variant": variant,
           "config": {**_method_defaults(method_key, avg), **body.config},
           "active": True, "enrolled_on": today_str(),
           "created_at": now_iso(), "updated_at": now_iso()}
    await col.insert_one(doc)
    return await _method_view_for(user, method_key, doc)


@router.delete("/savings-methods/{method_key}")
async def unenroll_savings_method(method_key: str,
                                  user: dict = Depends(require("budget:write"))):
    """Stop a method. Its events (the savings history) are kept — the
    library still shows what was saved while it ran."""
    await _require_household(user)
    if method_key not in _METHOD_BY_KEY:
        raise HTTPException(status_code=404, detail="Unknown savings method")
    col = _scoped("personal_savings_methods", user)
    existing = await col.find_one({"method_key": method_key})
    if not existing:
        raise HTTPException(status_code=404, detail="Not enrolled in this method")
    await col.update_one({"id": existing["id"]},
                         {"$set": {"active": False, "updated_at": now_iso()}})
    return await _method_view_for(
        user, method_key, {**existing, "active": False})


class SavingsActionIn(BaseModel):
    action: str
    day: Optional[int] = Field(default=None, ge=1, le=31)
    number: Optional[int] = Field(default=None, ge=1, le=100)


@router.post("/savings-methods/{method_key}/actions")
async def savings_method_action(method_key: str, body: SavingsActionIn,
                                user: dict = Depends(require("budget:write"))):
    """Take a method's next step. Each action records a savings event;
    amounts are tracked savings (the money stays in the household's
    accounts — a method is a rhythm, not a transfer)."""
    await _require_household(user)
    if method_key not in _METHOD_BY_KEY:
        raise HTTPException(status_code=404, detail="Unknown savings method")
    col = _scoped("personal_savings_methods", user)
    enrollment = await col.find_one({"method_key": method_key})
    if not enrollment or not enrollment.get("active", True):
        raise HTTPException(status_code=400,
                            detail="Enroll in this method first")
    cfg = dict(enrollment.get("config") or {})
    today = _to_date(today_str())
    action = body.action
    amount: Optional[int] = None
    note: Optional[str] = None

    if method_key == "day_of_month" and action == "save_day":
        day = body.day or today.day
        amount = day * 100
        note = f"Day {day} of the stack"
    elif method_key == "fifty_two_week" and action == "save_week":
        amounts = _fifty_two_week_amounts(enrollment.get("variant") or "standard")
        idx = int(cfg.get("week_index") or 0)
        if idx >= 52:
            raise HTTPException(status_code=400, detail="Challenge complete")
        amount = amounts[idx]
        cfg["week_index"] = idx + 1
        note = f"Week {idx + 1}"
    elif method_key == "hundred_envelope" and action == "draw_envelope":
        remaining = list(cfg.get("remaining") or [])
        if not remaining:
            raise HTTPException(status_code=400,
                                detail="The box is empty — complete")
        number = body.number if body.number is not None else remaining[0]
        if number not in remaining:
            raise HTTPException(status_code=400,
                                detail="That envelope is already drawn")
        remaining.remove(number)
        cfg["remaining"] = remaining
        cfg["drawn"] = list(cfg.get("drawn") or []) + [number]
        amount = number * 100
        note = f"Envelope #{number}"
    elif method_key == "round_ups" and action == "sweep_roundups":
        swept = set(cfg.get("swept_txn_ids") or [])
        txns = await _scoped("bank_transactions", user).find_list(limit=100000)
        total = 0
        new_ids = []
        for t in txns:
            amt = int(t.get("amount_cents") or 0)
            if amt >= 0 or t.get("is_transfer") or t.get("id") in swept:
                continue
            total += _round_up_cents(-amt)
            new_ids.append(t.get("id"))
        cfg["swept_txn_ids"] = sorted(swept | set(new_ids))
        amount = total
        note = f"Round-ups from {len(new_ids)} purchases"
    elif method_key == "five_dollar_rule" and action == "log_five":
        amount = 500
        note = "A $5 bill retired to savings"
    elif method_key == "no_spend_days" and action == "keep_day":
        amount = int(cfg.get("avg_daily_discretionary_cents") or 0)
        if amount <= 0:
            raise HTTPException(
                status_code=400,
                detail="No discretionary spending history to sweep yet")
        note = "A kept no-spend day"
    elif method_key == "payday_escalator" and action == "record_payday":
        pct = int(cfg.get("percent") or 5)
        amount = round(int(cfg.get("payday_amount_cents") or 0) * pct / 100)
        count = int(cfg.get("payday_count") or 0) + 1
        cfg["payday_count"] = count
        every = int(cfg.get("rise_every_paydays") or 2)
        if (every and count % every == 0
                and pct < int(cfg.get("cap_percent") or 20)):
            cfg["pending_rise"] = True
        note = f"Payday at {pct}%"
    elif method_key == "payday_escalator" and action == "approve_rise":
        if not cfg.get("pending_rise"):
            raise HTTPException(status_code=400,
                                detail="No rise is waiting for approval")
        cfg["percent"] = min(int(cfg.get("percent") or 5) + 1,
                             int(cfg.get("cap_percent") or 20))
        cfg["pending_rise"] = False
        amount = 0
        note = f"Rise approved — now {cfg['percent']}%"
    elif method_key == "payday_escalator" and action == "decline_rise":
        if not cfg.get("pending_rise"):
            raise HTTPException(status_code=400,
                                detail="No rise is waiting for approval")
        cfg["pending_rise"] = False
        amount = 0
        note = "Rise declined"
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Action {action} is not part of this method")

    if amount:
        await _scoped("personal_savings_events", user).insert_one({
            "id": new_id(), "enrollment_id": enrollment["id"],
            "method_key": method_key, "action": action,
            "amount_cents": int(amount), "date": today.isoformat(),
            "note": note, "created_at": now_iso()})
    await col.update_one({"id": enrollment["id"]},
                         {"$set": {"config": cfg, "updated_at": now_iso()}})
    view = await _method_view_for(user, method_key,
                                  {**enrollment, "config": cfg})
    view["action_result"] = {"action": action,
                             "amount_cents": int(amount or 0), "note": note}
    return view


# ---------------------------------------------------------------- forecast

@router.get("/forecast")
async def get_forecast(months: int = Query(6, ge=1, le=12),
                       user: dict = Depends(require("budget:read"))):
    """Cash-flow forecast: for each coming month, the bills that fall
    due (walked forward from the recurring-bill registry) and each
    envelope's projected health — projected spend is the envelope
    category's average over the last three months. Everything is
    labeled PROJECTION with its basis; nothing here is a promise."""
    await _require_household(user)
    today = _to_date(today_str())
    cur_period = today_str()[:7]
    series = await _category_spend(user)
    window = {_shift_period(cur_period, -k) for k in (3, 2, 1)}
    avg_spend: dict[str, int] = {}
    for cat, rows in series.items():
        total = sum(amt for dstr, amt in rows if dstr[:7] in window)
        avg_spend[cat] = round(total / 3)
    bills = [b for b in await _scoped("personal_recurring_bills", user).find_list()
             if b.get("active", True)]
    env_views = await _envelope_views(user, today)
    out_months = []
    for i in range(months):
        period = _shift_period(cur_period, i)
        m_start_s, m_end_s = _month_range(period)
        m_start, m_end_excl = _to_date(m_start_s), _to_date(m_end_s)
        m_last = m_end_excl - timedelta(days=1)
        due_rows = []
        for b in bills:
            for d in _bill_occurrences(b, m_start, m_last):
                due_rows.append({"bill_id": b["id"], "name": b["name"],
                                 "amount_cents": int(b.get("amount_cents") or 0),
                                 "due_date": d.isoformat()})
        due_rows.sort(key=lambda r: r["due_date"])
        env_proj = []
        for v in env_views:
            projected = avg_spend.get(v["category"], 0)
            state, remaining = _envelope_state(
                v["effective_allocated_cents"], projected)
            env_proj.append({"envelope_id": v["id"], "name": v["name"],
                             "category": v["category"],
                             "projected_spent_cents": projected,
                             "projected_remaining_cents": remaining,
                             "state": state, "label": "PROJECTION"})
        out_months.append({
            "month": period,
            "bills_due_cents": sum(r["amount_cents"] for r in due_rows),
            "bills": due_rows, "envelopes": env_proj})
    return {"label": "PROJECTION",
            "basis": ("Bills project from your recurring-bill registry; "
                      "envelope spending projects from each category's "
                      "average over the last three months."),
            "months": out_months}


# ---------------------------------------------------------------- age of money

@router.get("/age-of-money")
async def get_age_of_money(user: dict = Depends(require("budget:read"))):
    """Age of money (FIFO): how many days ago the household earned
    the oldest dollar it has not spent yet. Income is consumed
    oldest-first as spending lands; transfers between the household's
    own accounts are neither income nor spending."""
    await _require_household(user)
    today = _to_date(today_str())
    txns = await _scoped("bank_transactions", user).find_list(
        sort=[("txn_date", 1)], limit=100000)
    queue: deque = deque()
    today_s = today.isoformat()
    for t in txns:
        if t.get("is_transfer") or not t.get("txn_date"):
            continue
        if t["txn_date"] > today_s:
            continue
        amt = int(t.get("amount_cents") or 0)
        if amt > 0:
            queue.append([t["txn_date"], amt])
        elif amt < 0:
            spend = -amt
            while spend > 0 and queue:
                head = queue[0]
                take = min(head[1], spend)
                head[1] -= take
                spend -= take
                if head[1] == 0:
                    queue.popleft()
    if not queue:
        return {"as_of": today_s, "age_days": 0,
                "oldest_unspent_income_date": None,
                "unspent_income_cents": 0}
    oldest = queue[0][0]
    return {"as_of": today_s,
            "age_days": (today - _to_date(oldest)).days,
            "oldest_unspent_income_date": oldest,
            "unspent_income_cents": sum(c for _, c in queue)}
