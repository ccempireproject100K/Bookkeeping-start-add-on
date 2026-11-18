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
from lib import periods as PC

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
    # Optional intercompany marking: this line is a deal with a sister
    # business (same owner). counterparty_tenant_id names that business.
    intercompany: bool = False
    counterparty_tenant_id: Optional[str] = None


class EntryBody(BaseModel):
    entry_date: Optional[str] = None
    memo: Optional[str] = None
    lines: List[LineIn]
    post: bool = False
    # Entry-level intercompany marking; lines inherit unless they set
    # their own. Used only by the consolidated multi-entity view.
    intercompany: bool = False
    counterparty_tenant_id: Optional[str] = None
    period_override_reason: Optional[str] = None


@router.post("/journal-entries")
async def create_entry(body: EntryBody, user: dict = Depends(require("ledger:post_draft"))):
    draft_lines = [{"account_code": l.account_code, "debit": to_cents(l.debit),
                    "credit": to_cents(l.credit), "memo": l.memo,
                    "intercompany": l.intercompany,
                    "counterparty_tenant_id": l.counterparty_tenant_id} for l in body.lines]
    status = "posted" if body.post else "draft"
    if status == "posted" and user["role"] not in ("owner",):
        # bookkeepers create drafts; only owner can post directly
        status = "draft"
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": body.entry_date or today_str(), "source": "manual",
        "memo": body.memo, "lines": draft_lines,
        "intercompany": body.intercompany,
        "counterparty_tenant_id": body.counterparty_tenant_id,
        "period_override_reason": body.period_override_reason,
    }, status=status)
    return entry


class PostEntryBody(BaseModel):
    period_override_reason: Optional[str] = None


@router.post("/journal-entries/{entry_id}/post")
async def post_entry(entry_id: str, body: Optional[PostEntryBody] = None,
                     user: dict = Depends(require("ledger:approve"))):
    if isinstance(body, dict) and "tenant_id" in body:
        # Direct-call compat: main's suites call post_entry(entry_id,
        # user) with the user dict in the body position (pre-override
        # signature); FastAPI always passes a PostEntryBody or None.
        user, body = body, None
    return await L.post_draft(db, user["tenant_id"], user, entry_id,
                              period_override_reason=body.period_override_reason if body else None)


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
    rows = await Scoped(db.fiscal_periods, user["tenant_id"]).find_list(sort=[("key", -1)])
    # Resolve time-based transitions (grace elapsed -> hard lock; reopen
    # window expired -> re-lock) so the list never shows a stale status.
    return [await L.resolve_period(db, user["tenant_id"], p) for p in rows]


class PeriodAction(BaseModel):
    status: str  # open | soft_closed | locked


