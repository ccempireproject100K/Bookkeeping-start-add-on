"""Owner-friendly bookkeeping operations that complete the ledger:
overview KPIs + books-health, sales-tax remittance, tips payout, owner draws/contributions,
month-end close/lock, and bank CSV import + match + reconcile. All post balanced journal entries."""

import csv
import io
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lib.audit import audit
from lib.auth import Principal, require
from lib.bookkeeping import (ACCOUNT_NAME, ACCOUNT_TYPE, MONEY_ACCT, account_balances, assert_open, closed_through,
                             ensure_accounts, post_entry, _f)
from lib.dates import now_iso, today_iso
from lib.db import db
from lib.money import money

router = APIRouter(prefix="/books")
DateStr = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")


class PayoutIn(BaseModel):
    amount: float = Field(gt=0)
    method: str = "bank"
    date: str = DateStr
    note: str = Field(default="", max_length=300)


class EquityIn(BaseModel):
    kind: str  # draw | contribution
    amount: float = Field(gt=0)
    method: str = "bank"
    date: str = DateStr
    note: str = Field(default="", max_length=300)


class CloseIn(BaseModel):
    through: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class BankImportIn(BaseModel):
    account: str = "1010"
    csv: str = Field(min_length=1, max_length=2_000_000)


class BankResolveIn(BaseModel):
    action: str  # ignore | income | expense
    category: str = Field(default="", max_length=80)


def _today(p: Principal) -> str:
    return today_iso(p.settings.get("timezone"))


# ---------- Owner overview + books health ----------
@router.get("/overview")
async def overview(p: Principal = Depends(require("report:read"))):
    await ensure_accounts(p)
    today = _today(p)
    mstart = today[:8] + "01"
    allb = await account_balances(p, end=today)
    mtd = await account_balances(p, start=mstart, end=today)
    cash = _f(allb.get("1000", 0) + allb.get("1010", 0))
    ar = _f(allb.get("1100", 0))
    inv = _f(allb.get("1200", 0))
    tax_owed = _f(-allb.get("2100", 0))
    tips_owed = _f(-allb.get("2200", 0))
    ap = _f(-allb.get("2000", 0))
    income = sum(-v for c, v in mtd.items() if ACCOUNT_TYPE.get(c) == "income")
    expense = sum(v for c, v in mtd.items() if ACCOUNT_TYPE.get(c) == "expense")
    top = sorted(((ACCOUNT_NAME.get(c, c), _f(v)) for c, v in mtd.items() if ACCOUNT_TYPE.get(c) == "expense" and v > 0),
                 key=lambda x: -x[1])[:5]
    # health: trial balance, inventory & A/R reconcile
    td = sum(max(v, 0) for v in allb.values())
    tc = sum(max(-v, 0) for v in allb.values())
    from routers.bookkeeping import inventory_valuation, ar_aging
    iv = await inventory_valuation(p=p)
    aging = await ar_aging(p=p)
    return {
        "currency": p.settings.get("currency", "USD"),
        "cash_on_hand": cash, "accounts_receivable": ar, "inventory_value": inv,
        "accounts_payable": ap, "tax_owed": tax_owed, "tips_owed": tips_owed,
        "income_mtd": _f(income), "expenses_mtd": _f(expense), "net_profit_mtd": _f(income - expense),
        "top_expenses": [{"name": n, "amount": a} for n, a in top],
        "closed_through": await closed_through(p),
        "health": {"trial_balanced": td == tc, "inventory_reconciled": iv.reconciled, "ar_reconciled": aging.reconciled},
    }


# ---------- Sales tax ----------
@router.get("/tax-summary")
async def tax_summary(start: str = "", end: str = "", p: Principal = Depends(require("report:read"))):
    today = _today(p)
    start, end = start or today[:8] + "01", end or today
    period = await account_balances(p, start=start, end=end)
    allb = await account_balances(p, end=end)
    return {"start": start, "end": end, "collected": _f(-period.get("2100", 0)) if period.get("2100", 0) < 0 else 0.0,
            "net_change": _f(-period.get("2100", 0)), "owed": _f(-allb.get("2100", 0))}


@router.post("/tax/remit")
async def tax_remit(body: PayoutIn, p: Principal = Depends(require("txn:adjust"))):
    d = body.date or _today(p)
    await assert_open(p, d)
    amount = money(body.amount)
    acct = MONEY_ACCT.get(body.method, "1010")
    await post_entry(p, d, f"Sales tax remittance {body.note}".strip(), [("2100", amount), (acct, -amount)],
                     source="tax", ref=f"tax:{uuid.uuid4().hex}")
    await audit(p, "tax.remit", "journal", None, {"amount": amount})
    return {"ok": True, "amount": amount}


# ---------- Tips payout ----------
@router.get("/tips-summary")
async def tips_summary(p: Principal = Depends(require("report:read"))):
    allb = await account_balances(p, end=_today(p))
    return {"owed": _f(-allb.get("2200", 0))}


@router.post("/tips/payout")
async def tips_payout(body: PayoutIn, p: Principal = Depends(require("txn:adjust"))):
    d = body.date or _today(p)
    await assert_open(p, d)
    amount = money(body.amount)
    acct = MONEY_ACCT.get(body.method, "1000")
    await post_entry(p, d, f"Tips paid out {body.note}".strip(), [("2200", amount), (acct, -amount)],
                     source="tips", ref=f"tips:{uuid.uuid4().hex}")
    await audit(p, "tips.payout", "journal", None, {"amount": amount})
    return {"ok": True, "amount": amount}


