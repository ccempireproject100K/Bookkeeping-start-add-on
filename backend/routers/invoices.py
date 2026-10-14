"""Invoices: issue (AR) and pay. Each transition posts a balanced journal entry."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/invoices", tags=["invoices"])


def coll(user):
    return Scoped(db.invoices, user["tenant_id"])


class InvLine(BaseModel):
    kind: str = "service"            # service | product
    name: str
    revenue_code: str = "4020"
    amount: float


class InvoiceBody(BaseModel):
    client_name: str
    lines: List[InvLine]
    tax: float = 0
    due_date: Optional[str] = None


@router.get("")
async def list_invoices(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("created_at", -1)], limit=200)


@router.post("")
async def issue_invoice(body: InvoiceBody, user: dict = Depends(require("ops:manage"))):
    rev_lines = []
    subtotal = 0
    for ln in body.lines:
        amt = to_cents(ln.amount)
        subtotal += amt
        rev_lines.append({"revenue_code": ln.revenue_code, "amount_cents": amt})
    tax_cents = to_cents(body.tax)
    gross = subtotal + tax_cents
    inv_id = new_id()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, P.build_invoice_issue(
        revenue_lines=rev_lines, tax_cents=tax_cents, cogs_cents=0,
        gross_cents=gross, entry_date=today_str(), source_ref=inv_id,
        memo=f"Invoice — {body.client_name}",
    ), status="posted")
    doc = {
        "id": inv_id, "client_name": body.client_name,
        "lines": [l.model_dump() for l in body.lines],
        "subtotal_cents": subtotal, "tax_cents": tax_cents, "total_cents": gross,
        "status": "issued", "due_date": body.due_date, "issue_entry": entry["number"],
        "paid_entry": None, "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


class PayBody(BaseModel):
    payment_method: str = "bank"


@router.post("/{invoice_id}/pay")
async def pay_invoice(invoice_id: str, body: PayBody, user: dict = Depends(require("ops:manage"))):
    inv = await coll(user).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv["status"] == "paid":
        raise HTTPException(status_code=409, detail="Invoice already paid")
    entry = await L.post_journal_entry(db, user["tenant_id"], user, P.build_invoice_paid(
        amount_cents=inv["total_cents"], payment_method=body.payment_method,
        entry_date=today_str(), source_ref=invoice_id,
        memo=f"Payment — {inv['client_name']}",
    ), status="posted")
    await coll(user).update_one({"id": invoice_id}, {"$set": {
        "status": "paid", "paid_entry": entry["number"], "paid_at": now_iso(),
    }})
    return {"status": "paid", "paid_entry": entry["number"]}
