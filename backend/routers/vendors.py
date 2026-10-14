"""Vendors, direct expenses (with receipt metadata), vendor credits, and 1099 reporting.
All expense/credit events post exactly one balanced journal entry into the shared ledger."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib.coa import ACCOUNT_TYPE_OF, NAME_OF
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/vendors", tags=["vendors"])


def vendors(user):
    return Scoped(db.vendors, user["tenant_id"])


def expenses(user):
    return Scoped(db.expenses, user["tenant_id"])


def credits(user):
    return Scoped(db.vendor_credits, user["tenant_id"])


# ---------------- Vendors ----------------
class VendorBody(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    is_1099: bool = False
    tax_id: Optional[str] = None
    default_expense_code: str = "5700"


@router.get("")
async def list_vendors(user: dict = Depends(get_current_user)):
    return await vendors(user).find_list(sort=[("name", 1)], limit=500)


@router.post("")
async def create_vendor(body: VendorBody, user: dict = Depends(require("ops:manage"))):
    doc = {"id": new_id(), **body.model_dump(), "created_at": now_iso()}
    await vendors(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.patch("/{vendor_id}")
async def update_vendor(vendor_id: str, body: VendorBody, user: dict = Depends(require("ops:manage"))):
    v = await vendors(user).find_one({"id": vendor_id})
    if not v:
        raise HTTPException(status_code=404, detail="Vendor not found")
    await vendors(user).update_one({"id": vendor_id}, {"$set": body.model_dump()})
    return {"id": vendor_id, **body.model_dump()}


# ---------------- Direct expenses ----------------
class ReceiptMeta(BaseModel):
    merchant: Optional[str] = None
    receipt_date: Optional[str] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    filename: Optional[str] = None


class ExpenseBody(BaseModel):
    vendor: str
    vendor_id: Optional[str] = None
    category_code: str = "5700"
    amount: float
    payment_method: str = "bank"          # cash | card | bank
    expense_date: Optional[str] = None
    description: str = ""
    billable_job_id: Optional[str] = None
    receipt: Optional[ReceiptMeta] = None


@router.get("/expenses/list")
async def list_expenses(user: dict = Depends(require("ledger:read"))):
    return await expenses(user).find_list(sort=[("created_at", -1)], limit=500)


@router.post("/expenses")
async def create_expense(body: ExpenseBody, user: dict = Depends(require("ops:manage"))):
    if body.category_code not in ACCOUNT_TYPE_OF:
        raise HTTPException(status_code=422, detail="Unknown category account")
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    cash = P._cash_account(body.payment_method)
    exp_id = new_id()
    edate = body.expense_date or today_str()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": edate, "source": "expense", "source_ref": exp_id,
        "memo": f"Expense — {body.vendor}: {body.description}".strip(),
        "lines": [P.dr(body.category_code, amount, body.description or body.vendor),
                  P.cr(cash, amount, "Paid")],
    }, status="posted")
    doc = {
        "id": exp_id, "vendor": body.vendor, "vendor_id": body.vendor_id,
        "category_code": body.category_code, "category_name": NAME_OF.get(body.category_code),
        "amount_cents": amount, "payment_method": body.payment_method, "expense_date": edate,
        "description": body.description, "billable_job_id": body.billable_job_id,
        "receipt": body.receipt.model_dump() if body.receipt else None,
        "entry_number": entry["number"], "created_at": now_iso(),
    }
    await expenses(user).insert_one(doc)
    # if tied to a job, record it as a job cost too (no extra GL posting; cost already hit the expense account)
    if body.billable_job_id:
        job = await Scoped(db.jobs, user["tenant_id"]).find_one({"id": body.billable_job_id})
        if job:
            await Scoped(db.job_costs, user["tenant_id"]).insert_one({
                "id": new_id(), "job_id": body.billable_job_id, "job_number": job["job_number"],
                "category": "other", "description": f"{body.vendor}: {body.description}",
                "amount_cents": amount, "hours": 0, "rate": 0, "product_id": None, "qty": 1,
                "technician_id": None, "entry_number": entry["number"], "created_at": now_iso(),
            })
    doc.pop("_id", None)
    return doc


# ---------------- Vendor credits ----------------
class CreditBody(BaseModel):
    vendor: str
    vendor_id: Optional[str] = None
    amount: float
    expense_code: str = "5700"
    memo: str = ""


@router.get("/credits/list")
async def list_credits(user: dict = Depends(require("ledger:read"))):
    return await credits(user).find_list(sort=[("created_at", -1)], limit=300)


@router.post("/credits")
async def create_credit(body: CreditBody, user: dict = Depends(require("ops:manage"))):
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    cid = new_id()
    # reduce AP, reverse the expense (vendor owes you / future offset)
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "vendor_credit", "source_ref": cid,
        "memo": f"Vendor credit — {body.vendor}: {body.memo}".strip(),
        "lines": [P.dr("2010", amount, "AP reduced by credit"),
                  P.cr(body.expense_code, amount, "Expense reversed")],
    }, status="posted")
    doc = {"id": cid, "vendor": body.vendor, "vendor_id": body.vendor_id,
           "amount_cents": amount, "remaining_cents": amount, "expense_code": body.expense_code,
           "memo": body.memo, "entry_number": entry["number"], "created_at": now_iso()}
    await credits(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


# ---------------- 1099 report ----------------
@router.get("/1099")
async def report_1099(year: str, user: dict = Depends(require("ledger:read"))):
    tenant_id = user["tenant_id"]
    vends = await vendors(user).find_list({"is_1099": True})
    out = []
    for v in vends:
        paid = 0
        # bills paid in year
        for b in await Scoped(db.bills, tenant_id).find_list({"vendor": v["name"]}):
            for pmt in b.get("payments", []):
                if (pmt.get("date") or "")[:4] == year:
                    paid += pmt.get("cash_cents", 0)
        # direct expenses in year
        for e in await expenses(user).find_list({"vendor": v["name"]}):
            if (e.get("expense_date") or "")[:4] == year:
                paid += e.get("amount_cents", 0)
        out.append({"vendor_id": v["id"], "name": v["name"], "tax_id": v.get("tax_id"),
                    "paid_cents": paid, "paid": cents_to_dollars(paid),
                    "reportable": paid >= 60000})   # $600 IRS threshold
    out.sort(key=lambda x: -x["paid_cents"])
    return {"year": year, "threshold": 600.0, "vendors": out,
            "total": cents_to_dollars(sum(v["paid_cents"] for v in out))}