# ---------- Owner equity ----------
@router.post("/equity")
async def equity(body: EquityIn, p: Principal = Depends(require("settings:write"))):
    if body.kind not in ("draw", "contribution"):
        raise HTTPException(400, "kind must be draw or contribution")
    d = body.date or _today(p)
    await assert_open(p, d)
    amount = money(body.amount)
    acct = MONEY_ACCT.get(body.method, "1010")
    signed = [("3000", -amount), (acct, amount)] if body.kind == "contribution" else [("3000", amount), (acct, -amount)]
    await post_entry(p, d, f"Owner {body.kind} {body.note}".strip(), signed, source="equity", ref=f"equity:{uuid.uuid4().hex}")
    await audit(p, f"equity.{body.kind}", "journal", None, {"amount": amount})
    return {"ok": True, "amount": amount, "kind": body.kind}


# ---------- Period close ----------
@router.get("/close")
async def get_close(p: Principal = Depends(require("report:read"))):
    return {"closed_through": await closed_through(p)}


@router.post("/close")
async def set_close(body: CloseIn, p: Principal = Depends(require("settings:write"))):
    await db.book_closes.update_one({"tenant_id": p.tenant_id}, {"$set": {"through": body.through, "updated_at": now_iso()}}, upsert=True)
    await audit(p, "books.close", "tenant", p.tenant_id, {"through": body.through})
    return {"closed_through": body.through}


@router.post("/reopen")
async def reopen(p: Principal = Depends(require("settings:write"))):
    await db.book_closes.delete_one({"tenant_id": p.tenant_id})
    await audit(p, "books.reopen", "tenant", p.tenant_id, {})
    return {"closed_through": None}


# ---------- Bank import + match + reconcile ----------
@router.post("/bank/import")
async def bank_import(body: BankImportIn, p: Principal = Depends(require("txn:adjust"))):
    rows = list(csv.reader(io.StringIO(body.csv.strip())))
    if not rows:
        raise HTTPException(400, "Empty CSV")
    header = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None
    di, de, ai = col("date"), col("description", "memo", "payee"), col("amount")
    if di is None or ai is None:
        raise HTTPException(400, "CSV needs at least 'date' and 'amount' columns (optional 'description').")
    imported, dups = 0, 0
    for r in rows[1:]:
        if len(r) <= max(di, ai):
            continue
        try:
            amt = money(float(str(r[ai]).replace(",", "").replace("$", "")))
        except ValueError:
            continue
        d = (r[di] or "").strip()[:10]
        desc = (r[de].strip() if de is not None and len(r) > de else "")
        key = f"{d}|{amt}|{desc}"[:200]
        if await db.bank_txns.find_one({"tenant_id": p.tenant_id, "dedupe": key}):
            dups += 1
            continue
        # auto-match: a journal cash/bank line on the same date with the same amount
        side = "debit_cents" if amt > 0 else "credit_cents"
        cents = abs(int(round(amt * 100)))
        match = await db.journal_entries.find_one({"tenant_id": p.tenant_id, "date": d,
                                                   "lines": {"$elemMatch": {"account": {"$in": ["1000", body.account]}, side: cents}}}, {"_id": 0, "id": 1, "memo": 1})
        await db.bank_txns.insert_one({"tenant_id": p.tenant_id, "id": str(uuid.uuid4()), "account": body.account,
                                       "date": d, "description": desc, "amount": amt, "dedupe": key,
                                       "status": "matched" if match else "unmatched",
                                       "matched_entry": match["id"] if match else None, "created_at": now_iso()})
        imported += 1
    return {"imported": imported, "duplicates": dups}


@router.get("/bank")
async def bank_list(status: str = "", p: Principal = Depends(require("report:read"))):
    f: dict = {"tenant_id": p.tenant_id}
    if status:
        f["status"] = status
    docs = await db.bank_txns.find(f, {"_id": 0, "tenant_id": 0, "dedupe": 0}).sort("date", -1).to_list(2000)
    return docs


@router.post("/bank/{txn_id}/resolve")
async def bank_resolve(txn_id: str, body: BankResolveIn, p: Principal = Depends(require("txn:adjust"))):
    bt = await db.bank_txns.find_one({"tenant_id": p.tenant_id, "id": txn_id}, {"_id": 0})
    if not bt:
        raise HTTPException(404, "Bank line not found")
    if body.action == "ignore":
        await db.bank_txns.update_one({"tenant_id": p.tenant_id, "id": txn_id}, {"$set": {"status": "ignored"}})
        return {"ok": True}
    from lib.money import create_txn
    amt = money(abs(bt["amount"]))
    method = "bank" if bt["account"] == "1010" else "cash"
    if body.action == "income":
        await create_txn(p, "income", body.category or "Other income", amt, bt["date"], bt["description"] or "Bank import", payment_method=method)
    elif body.action == "expense":
        await create_txn(p, "expense", body.category or "Other", amt, bt["date"], bt["description"] or "Bank import", payment_method=method)
    else:
        raise HTTPException(400, "action must be ignore, income or expense")
    await db.bank_txns.update_one({"tenant_id": p.tenant_id, "id": txn_id}, {"$set": {"status": "reconciled"}})
    return {"ok": True}
