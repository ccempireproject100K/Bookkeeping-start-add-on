"""Developers (v1): API keys, outbound webhooks, and the public v1 API.

Management endpoints are owner-only user-session routes
(require("ledger:admin")):

* API keys — created keys are shown ONCE; only a SHA-256 hash + a
  display stub are stored (lib/developer.py, mirroring
  lib/integration_auth.py). List views are metadata-only.
* Webhook subscriptions — the signing secret is returned ONCE at
  creation and never appears in list/detail views; it lives only in
  the stored subscription so deliveries can be HMAC-signed.
* A delivery log for the tenant's subscriptions.

The public v1 surface authenticates with the API key itself
(lib.developer.get_api_key_actor; role "api_key", in NO PERMISSIONS
entry). v1 is read-only and tenant-scoped by construction — the
tenant always comes from the key record, never from the request.
Amounts are integer cents, like everywhere else in the product.
"""
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import db
from lib import developer as dev
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, now_iso

router = APIRouter(prefix="/developers", tags=["developers"])


# ---------------------------------------------------------------- API keys

class CreateKeyBody(BaseModel):
    label: str = Field(default="API key", max_length=120)


@router.post("/keys")
async def create_api_key(body: CreateKeyBody,
                         user: dict = Depends(require("ledger:admin"))):
    """Mint an API key for this workspace. The full key is returned
    ONCE in this response; only its hash is stored."""
    doc, full = dev.new_api_key_doc(
        tenant_id=user["tenant_id"], created_by=user.get("email", ""),
        label=body.label)
    await db.api_keys.insert_one(doc)
    await audit(user, "developer.api_key_created", "api_key", doc["id"],
                {"label": doc["label"], "key_stub": doc["key_stub"]})
    out = dev.public_api_key_view(doc)
    out["key"] = full
    out["api_key"] = full
    return out


@router.get("/keys")
async def list_api_keys(user: dict = Depends(require("ledger:admin"))):
    rows = await db.api_keys.find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0}).sort(
            "created_at", -1).to_list(200)
    return [dev.public_api_key_view(r) for r in rows]


@router.post("/keys/{key_id}/revoke")
async def revoke_api_key(key_id: str,
                         user: dict = Depends(require("ledger:admin"))):
    doc = await db.api_keys.find_one(
        {"id": key_id, "tenant_id": user["tenant_id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="API key not found")
    if doc.get("revoked_at"):
        return {"ok": True, "id": key_id, "revoked_at": doc["revoked_at"]}
    ts = now_iso()
    await db.api_keys.update_one(
        {"id": key_id, "tenant_id": user["tenant_id"]},
        {"$set": {"revoked_at": ts}})
    await audit(user, "developer.api_key_revoked", "api_key", key_id,
                {"key_stub": doc.get("key_stub"), "label": doc.get("label")})
    return {"ok": True, "id": key_id, "revoked_at": ts}


# ---------------------------------------------------------------- webhooks

class CreateWebhookBody(BaseModel):
    url: str = Field(max_length=2000)
    events: List[str] = Field(default_factory=list)


def _validate_webhook_body(body: CreateWebhookBody) -> str:
    url = (body.url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422,
                            detail="Webhook url must be an http(s) URL")
    if not body.events:
        raise HTTPException(status_code=422,
                            detail="Subscribe to at least one event")
    bad = [e for e in body.events if e not in dev.EVENT_TYPES]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event(s): {', '.join(bad)}. "
                   f"Valid events: {', '.join(dev.EVENT_TYPES)}")
    return url


@router.get("/events")
async def list_events(user: dict = Depends(require("ledger:admin"))):
    """The webhook event catalog (docs support)."""
    return [{"event": e, "description": dev.EVENT_DESCRIPTIONS[e]}
            for e in dev.EVENT_TYPES]


@router.post("/webhooks")
async def create_webhook(body: CreateWebhookBody,
                         user: dict = Depends(require("ledger:admin"))):
    """Create a subscription. The signing secret is returned ONCE in
    this response; verify deliveries with it (HMAC-SHA256 over the raw
    body, X-MAE-Signature-256: sha256=<hex>)."""
    url = _validate_webhook_body(body)
    doc, secret = dev.new_subscription_doc(
        tenant_id=user["tenant_id"], created_by=user.get("email", ""),
        url=url, events=body.events)
    await db.webhook_subscriptions.insert_one(doc)
    await audit(user, "developer.webhook_created", "webhook_subscription",
                doc["id"], {"url": url, "events": doc["events"]})
    out = dev.public_subscription_view(doc)
    out["signing_secret"] = secret
    return out


@router.get("/webhooks")
async def list_webhooks(user: dict = Depends(require("ledger:admin"))):
    rows = await db.webhook_subscriptions.find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0}).sort(
            "created_at", -1).to_list(200)
    return [dev.public_subscription_view(r) for r in rows]


