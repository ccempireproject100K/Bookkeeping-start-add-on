"""Customers & service locations."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import require, get_current_user

router = APIRouter(prefix="/customers", tags=["customers"])


def coll(user):
    return Scoped(db.customers, user["tenant_id"])


class Location(BaseModel):
    label: str = "Primary"
    address: str


class CustomerBody(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    locations: List[Location] = []


@router.get("")
async def list_customers(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("name", 1)], limit=500)


@router.post("")
async def create_customer(body: CustomerBody, user: dict = Depends(require("ops:manage"))):
    doc = {
        "id": new_id(), "name": body.name, "email": body.email, "phone": body.phone,
        "locations": [l.model_dump() for l in body.locations], "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/{customer_id}")
async def get_customer(customer_id: str, user: dict = Depends(get_current_user)):
    c = await coll(user).find_one({"id": customer_id})
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {k: v for k, v in c.items() if k != "_id"}


@router.get("/{customer_id}/statement")
async def statement(customer_id: str, user: dict = Depends(require("ledger:read"))):
    from lib.base import cents_to_dollars
    c = await coll(user).find_one({"id": customer_id})
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    name = c["name"]
    invoices = await Scoped(db.invoices, user["tenant_id"]).find_list()
    events = []
    outstanding = 0
    for inv in invoices:
        if inv.get("customer_id") != customer_id and inv.get("client_name") != name:
            continue
        events.append({"date": (inv.get("created_at") or "")[:10], "type": "Invoice",
                       "ref": inv.get("issue_entry"), "charge_cents": inv["total_cents"],
                       "payment_cents": 0})
        paid = inv.get("paid_cents", inv["total_cents"] if inv.get("status") == "paid" else 0)
        for pmt in inv.get("payments", []):
            events.append({"date": pmt.get("date"), "type": "Payment", "ref": pmt.get("entry"),
                           "charge_cents": 0, "payment_cents": pmt.get("amount_cents", 0)})
        outstanding += inv["total_cents"] - paid
    events.sort(key=lambda e: (e["date"] or "", e["type"]))
    running = 0
    for e in events:
        running += e["charge_cents"] - e["payment_cents"]
        e["balance"] = cents_to_dollars(running)
        e["charge"] = cents_to_dollars(e["charge_cents"])
        e["payment"] = cents_to_dollars(e["payment_cents"])
    return {"customer": {"id": customer_id, "name": name, "email": c.get("email"), "phone": c.get("phone")},
            "events": events, "outstanding": cents_to_dollars(outstanding), "outstanding_cents": outstanding}
