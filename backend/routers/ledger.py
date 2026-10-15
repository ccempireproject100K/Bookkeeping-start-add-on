"""Ledger router: chart of accounts, journal entries, reports, fiscal periods, opening balances."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require
from lib.coa import ACCOUNT_TYPE_OF, NORMAL_BALANCE_OF
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/ledger", tags=["ledger"])


def accts(user):
    return Scoped(db.accounts, user["tenant_id"])


def entries(user):
    return Scoped(db.journal_entries, user["tenant_id"])


def lines(user):
    return Scoped(db.journal_lines, user["tenant_id"])


# ---------------- Chart of Accounts ----------------
@router.get("/accounts")
async def list_accounts(user: dict = Depends(require("ledger:read"))):
    rows = await accts(user).find_list(sort=[("code", 1)])
    return rows


class AccountBody(BaseModel):
    code: str
    name: str
    type: str
    parent_code: Optional[str] = None
    description: Optional[str] = None


@router.post("/accounts")
async def create_account(body: AccountBody, user: dict = Depends(require("ledger:admin"))):
    if body.type not in ("asset", "liability", "equity", "revenue", "expense"):
        raise HTTPException(status_code=422, detail="Invalid account type")
    if await accts(user).find_one({"code": body.code}):
        raise HTTPException(status_code=409, detail="Account code already exists")
    normal = "debit" if body.type in ("asset", "expense") else "credit"
    doc = {
        "id": new_id(), "code": body.code, "name": body.name, "type": body.type,
        "normal_balance": normal, "parent_code": body.parent_code, "is_system": False,
        "archived": False, "description": body.description, "created_at": now_iso(),
    }
    await accts(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


class AccountPatch(BaseModel):
    name: Optional[str] = None
    archived: Optional[bool] = None
    description: Optional[str] = None


@router.patch("/accounts/{code}")
async def patch_account(code: str, body: AccountPatch, user: dict = Depends(require("ledger:admin"))):
    acc = await accts(user).find_one({"code": code})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    update = {}
    if body.name is not None:
        update["name"] = body.name
    if body.description is not None:
        update["description"] = body.description
    if body.archived is not None:
        if body.archived:
            if acc.get("is_system"):
                raise HTTPException(status_code=409, detail="System accounts cannot be archived")
            used = await lines(user).count({"account_code": code})
            if used:
                raise HTTPException(status_code=409, detail="Account is referenced by journal lines")
        update["archived"] = body.archived
    await accts(user).update_one({"code": code}, {"$set": update})
    updated = await accts(user).find_one({"code": code})
    updated.pop("_id", None)
    return updated


# ---------------- Journal Entries ----------------
@router.get("/journal-entries")
async def list_entries(
    user: dict = Depends(require("ledger:read")),
    status: Optional[str] = None, source: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    account: Optional[str] = None, limit: int = 200,
):
    q = {}
    if status:
        q["status"] = status
    if source:
        q["source"] = source
    if date_from or date_to:
        q["entry_date"] = {}
        if date_from:
            q["entry_date"]["$gte"] = date_from
        if date_to:
            q["entry_date"]["$lte"] = date_to
    if account:
        ids = {ln["entry_id"] for ln in await lines(user).find_list({"account_code": account})}
        q["id"] = {"$in": list(ids)}
    rows = await entries(user).find_list(q, sort=[("entry_date", -1), ("created_at", -1)], limit=limit)
    return rows


@router.get("/journal-entries/{entry_id}")
async def get_entry(entry_id: str, user: dict = Depends(require("ledger:read"))):
    e = await entries(user).find_one({"id": entry_id})
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    payload = {k: v for k, v in e.items() if k != "_id"}
    payload["lines"] = await lines(user).find_list({"entry_id": entry_id}, sort=[("created_at", 1)])
    return payload


class LineIn(BaseModel):
    account_code: str
    debit: float = 0
    credit: float = 0
    memo: Optional[str] = None


class EntryBody(BaseModel):
    entry_date: Optional[str] = None
    memo: Optional[str] = None
    lines: List[LineIn]
    post: bool = False


@router.post("/journal-entries")
async def create_entry(body: EntryBody, user: dict = Depends(require("ledger:post_draft"))):
    draft_lines = [{"account_code": l.account_code, "debit": to_cents(l.debit),
                    "credit": to_cents(l.credit), "memo": l.memo} for l in body.lines]
    status = "posted" if body.post else "draft"
    if status == "posted" and user["role"] not in ("owner",):
        # bookkeepers create drafts; only owner can post directly
        status = "draft"
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": body.entry_date or today_str(), "source": "manual",
        "memo": body.memo, "lines": draft_lines,
    }, status=status)
    return entry


@router.post("/journal-entries/{entry_id}/post")
async def post_entry(entry_id: str, user: dict = Depends(require("ledger:approve"))):
    return await L.post_draft(db, user["tenant_id"], user, entry_id)


@router.post("/journal-entries/{entry_id}/reverse")
async def reverse(entry_id: str, user: dict = Depends(require("ledger:approve"))):
    return await L.reverse_entry(db, user["tenant_id"], user, entry_id)


# ---------------- Reports ----------------
async def _target_currency(user: dict, currency: Optional[str]) -> str:
    """Resolve the report target currency: explicit ?currency= wins, else the
    workspace base_currency. Raises 422 on unknown codes."""
    from lib.currencies import normalize as _norm, get_base_currency
    if currency:
        try:
            return _norm(currency)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unsupported currency: {currency!r}")
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0, "settings": 1})
    return get_base_currency((tenant or {}).get("settings"))


@router.get("/trial-balance")
async def trial_balance(as_of: Optional[str] = None, currency: Optional[str] = None,
                        user: dict = Depends(require("ledger:read"))):
    return await L.trial_balance(db, user["tenant_id"], as_of or today_str(),
                                 target_currency=await _target_currency(user, currency))


@router.get("/balance-sheet")
async def balance_sheet(as_of: Optional[str] = None, currency: Optional[str] = None,
                        user: dict = Depends(require("ledger:read"))):
    return await L.balance_sheet(db, user["tenant_id"], as_of or today_str(),
                                 target_currency=await _target_currency(user, currency))


@router.get("/pnl")
async def pnl(date_from: Optional[str] = None, date_to: Optional[str] = None,
              currency: Optional[str] = None, user: dict = Depends(require("ledger:read"))):
    return await L.pnl(db, user["tenant_id"], date_from or "2000-01-01",
                       date_to or today_str(),
                       target_currency=await _target_currency(user, currency))


@router.get("/general-ledger")
async def general_ledger(account: str, date_from: Optional[str] = None,
                         date_to: Optional[str] = None, user: dict = Depends(require("ledger:read"))):
    if account not in ACCOUNT_TYPE_OF:
        # allow custom accounts too
        if not await accts(user).find_one({"code": account}):
            raise HTTPException(status_code=404, detail="Account not found")
    return await L.general_ledger(db, user["tenant_id"], account, date_from, date_to or today_str())


# ---------------- Fiscal periods ----------------
@router.get("/periods")
async def list_periods(user: dict = Depends(require("ledger:read"))):
    return await Scoped(db.fiscal_periods, user["tenant_id"]).find_list(sort=[("key", -1)])


class PeriodAction(BaseModel):
    status: str  # open | soft_closed | locked


@router.post("/periods/{key}/status")
async def set_period_status(key: str, body: PeriodAction, user: dict = Depends(require("ledger:admin"))):
    if body.status not in ("open", "soft_closed", "locked"):
        raise HTTPException(status_code=422, detail="Invalid status")
    s = Scoped(db.fiscal_periods, user["tenant_id"])
    p = await s.find_one({"key": key})
    if not p:
        await L.ensure_period(db, user["tenant_id"], f"{key}-01")
    upd = {"status": body.status}
    if body.status == "locked":
        upd["locked_by"] = user["email"]
        upd["locked_at"] = now_iso()
    await s.update_one({"key": key}, {"$set": upd})
    await L.audit(db, user["tenant_id"], user, f"period.{body.status}", {"period": key})
    result = await s.find_one({"key": key})
    result.pop("_id", None)
    return result


# ---------------- Opening balances migration (idempotent) ----------------
class OpeningBody(BaseModel):
    cash_register: float = 0
    cash_bank: float = 0
    accounts_payable: float = 0
    as_of: Optional[str] = None


@router.post("/opening-balances")
async def opening_balances(body: OpeningBody, user: dict = Depends(require("ledger:admin"))):
    tenant_id = user["tenant_id"]
    marker = await db.migration_markers.find_one({"tenant_id": tenant_id, "type": "opening_balances"})
    if marker and marker.get("status") == "complete":
        raise HTTPException(status_code=409, detail="Opening balances already migrated")

    # auto-compute inventory + AR from operational data
    prods = await Scoped(db.products, tenant_id).find_list()
    inv_retail = sum(int(p.get("quantity_on_hand", 0)) * int(p.get("unit_cost_cents", 0))
                     for p in prods if p.get("type") == "retail")
    inv_supply = sum(int(p.get("quantity_on_hand", 0)) * int(p.get("unit_cost_cents", 0))
                     for p in prods if p.get("type") == "supply")
    ar = 0
    for inv in await Scoped(db.invoices, tenant_id).find_list({"status": "issued"}):
        ar += int(inv.get("total_cents", 0))

    snapshot = {
        "1010": to_cents(body.cash_register),
        "1020": to_cents(body.cash_bank),
        "1200": ar,
        "1300": inv_retail,
        "1310": inv_supply,
        "2010": to_cents(body.accounts_payable),
    }
    draft = P.build_opening_balances(
        snapshot=snapshot, entry_date=body.as_of or today_str(),
        memo="Opening balances migration (automated). Equity is the balancing plug.",
    )
    entry = await L.post_journal_entry(db, tenant_id, user, draft, status="posted")
    tb = await L.trial_balance(db, tenant_id, entry["entry_date"])
    if not tb["balanced"]:
        raise HTTPException(status_code=500, detail="Opening balances did not balance")
    await db.migration_markers.insert_one({
        "tenant_id": tenant_id, "type": "opening_balances", "status": "complete",
        "entry_id": entry["id"], "at": now_iso(),
    })
    return {"entry": entry, "trial_balance_balanced": tb["balanced"]}


# ---------------- Year-end close ----------------
class YearCloseBody(BaseModel):
    year: str


@router.get("/year-close/status")
async def year_close_status(user: dict = Depends(require("ledger:read"))):
    rows = await db.migration_markers.find(
        {"tenant_id": user["tenant_id"], "type": {"$regex": "^year_close:"}}, {"_id": 0}
    ).to_list(50)
    return [{"year": r["type"].split(":")[1], "entry_id": r.get("entry_id"), "at": r.get("at")} for r in rows]


@router.post("/year-close")
async def year_close(body: YearCloseBody, user: dict = Depends(require("ledger:admin"))):
    tenant_id = user["tenant_id"]
    year = body.year
    mtype = f"year_close:{year}"
    if await db.migration_markers.find_one({"tenant_id": tenant_id, "type": mtype}):
        raise HTTPException(status_code=409, detail=f"Fiscal year {year} is already closed")

    as_of = f"{year}-12-31"
    date_from = f"{year}-01-01"
    bals = await L._balances(db, tenant_id, as_of=as_of, date_from=date_from)
    before = await L.trial_balance(db, tenant_id, as_of)

    lines = []
    net_income = 0
    for code, v in bals.items():
        t = ACCOUNT_TYPE_OF.get(code)
        if t not in ("revenue", "expense"):
            continue
        net_debit = v["debit"] - v["credit"]
        if net_debit == 0:
            continue
        if net_debit > 0:  # expense balance -> credit to zero it
            lines.append(P.cr(code, net_debit, f"Close FY{year}"))
            net_income -= net_debit
        else:              # revenue balance -> debit to zero it
            lines.append(P.dr(code, -net_debit, f"Close FY{year}"))
            net_income += -net_debit
    if not lines:
        raise HTTPException(status_code=422, detail=f"No revenue or expense activity to close for {year}")
    if net_income > 0:
        lines.append(P.cr("3100", net_income, "Net income to Retained Earnings"))
    elif net_income < 0:
        lines.append(P.dr("3100", -net_income, "Net loss to Retained Earnings"))

    entry = await L.post_journal_entry(db, tenant_id, user, {
        "entry_date": as_of, "source": "year_close",
        "memo": f"Fiscal year-end close {year}", "lines": lines,
    }, status="posted")

    periods = Scoped(db.fiscal_periods, tenant_id)
    for m in range(1, 13):
        key = f"{year}-{m:02d}"
        await L.ensure_period(db, tenant_id, f"{key}-01")
        await periods.update_one({"key": key}, {"$set": {
            "status": "locked", "year_closed": True,
            "locked_by": user["email"], "locked_at": now_iso(),
        }})

    await db.migration_markers.insert_one({
        "tenant_id": tenant_id, "type": mtype, "status": "complete",
        "entry_id": entry["id"], "at": now_iso(),
    })
    after = await L.trial_balance(db, tenant_id, as_of)
    await L.audit(db, tenant_id, user, "year.closed", {"year": year, "net_income_cents": net_income})
    return {"entry": entry, "net_income_cents": net_income, "before": before, "after": after}