async def _own_subscription(user, sub_id: str) -> dict:
    doc = await db.webhook_subscriptions.find_one(
        {"id": sub_id, "tenant_id": user["tenant_id"]})
    if not doc:
        raise HTTPException(status_code=404,
                            detail="Webhook subscription not found")
    return doc


@router.delete("/webhooks/{sub_id}")
async def delete_webhook(sub_id: str,
                         user: dict = Depends(require("ledger:admin"))):
    doc = await _own_subscription(user, sub_id)
    await db.webhook_subscriptions.delete_one(
        {"id": sub_id, "tenant_id": user["tenant_id"]})
    await audit(user, "developer.webhook_deleted", "webhook_subscription",
                sub_id, {"url": doc.get("url")})
    return {"ok": True, "id": sub_id}


@router.post("/webhooks/{sub_id}/test")
async def test_webhook(sub_id: str,
                       user: dict = Depends(require("ledger:admin"))):
    """Fire a webhook.test delivery at the subscription's URL now and
    return the delivery result (signed like any other delivery)."""
    doc = await _own_subscription(user, sub_id)
    delivery = await dev.deliver_to_subscription(
        doc, user["tenant_id"], "webhook.test",
        {"subscription_id": sub_id,
         "message": "Test webhook from Mind at Ease Solutions"})
    await audit(user, "developer.webhook_test", "webhook_subscription",
                sub_id, {"delivery_id": delivery["id"],
                         "status": delivery.get("status")})
    return dev.public_delivery_view(delivery)


@router.get("/deliveries")
async def list_deliveries(subscription_id: Optional[str] = None,
                          limit: int = Query(50, ge=1, le=500),
                          user: dict = Depends(require("ledger:admin"))):
    """Recent webhook delivery attempts for this workspace."""
    q = {"tenant_id": user["tenant_id"]}
    if subscription_id:
        q["subscription_id"] = subscription_id
    rows = await db.webhook_deliveries.find(q, {"_id": 0}).sort(
        "created_at", -1).to_list(limit)
    return [dev.public_delivery_view(r) for r in rows]


# ---------------------------------------------------------------- public v1

@router.get("/v1/whoami")
async def v1_whoami(actor: dict = Depends(dev.get_api_key_actor)):
    return {"tenant_id": actor["tenant_id"],
            "scopes": actor.get("scopes", []), "auth": "api_key"}


@router.get("/v1/invoices")
async def v1_invoices(limit: int = Query(50, ge=1, le=500),
                      actor: dict = Depends(dev.get_api_key_actor)):
    """The key's tenant's invoices, newest first. Integer cents."""
    rows = await Scoped(db.invoices, actor["tenant_id"]).find_list(
        sort=[("created_at", -1)], limit=limit)
    out = []
    for inv in rows:
        total = int(inv.get("total_cents") or 0)
        paid = int(inv.get("paid_cents") or 0)
        out.append({
            "id": inv.get("id"),
            "client_name": inv.get("client_name"),
            "status": inv.get("status"),
            "total_cents": total,
            "paid_cents": paid,
            "balance_cents": total - paid,
            "currency": inv.get("currency") or "USD",
            "issue_date": inv.get("issue_date")
            or (inv.get("created_at") or "")[:10] or None,
            "due_date": inv.get("due_date"),
        })
    return out


@router.get("/v1/customers")
async def v1_customers(limit: int = Query(50, ge=1, le=500),
                       actor: dict = Depends(dev.get_api_key_actor)):
    """The key's tenant's customers, newest first."""
    rows = await Scoped(db.customers, actor["tenant_id"]).find_list(
        sort=[("created_at", -1)], limit=limit)
    return [{
        "id": c.get("id"),
        "name": c.get("name"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "created_at": c.get("created_at"),
    } for c in rows]
