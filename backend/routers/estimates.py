"""Estimates / quotes with estimate→invoice conversion, and recurring-invoice
templates (manual generate; no scheduler). Estimates do not touch the GL until
converted into an invoice."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
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
    return inv


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
