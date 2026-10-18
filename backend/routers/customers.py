"""Customers & service locations."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import require, get_current_user
from lib import idempotency as idem
from lib.integration_auth import actor_label, get_sync_actor

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


def _is_deleted(c):
    """Soft-delete marker: DELETE stamps deleted_at (+ active=False) instead of
    removing the row, so historical invoices and statements keep resolving."""
    return bool(c.get("deleted_at"))


def _active_only(customers):
    """Customers still in service — soft-deleted rows are hidden from lists and
    must not block name/contact reuse in duplicate checks."""
    return [c for c in customers if not _is_deleted(c)]


class Location(BaseModel):
    label: str = "Primary"
    address: str


class CustomerFiscal(BaseModel):
    """Mexican receptor fiscal block (playbook §3.2). Optional everywhere;
    required only when a CFDI is stamped to this customer. Validated with
    the SAT catalogues whenever present, in any market."""
    rfc: str
    razon_social: str
    cp_fiscal: str
    regimen_fiscal: str
    uso_cfdi: str = "G03"


class CustomerBody(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    locations: List[Location] = []
    fiscal: Optional[CustomerFiscal] = None


def _fiscal_or_422(body: CustomerBody):
    """Serialise the optional fiscal block, enforcing CFDI validity."""
    if body.fiscal is None:
        return None
    from lib.cfdi import validate_receptor
    data = body.fiscal.model_dump()
    errs = validate_receptor(data)
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))
    return data


@router.get("")
async def list_customers(user: dict = Depends(get_current_user)):
    rows = await coll(user).find_list(sort=[("name", 1)], limit=500)
    return _active_only(rows)


@router.post("")
async def create_customer(body: CustomerBody,
                          user: dict = Depends(get_sync_actor),
                          request: Request = None):
    # Sync surface: a user session (ops:manage) OR a dedicated
    # integration credential may create customers here. When the caller
    # sends an Idempotency-Key (Momo does), the push is deduped
    # server-side — see lib/idempotency.py and INTEGRATION_CONTRACT.md.
    # `request` trails `user` so pre-existing direct callers using
    # (body, user) keep working; FastAPI injects it by type.
    idem_key = request.headers.get("Idempotency-Key") if request else None
    sess = await idem.begin(
        tenant_id=user["tenant_id"], key=idem_key,
        endpoint="POST /customers",
        req_hash=idem.request_hash(
            "POST", request.url.path if request else "/api/customers",
            body.model_dump()),
        actor=actor_label(user))
    if sess.is_replay:
        return sess.replay_response()
    try:
        if idem_key:
            # Crash-window guard: a previous attempt with this key
            # created the customer but its response was never recorded.
            stamped = await coll(user).find_one({"sync_key": idem_key})
            if stamped and not _is_deleted(stamped):
                out = {k: v for k, v in stamped.items() if k != "sync_key"}
                return await sess.complete(out, resource_id=stamped["id"])
        existing = await coll(user).find_list()
        dup = _find_duplicate(_active_only(existing), body.name, body.email, body.phone)
        if dup:
            raise HTTPException(status_code=409, detail={
                "message": "Customer already exists",
                "existing_id": dup.get("id"),
                "existing_name": dup.get("name"),
            })
        doc = {
            "id": new_id(), "name": body.name, "email": body.email, "phone": body.phone,
            "locations": [l.model_dump() for l in body.locations], "created_at": now_iso(),
            "active": True, "deleted_at": None, "fiscal": _fiscal_or_422(body),
        }
        if idem_key:
            doc["sync_key"] = idem_key
        await coll(user).insert_one(doc)
        doc.pop("_id", None)
        doc.pop("sync_key", None)
        return await sess.complete(doc, resource_id=doc["id"])
    except HTTPException as e:
        await sess.fail(e.status_code, str(e.detail))
        raise


@router.patch("/{customer_id}")
async def update_customer(customer_id: str, body: CustomerBody,
                          user: dict = Depends(require("ops:manage"))):
    c = await coll(user).find_one({"id": customer_id})
    if not c or _is_deleted(c):
        raise HTTPException(status_code=404, detail="Customer not found")
    # Same duplicate rule as create, excluding the customer being edited:
    # renaming onto another active customer's name + email/phone is a 409.
    others = [o for o in _active_only(await coll(user).find_list())
              if o.get("id") != customer_id]
    dup = _find_duplicate(others, body.name, body.email, body.phone)
    if dup:
        raise HTTPException(status_code=409, detail={
            "message": "Customer already exists",
            "existing_id": dup.get("id"),
            "existing_name": dup.get("name"),
        })
    await coll(user).update_one({"id": customer_id}, {"$set": {
        "name": body.name, "email": body.email, "phone": body.phone,
        "locations": [l.model_dump() for l in body.locations],
        "fiscal": _fiscal_or_422(body),
        "updated_at": now_iso(),
    }})
    updated = await coll(user).find_one({"id": customer_id})
    return {k: v for k, v in updated.items() if k != "_id"}


@router.delete("/{customer_id}")
async def delete_customer(customer_id: str, user: dict = Depends(require("ops:manage"))):
    """Soft delete: stamp deleted_at and active=False, keep the row. Invoices
    reference customers by id (and legacy rows by name), so removing the row
    would orphan history; statements for a deleted customer must still resolve."""
    c = await coll(user).find_one({"id": customer_id})
    if not c or _is_deleted(c):
        raise HTTPException(status_code=404, detail="Customer not found")
    ts = now_iso()
    await coll(user).update_one({"id": customer_id},
                                {"$set": {"deleted_at": ts, "active": False,
                                          "updated_at": ts}})
    return {"id": customer_id, "deleted": True, "deleted_at": ts}


@router.get("/{customer_id}")
async def get_customer(customer_id: str, user: dict = Depends(get_current_user)):
    # Soft-deleted customers still resolve here (and in statements) so invoice
    # history stays readable; they are only hidden from the list endpoint.
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
    # each other's invoices. Soft-deleted customers don't create ambiguity —
    # only active customers count.
    tenant_customers = _active_only(await coll(user).find_list())
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
