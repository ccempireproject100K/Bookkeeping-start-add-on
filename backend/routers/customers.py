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


def _norm_name(s):
    """Normalize a customer name for comparison: trim, collapse whitespace, lowercase."""
    return " ".join((s or "").split()).lower()


def _norm_email(s):
    return (s or "").strip().lower()


def _norm_phone(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _invoice_matches_statement(inv, customer_id, name, name_unique_in_tenant):
    """Decide whether an invoice belongs on a customer's statement.

    The invoice's customer_id link is authoritative when present. The legacy
    client_name fallback is only used for unlinked invoices AND only when the
    name is unambiguous within the tenant — otherwise two same-name customers
    would each see the other's invoices.
    """
    inv_cid = inv.get("customer_id")
    if inv_cid:
        return inv_cid == customer_id
    return bool(name_unique_in_tenant) and inv.get("client_name") == name


def _find_duplicate(customers, name, email, phone):
    """Pure duplicate check over a customer list. Returns the matching doc or None.

    A duplicate is: same normalized name AND (same normalized email OR same
    normalized phone). Name alone is not enough — two different customers can
    share a name.
    """
    nname = _norm_name(name)
    nemail = _norm_email(email)
    nphone = _norm_phone(phone)
    if not nname or (not nemail and not nphone):
        return None
    for c in customers:
        if _norm_name(c.get("name")) != nname:
            continue
        if (nemail and _norm_email(c.get("email")) == nemail) or \
           (nphone and _norm_phone(c.get("phone")) == nphone):
            return c
    return None


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
    existing = await coll(user).find_list()
    dup = _find_duplicate(existing, body.name, body.email, body.phone)
    if dup:
        raise HTTPException(status_code=409, detail={
            "message": "Customer already exists",
            "existing_id": dup.get("id"),
            "existing_name": dup.get("name"),
        })
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
    # The legacy client_name fallback is only safe when no other customer in
    # this tenant shares the name; otherwise same-name customers would see
    # each other's invoices.
    tenant_customers = await coll(user).find_list()
    same_name = sum(1 for o in tenant_customers if _norm_name(o.get("name")) == _norm_name(name))
    name_unique = same_name <= 1
    events = []
    outstanding = 0
    for inv in invoices:
        if not _invoice_matches_statement(inv, customer_id, name, name_unique):
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
