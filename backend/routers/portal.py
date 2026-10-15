"""Magic-link client portal: passwordless, expiring links for customers.

The owner generates a link per invoice (from the API: POST /portal/links with
invoice_id or customer_id). The customer opens it with NO login and can:
  - view that invoice (details + amounts only),
  - pay it (posts a real journal entry and clears AR, same as an in-shop payment).

Links are random tokens (secrets.token_urlsafe); only the SHA-256 hash is stored
in the `portal_links` collection along with the expiry. Invalid/expired/revoked
tokens -> 404. The public endpoints are intentionally unauthenticated.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/portal", tags=["portal"])

DEFAULT_TTL_HOURS = 24 * 7  # 7 days


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


class LinkIn(BaseModel):
    invoice_id: Optional[str] = None
    customer_id: Optional[str] = None
    expires_in_hours: int = Field(default=DEFAULT_TTL_HOURS, ge=1, le=24 * 90)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _lookup_link(token: str) -> dict:
    """Public: resolve a magic token to its link doc. Invalid/expired/revoked -> 404."""
    link = await db.portal_links.find_one({"token_hash": _digest(token)}, {"_id": 0})
    if not link or link.get("revoked") or link.get("used"):
        raise HTTPException(status_code=404, detail="Link not found")
    if link["expires_at"] <= now_iso():
        raise HTTPException(status_code=404, detail="Link expired")
    return link


@router.post("/links")
async def create_link(body: LinkIn, user: dict = Depends(require("ops:manage"))):
    """Generate a magic link. Pass invoice_id, or customer_id to link that
    customer's most recent unpaid invoice."""
    inv_coll = Scoped(db.invoices, user["tenant_id"])
    if body.invoice_id:
        inv = await inv_coll.find_one({"id": body.invoice_id})
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")
    elif body.customer_id:
        invs = await inv_coll.find_list({"customer_id": body.customer_id},
                                        sort=[("created_at", -1)], limit=50)
        inv = next((i for i in invs
                    if i["total_cents"] - i.get("paid_cents", 0) > 0), None)
        if not inv:
            raise HTTPException(status_code=404, detail="No unpaid invoice for this customer")
    else:
        raise HTTPException(status_code=422, detail="Pass invoice_id or customer_id")

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=body.expires_in_hours)).isoformat()
    await db.portal_links.insert_one({
        "id": new_id(), "token_hash": _digest(token), "tenant_id": user["tenant_id"],
        "invoice_id": inv["id"], "customer_id": inv.get("customer_id"),
        "created_by": user.get("name"), "created_at": now_iso(),
        "expires_at": expires_at, "revoked": False, "used": False,
    })
    await _audit(user, "portal.link.create", "invoice", inv["id"],
                 {"customer_id": inv.get("customer_id"), "expires_at": expires_at})
    return {"token": token, "expires_at": expires_at, "invoice_id": inv["id"],
            "client_name": inv.get("client_name")}


@router.get("/links")
async def list_links(user: dict = Depends(require("ops:manage"))):
    """All links (newest first) with a computed status for the management UI:
    active | expired | used | revoked."""
    rows = await Scoped(db.portal_links, user["tenant_id"]).find_list(
        {}, sort=[("created_at", -1)], limit=200,
        projection={"_id": 0, "token_hash": 0})
    now = now_iso()
    for r in rows:
        if r.get("revoked"):
            r["status"] = "revoked"
        elif r.get("used"):
            r["status"] = "used"
        elif r.get("expires_at", "") <= now:
            r["status"] = "expired"
        else:
            r["status"] = "active"
    return rows


@router.post("/links/{link_id}/revoke")
async def revoke_link(link_id: str, user: dict = Depends(require("ops:manage"))):
    r = await Scoped(db.portal_links, user["tenant_id"]).update_one(
        {"id": link_id}, {"$set": {"revoked": True}})
    if not r.matched_count:
        raise HTTPException(status_code=404, detail="Link not found")
    await _audit(user, "portal.link.revoke", "portal_link", link_id, {})
    return {"ok": True}