@router.post("/periods/{key}/status")
async def set_period_status(key: str, body: PeriodAction, user: dict = Depends(require("ledger:admin"))):
    """Legacy status setter, kept for compatibility — it now delegates to
    the period-close workflow (lib/periods.py) so there is no silent way
    around the checklist, the snapshot, or the reopen rules."""
    if body.status not in ("open", "soft_closed", "locked"):
        raise HTTPException(status_code=422, detail="Invalid status")
    tid = user["tenant_id"]
    await L.ensure_period(db, tid, f"{key}-01")
    s = Scoped(db.fiscal_periods, tid)
    period = await L.resolve_period(db, tid, await s.find_one({"key": key}))
    reopen = period.get("reopen") or {}

    if body.status == "open":
        if period["status"] == "open" and reopen.get("status") != "active":
            return period
        settings = await PC.get_settings(db, tid)
        if settings["reopen_apparatus"]:
            raise HTTPException(
                status_code=409,
                detail=(f"Period {key} is closed. Reopen it via "
                        f"POST /ledger/periods/{key}/reopen — a written reason is required."))
        # Apparatus OFF: plain reopen, as the workflow itself would do.
        await s.update_one({"key": key}, {"$set": {"status": "open", "reopen": None}})
        await L._period_audit(db, tid, user, "period.reopened", key,
                              {"apparatus": False, "via": "legacy_status"})
        result = await s.find_one({"key": key})
        result.pop("_id", None)
        return result
    if body.status == "soft_closed":
        if period["status"] != "open" or reopen.get("status") == "active":
            return period
        result = await PC.close_period(db, tid, user, key, via="legacy_status")
        return result["period"]
    # locked
    # Bake-off #5 (main): a period cannot lock over a FAILED, unwaived
    # balance assertion whose statement date falls inside it — on any
    # lock path through this endpoint. Waive the assertion (named +
    # reasoned, audited) or fix the books first.
    if period.get("start_date") and period.get("end_date"):
        failed = []
        async for a in db.balance_assertions.find(
                {"tenant_id": tid,
                 "statement_date": {"$gte": period["start_date"],
                                    "$lte": period["end_date"]}},
                {"_id": 0}):
            if a.get("status") == "failed" and not a.get("waived"):
                failed.append({"id": a["id"],
                               "bank_account_id": a.get("bank_account_id"),
                               "statement_date": a.get("statement_date"),
                               "difference_cents": a.get("difference_cents")})
        if failed:
            raise HTTPException(status_code=409, detail={
                "error": "Period has failed balance assertions",
                "failed_assertions": failed})
    if period["status"] == "locked" and reopen.get("status") != "active":
        return period
    if period["status"] == "soft_closed" and reopen.get("status") != "active":
        return await PC.lock_now(db, tid, user, key)
    result = await PC.close_period(db, tid, user, key, via="legacy_status", force_hard=True)
    return result["period"]


# ---------------- Period-close workflow (Top-20 #19) ----------------
class PeriodSettingsBody(BaseModel):
    snapshot_on_close: Optional[bool] = None
    soft_close_stage: Optional[bool] = None
    reopen_apparatus: Optional[bool] = None
    grace_days: Optional[int] = None
    reopen_window_hours: Optional[int] = None


@router.get("/period-settings")
async def get_period_settings(user: dict = Depends(require("ledger:read"))):
    return await PC.get_settings(db, user["tenant_id"])


@router.put("/period-settings")
async def put_period_settings(body: PeriodSettingsBody,
                              user: dict = Depends(require("settings:write"))):
    return await PC.update_settings(db, user["tenant_id"], user,
                                    body.model_dump(exclude_none=True))


@router.get("/periods/{key}/checklist")
async def period_checklist(key: str, user: dict = Depends(require("ledger:read"))):
    return await PC.compute_checklist(db, user["tenant_id"], key)


class CloseBody(BaseModel):
    acknowledge: List[str] = []
    grace_days: Optional[int] = None


@router.post("/periods/{key}/close")
async def close_period(key: str, body: CloseBody,
                       user: dict = Depends(require("ledger:admin"))):
    return await PC.close_period(db, user["tenant_id"], user, key,
                                 acknowledge=body.acknowledge,
                                 grace_days=body.grace_days)


@router.post("/periods/{key}/lock")
async def lock_period(key: str, user: dict = Depends(require("ledger:admin"))):
    return await PC.lock_now(db, user["tenant_id"], user, key)


class ReopenBody(BaseModel):
    reason: Optional[str] = None
    window_hours: Optional[int] = None


@router.post("/periods/{key}/reopen")
async def reopen_period(key: str, body: ReopenBody,
                        user: dict = Depends(require("period:reopen"))):
    return await PC.reopen_period(db, user["tenant_id"], user, key,
                                  reason=body.reason, window_hours=body.window_hours)


@router.post("/periods/{key}/reopen/end")
async def end_reopen(key: str, user: dict = Depends(require("period:reopen"))):
    return await PC.end_reopen(db, user["tenant_id"], user, key)


@router.get("/periods/{key}/snapshots")
async def period_snapshots(key: str, user: dict = Depends(require("ledger:read"))):
    return await PC.list_snapshots(db, user["tenant_id"], key)


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
