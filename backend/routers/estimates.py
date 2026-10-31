"""Estimates / quotes with estimate→invoice conversion, and recurring-invoice
templates (manual generate; no scheduler). Estimates do not touch the GL until
converted into an invoice."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P
from routers.invoices import InvoiceBody, InvLine, issue_invoice

router = APIRouter(prefix="/estimates", tags=["estimates"])


def coll(user):
    return Scoped(db.estimates, user["tenant_id"])


def recurring(user):
    return Scoped(db.recurring_invoices, user["tenant_id"])


class EstLine(BaseModel):
    kind: str = "service"
    name: str
    revenue_code: str = "4020"
    amount: float


class EstimateBody(BaseModel):
    client_name: str
    customer_id: Optional[str] = None
    lines: List[EstLine]
    tax: float = 0
    notes: str = ""
    # Bake-off #8: a deposit ask on the quote, with its own due date.
    deposit_amount: float = 0
    deposit_due_date: Optional[str] = None


@router.get("")
async def list_estimates(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("created_at", -1)], limit=500)


@router.post("")
async def create_estimate(body: EstimateBody, user: dict = Depends(require("ops:manage"))):
    subtotal = sum(to_cents(l.amount) for l in body.lines)
    tax = to_cents(body.tax)
    seq = await Scoped(db.estimates, user["tenant_id"]).next_seq(db.counters, "estimate")
    doc = {
        "id": new_id(), "number": f"EST-{seq:04d}", "client_name": body.client_name,
        "customer_id": body.customer_id, "lines": [l.model_dump() for l in body.lines],
        "subtotal_cents": subtotal, "tax_cents": tax, "total_cents": subtotal + tax,
        "total": cents_to_dollars(subtotal + tax), "status": "draft", "notes": body.notes,
        "deposit_cents": to_cents(body.deposit_amount),
        "deposit_due_date": body.deposit_due_date,
        "invoice_id": None, "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


class StatusBody(BaseModel):
    status: str   # draft | sent | accepted | declined


@router.patch("/{estimate_id}")
async def update_status(estimate_id: str, body: StatusBody, user: dict = Depends(require("ops:manage"))):
    if body.status not in ("draft", "sent", "accepted", "declined"):
        raise HTTPException(status_code=422, detail="Invalid status")
    e = await coll(user).find_one({"id": estimate_id})
    if not e:
        raise HTTPException(status_code=404, detail="Estimate not found")
    await coll(user).update_one({"id": estimate_id}, {"$set": {"status": body.status}})
    return {"status": body.status}


@router.post("/{estimate_id}/convert")
async def convert_to_invoice(estimate_id: str, user: dict = Depends(require("ops:manage"))):
    e = await coll(user).find_one({"id": estimate_id})
    if not e:
        raise HTTPException(status_code=404, detail="Estimate not found")
    if e.get("invoice_id"):
        raise HTTPException(status_code=409, detail="Estimate already converted")
    body = InvoiceBody(
        client_name=e["client_name"], customer_id=e.get("customer_id"),
        lines=[InvLine(**{k: li.get(k) for k in ("kind", "name", "revenue_code", "amount") if li.get(k) is not None})
               for li in e["lines"]],
        tax=cents_to_dollars(e.get("tax_cents", 0)),
    )
    inv = await issue_invoice(body, user)
    await coll(user).update_one({"id": estimate_id}, {"$set": {"status": "converted", "invoice_id": inv["id"]}})
    # Bake-off #8: held deposits for this quote apply to the new invoice,
    # restating its balance (paid_cents/status/payments move together).
    applied = await _apply_deposits(user, estimate_id, inv["id"])
    if applied:
        fresh = await Scoped(db.invoices, user["tenant_id"]).find_one({"id": inv["id"]})
        if fresh:
            fresh["deposits_applied_cents"] = applied
            fresh["balance_cents"] = fresh["total_cents"] - fresh.get("paid_cents", 0)
            fresh["balance"] = cents_to_dollars(fresh["balance_cents"])
            return fresh
    return inv


# ---------------- Customer deposits (bake-off #8) ----------------
# Money taken before work starts is a LIABILITY (2200 Unearned Revenue)
# until the invoice exists; applying it settles AR (DR 2200 / CR 1200).
DEPOSIT_CODE = "2200"


def deposits(user):
    return Scoped(db.customer_deposits, user["tenant_id"])


class DepositBody(BaseModel):
    amount: float
    payment_method: str = "bank"     # cash | card | bank
    pay_date: Optional[str] = None


@router.post("/{estimate_id}/deposits")
async def record_deposit(estimate_id: str, body: DepositBody,
                         user: dict = Depends(require("ops:manage"))):
    e = await coll(user).find_one({"id": estimate_id})
    if not e:
        raise HTTPException(status_code=404, detail="Estimate not found")
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Deposit amount must be positive")
    cash = P._cash_account(body.payment_method)
    dep_id = new_id()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": body.pay_date or today_str(), "source": "deposit",
        "source_ref": dep_id,
        "memo": f"Deposit — {e['client_name']} ({e.get('number')})",
        "lines": [P.dr(cash, amount, "Deposit received"),
                  P.cr(DEPOSIT_CODE, amount, "Customer deposit held")],
    }, status="posted")
    doc = {"id": dep_id, "estimate_id": estimate_id,
           "customer_id": e.get("customer_id"), "client_name": e["client_name"],
           "amount_cents": amount, "remaining_cents": amount, "status": "held",
           "payment_method": body.payment_method,
           "entry_number": entry["number"], "created_at": now_iso()}
    await deposits(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/{estimate_id}/deposits")
async def list_deposits(estimate_id: str, user: dict = Depends(require("ledger:read"))):
    return await deposits(user).find_list({"estimate_id": estimate_id},
                                          sort=[("created_at", 1)], limit=200)


async def _apply_deposits(user, estimate_id: str, invoice_id: str) -> int:
    """Apply held deposits to a freshly converted invoice. Returns cents applied."""
    inv_coll = Scoped(db.invoices, user["tenant_id"])
    inv = await inv_coll.find_one({"id": invoice_id})
    if not inv:
        return 0
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    applied_total = 0
    for dep in await deposits(user).find_list({"estimate_id": estimate_id, "status": "held"},
                                              sort=[("created_at", 1)], limit=100):
        if balance <= 0:
            break
        apply = min(dep["remaining_cents"], balance)
        if apply <= 0:
            continue
        entry = await L.post_journal_entry(db, user["tenant_id"], user, {
            "entry_date": today_str(), "source": "deposit_applied",
            "source_ref": invoice_id,
            "memo": f"Deposit applied — {inv['client_name']}",
            "lines": [P.dr(DEPOSIT_CODE, apply, "Deposit earned"),
                      P.cr("1200", apply, "AR settled by deposit")],
        }, status="posted")
        remaining = dep["remaining_cents"] - apply
        await deposits(user).update_one({"id": dep["id"]}, {"$set": {
            "remaining_cents": remaining,
            "status": "applied" if remaining == 0 else "held"}})
        pmt = {"date": today_str(), "amount_cents": apply, "cash_cents": 0,
               "credit_cents": 0, "method": "deposit",
               "entry": entry["number"], "deposit_id": dep["id"]}
        await inv_coll.update_one({"id": invoice_id},
                                  {"$push": {"payments": pmt}})
        applied_total += apply
        balance -= apply
    if applied_total:
        fresh = await inv_coll.find_one({"id": invoice_id})
        new_paid = fresh.get("paid_cents", 0) + applied_total
        status = "paid" if new_paid >= fresh["total_cents"] else "partial"
        await inv_coll.update_one({"id": invoice_id},
                                  {"$set": {"paid_cents": new_paid, "status": status}})
    return applied_total


# ---------------- Recurring invoices ----------------
class RecurringBody(BaseModel):
    client_name: str
    customer_id: Optional[str] = None
    lines: List[EstLine]
    tax: float = 0
    frequency: str = "monthly"       # weekly | monthly | quarterly
    terms: str = "Net 30"


@router.get("/recurring/list")
async def list_recurring(user: dict = Depends(get_current_user)):
    return await recurring(user).find_list(sort=[("created_at", -1)], limit=200)


@router.post("/recurring")
async def create_recurring(body: RecurringBody, user: dict = Depends(require("ops:manage"))):
    doc = {
        "id": new_id(), "client_name": body.client_name, "customer_id": body.customer_id,
        "lines": [l.model_dump() for l in body.lines], "tax": body.tax,
        "frequency": body.frequency, "terms": body.terms, "last_generated": None,
        "generated_count": 0, "created_at": now_iso(),
    }
    await recurring(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.post("/recurring/{rec_id}/generate")
async def generate_recurring(rec_id: str, user: dict = Depends(require("ops:manage"))):
    r = await recurring(user).find_one({"id": rec_id})
    if not r:
        raise HTTPException(status_code=404, detail="Recurring template not found")
    body = InvoiceBody(
        client_name=r["client_name"], customer_id=r.get("customer_id"),
        lines=[InvLine(**{k: li.get(k) for k in ("kind", "name", "revenue_code", "amount") if li.get(k) is not None})
               for li in r["lines"]],
        tax=r.get("tax", 0), terms=r.get("terms"),
    )
    inv = await issue_invoice(body, user)
    await recurring(user).update_one({"id": rec_id}, {"$set": {"last_generated": today_str()},
                                                      "$inc": {"generated_count": 1}})
    return inv