def _strip_invoice(inv: dict) -> dict:
    lines = []
    for ln in inv.get("lines", []) or []:
        lines.append({"name": ln.get("name"), "kind": ln.get("kind"),
                      "amount_cents": to_cents(ln.get("amount"))})
    paid = inv.get("paid_cents", 0)
    return {
        "id": inv.get("id"), "client_name": inv.get("client_name"),
        "due_date": inv.get("due_date"), "terms": inv.get("terms"),
        "status": inv.get("status"), "issue_entry": inv.get("issue_entry"),
        "created_at": inv.get("created_at"), "lines": lines,
        "subtotal_cents": inv.get("subtotal_cents", 0),
        "tax_cents": inv.get("tax_cents", 0),
        "total_cents": inv.get("total_cents", 0),
        "paid_cents": paid,
        "balance_cents": inv.get("total_cents", 0) - paid,
    }


@router.get("/view/{token}")
async def portal_view(token: str):
    """Public: view the linked invoice. No login required. Invoice details + amounts only."""
    link = await _lookup_link(token)
    inv = await db.invoices.find_one(
        {"tenant_id": link["tenant_id"], "id": link["invoice_id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    tenant = await db.tenants.find_one({"id": link["tenant_id"]}, {"_id": 0, "name": 1})
    return {"business": (tenant or {}).get("name", ""), "invoice": _strip_invoice(inv)}


class PortalPayIn(BaseModel):
    amount: float = Field(gt=0)          # dollars
    payment_method: str = Field(default="bank", pattern=r"^(bank|card|cash)$")
    reference: str = Field(default="", max_length=120)


@router.post("/pay/{token}")
async def portal_pay(token: str, body: PortalPayIn):
    """Public: customer pays the linked invoice. Posts a real journal entry
    (Dr cash / Cr AR 1200), updates the invoice paid totals, and marks the
    link used once the invoice is paid in full."""
    link = await _lookup_link(token)
    inv = await db.invoices.find_one(
        {"tenant_id": link["tenant_id"], "id": link["invoice_id"]})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    paid = inv.get("paid_cents", 0)
    balance = inv["total_cents"] - paid
    if balance <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    cents = to_cents(body.amount)
    if cents <= 0:
        raise HTTPException(status_code=422, detail="Invalid payment amount")
    if cents > balance:
        raise HTTPException(status_code=422, detail="Amount exceeds the balance due")

    actor = {"id": "portal", "email": "portal@client", "name": "Client portal",
             "role": "owner", "tenant_id": link["tenant_id"]}
    cash = P._cash_account(body.payment_method)
    entry = await L.post_journal_entry(db, link["tenant_id"], actor, {
        "entry_date": today_str(), "source": "portal_payment", "source_ref": inv["id"],
        "memo": f"Client portal payment — {inv['client_name']}",
        "lines": [P.dr(cash, cents, "Client portal payment"),
                  P.cr("1200", cents, "AR cleared")],
    }, status="posted")

    new_paid = paid + cents
    status = "paid" if new_paid >= inv["total_cents"] else "partial"
    pmt = {"date": today_str(), "amount_cents": cents, "cash_cents": cents,
           "credit_cents": 0, "method": body.payment_method,
           "reference": body.reference, "entry": entry["number"], "source": "portal"}
    await db.invoices.update_one(
        {"tenant_id": link["tenant_id"], "id": inv["id"]},
        {"$set": {"status": status, "paid_cents": new_paid}, "$push": {"payments": pmt}})
    if status == "paid":
        await db.portal_links.update_one(
            {"token_hash": _digest(token)}, {"$set": {"used": True}})
    await _audit(actor, "portal.payment", "invoice", inv["id"],
                 {"amount_cents": cents, "method": body.payment_method,
                  "entry_number": entry["number"], "tenant_id": link["tenant_id"]})
    return {"ok": True, "status": status, "paid_cents": new_paid,
            "balance_cents": inv["total_cents"] - new_paid,
            "entry_number": entry["number"]}
