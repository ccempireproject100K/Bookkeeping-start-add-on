"""Vendor bills (accounts payable sub-ledger). Each bill posts to the GL."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date, timedelta

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/bills", tags=["bills"])


def coll(user):
    return Scoped(db.bills, user["tenant_id"])


class BillBody(BaseModel):
    vendor: str
    amount: float
    expense_code: str = "5700"
    bill_date: Optional[str] = None
    due_date: Optional[str] = None


@router.get("")
async def list_bills(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("created_at", -1)], limit=300)


@router.post("")
async def create_bill(body: BillBody, user: dict = Depends(require("ops:manage"))):
    bdate = body.bill_date or today_str()
    due = body.due_date or (date.fromisoformat(bdate) + timedelta(days=30)).isoformat()
    amount = to_cents(body.amount)
    bill_id = new_id()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": bdate, "source": "bill", "source_ref": bill_id,
        "memo": f"Bill — {body.vendor}",
        "lines": [P.dr(body.expense_code, amount, body.vendor), P.cr("2010", amount, "Accounts payable")],
    }, status="posted")
    doc = {
        "id": bill_id, "vendor": body.vendor, "amount_cents": amount, "expense_code": body.expense_code,
        "bill_date": bdate, "due_date": due, "status": "open",
        "issue_entry": entry["number"], "paid_entry": None, "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


class PayBody(BaseModel):
    payment_method: str = "bank"


@router.post("/{bill_id}/pay")
async def pay_bill(bill_id: str, body: PayBody, user: dict = Depends(require("ops:manage"))):
    bill = await coll(user).find_one({"id": bill_id})
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill["status"] == "paid":
        raise HTTPException(status_code=409, detail="Bill already paid")
    cash = P._cash_account(body.payment_method)
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "ap_payment", "source_ref": bill_id,
        "memo": f"Pay bill — {bill['vendor']}",
        "lines": [P.dr("2010", bill["amount_cents"], "AP settled"), P.cr(cash, bill["amount_cents"], "Payment")],
    }, status="posted")
    await coll(user).update_one({"id": bill_id}, {"$set": {"status": "paid", "paid_entry": entry["number"], "paid_at": now_iso()}})
    return {"status": "paid", "paid_entry": entry["number"]}
