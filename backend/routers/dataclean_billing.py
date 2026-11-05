"""DataClean billing: Stripe Checkout for the workspace's DataClean
subscription (Security and the bundle join the moment their sellable
flag flips — see lib/dataclean_api.py's ladder), and the webhook that
is the ONLY writer of the tenant's ``dataclean_billing`` record.

The record — product, status, Stripe ids, current period end — plus
the entitlement derived from it change ONLY inside the
signature-verified webhook below. Creating a Checkout Session
changes nothing, and the browser returning to
/dataclean-checkout?checkout=success changes nothing: Stripe's
servers confirming money is the sole source of truth (the books
billing rule, routers/billing.py, applied to this product family).

Deliveries are claimed by event id in ``stripe_webhook_events``
before processing (unique index in server.py), so a redelivery
returns 200 without reprocessing; state application is itself
idempotent. The Stripe account is shared with the books flow, so
handlers process only objects stamped with this flow's metadata
kind or resolvable through this flow's ids on the tenant — a books
subscription event can never move DataClean state, and vice versa.

Key-gated like every Stripe surface in this codebase: without
STRIPE_SECRET_KEY the checkout/status endpoints return an honest
503; without a webhook secret the webhook refuses every call.
"""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db import db
from lib import dataclean_billing as dcb
from lib.audit import append_audit
from lib.auth import require
from lib.base import new_id, now_iso

router = APIRouter(prefix="/dataclean/billing", tags=["dataclean-billing"])
logger = logging.getLogger(__name__)


def _require_billing():
    if not dcb.configured():
        raise HTTPException(
            status_code=503,
            detail="Online billing is not configured. Set STRIPE_SECRET_KEY "
                   "to enable DataClean subscriptions.")


def _origin(body_origin: str, request: Request) -> str:
    return (body_origin or request.headers.get("origin")
            or os.environ.get("APP_URL", "") or "").rstrip("/")


async def _audit_system(tenant_id, action, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": tenant_id,
        "actor_id": "stripe_webhook", "actor_name": "Stripe webhook",
        "actor_role": "system", "action": action,
        "entity": "tenant", "entity_id": tenant_id,
        "details": details or {}, "created_at": now_iso(),
    })


async def _audit_user(user, action, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action,
        "entity": "tenant", "entity_id": user["tenant_id"],
        "details": details or {}, "created_at": now_iso(),
    })


def _record_view(rec) -> dict:
    if not rec:
        return {"status": None, "product": None, "entitled": False,
                "current_period_end": None, "cancel_at_period_end": False,
                "has_billing_account": False}
    return {
        "status": rec.get("status"),
        "product": rec.get("product"),
        "entitled": dcb.record_entitles(rec),
        "current_period_end": rec.get("current_period_end"),
        "cancel_at_period_end": bool(rec.get("cancel_at_period_end")),
        "has_billing_account": bool(rec.get("stripe_customer_id")),
    }


# ---------------------------------------------------------------- status

@router.get("/status")
async def dataclean_billing_status(
        user: dict = Depends(require("ledger:admin"))):
    """What the checkout page renders: whether online billing is
    configured at all, the stamped catalog with its sellable flags,
    and this workspace's subscription state + entitlement."""
    rec = await dcb.get_billing_record(user["tenant_id"])
    entitled = await dcb.tenant_dataclean_entitled(user["tenant_id"])
    view = _record_view(rec)
    view["entitled"] = entitled
    return {
        "configured": dcb.configured(),
        "billing_mode": "live" if (dcb.secret().startswith("sk_live_")) else "test",
        "publishable_key": dcb.publishable_key(),
        "enforcement_on": dcb.enforcement_on(),
        "billing": view,
        "catalog": dcb.catalog_public(),
    }


# ---------------------------------------------------------------- checkout

class CheckoutIn(BaseModel):
    product: str  # "dataclean" | "security" | "bundle"
    origin_url: str = ""  # e.g. https://books.example.com — return-URL base
    ui_mode: str = "hosted"  # "hosted" redirect | "elements" embedded


def _sget(session, key):
    """Read a field off a Stripe session object (attribute or dict)."""
    value = getattr(session, key, None)
    if value is None and isinstance(session, dict):
        value = session.get(key)
    return value


@router.post("/checkout")
async def dataclean_checkout(body: CheckoutIn, request: Request,
                             user: dict = Depends(require("ledger:admin"))):
    """Start Stripe Checkout for a DataClean-family subscription.
    Creates the session and NOTHING else — the workspace's record and
    entitlement change only when the webhook confirms payment."""
    _require_billing()  # 503 BEFORE any SDK import when keys are absent
    catalog = dcb.product_catalog()
    product = (body.product or "").strip().lower()
    if product not in catalog:
        raise HTTPException(status_code=422,
                            detail=f"Unknown product: {body.product!r}")
    if not catalog[product]["sellable"]:
        raise HTTPException(
            status_code=422,
            detail=("Security isn't available yet — it goes on sale when "
                    "it ships, and the bundle with it. DataClean can be "
                    "subscribed to today.") if product != "dataclean" else
                   "This product isn't available for purchase yet.")
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    origin = _origin(body.origin_url, request)
    if not origin or not origin.startswith("http"):
        raise HTTPException(status_code=422, detail="origin_url is required")
    rec = tenant.get("dataclean_billing") or {}
    books_billing = tenant.get("billing") or {}
    customer_id = (rec.get("stripe_customer_id")
                   or books_billing.get("stripe_customer_id") or "")
    ui_mode = "elements" if body.ui_mode == "elements" else "hosted"
    try:
        session = await dcb.create_checkout_session(
            tenant_id=user["tenant_id"], product=product,
            user_email=user.get("email") or "",
            customer_id=customer_id, origin=origin, ui_mode=ui_mode)
    except Exception as e:
        logger.warning("dataclean checkout create failed tenant=%s "
                       "product=%s %s", user["tenant_id"], product,
                       type(e).__name__)
        raise HTTPException(status_code=502,
                            detail="Billing is unavailable — please try again.")
    await _audit_user(user, "dataclean_billing.checkout_started",
                      {"product": product, "session_id": _sget(session, "id"),
                       "ui_mode": ui_mode})
    return {"checkout_url": _sget(session, "url"),
            "client_secret": _sget(session, "client_secret"),
            "session_id": _sget(session, "id"), "ui_mode": ui_mode}


# ---------------------------------------------------------------- webhook state

async def _resolve_tenant(tenant_id=None, subscription_id=None,
                          customer_id=None):
    """Find the tenant a Stripe object belongs to: explicit metadata
    first, then the ids earlier events stamped on the tenant's
    dataclean_billing record."""
    if tenant_id:
        t = await db.tenants.find_one({"id": tenant_id})
        if t:
            return t
    if subscription_id:
        t = await db.tenants.find_one(
            {"dataclean_billing.subscription_id": subscription_id})
        if t:
            return t
    if customer_id:
        t = await db.tenants.find_one(
            {"dataclean_billing.stripe_customer_id": customer_id})
        if t:
            return t
    return None


async def _set_record(tenant, fields):
    """Merge fields into the tenant's dataclean_billing subdoc
    (whole-subdoc $set, read-modify-write like the books flow).
    Webhook handlers are the only callers — and the only writers of
    this subdoc anywhere."""
    merged = dict(tenant.get("dataclean_billing") or {})
    for k, v in fields.items():
        if v is not None:
            merged[k] = v
    merged["updated_at"] = now_iso()
    await db.tenants.update_one(
        {"id": tenant["id"]}, {"$set": {"dataclean_billing": merged}})
    return merged


def _is_ours(obj, tenant) -> bool:
    """Shared-account guard: an object belongs to this flow when its
    metadata says so, or when it resolved through this flow's ids."""
    meta = obj.get("metadata") or {}
    if meta.get("kind"):
        return meta.get("kind") == dcb.KIND
    return tenant is not None


async def _on_checkout_completed(obj):
    meta = obj.get("metadata") or {}
    if meta.get("kind") != dcb.KIND:
        return  # some other flow sharing the Stripe account
    tenant = await _resolve_tenant(tenant_id=meta.get("tenant_id"),
                                   customer_id=obj.get("customer"))
    if not tenant:
        logger.warning("dataclean checkout completed for unknown tenant=%s",
                       meta.get("tenant_id"))
        return
    product = meta.get("product")
    if product not in dcb.product_catalog():
        product = (tenant.get("dataclean_billing") or {}).get("product")
    paid = obj.get("payment_status") == "paid"
    fields = {
        "provider": "stripe",
        "stripe_customer_id": obj.get("customer"),
        "subscription_id": obj.get("subscription"),
        "checkout_session_id": obj.get("id"),
        "status": "active" if paid else "incomplete",
    }
    if product:
        fields["product"] = product
    await _set_record(tenant, fields)
    await _audit_system(tenant["id"], "dataclean_billing.subscription_started",
                        {"product": product, "session_id": obj.get("id"),
                         "payment_status": obj.get("payment_status")})


async def _subscription_product(obj, tenant):
    meta = obj.get("metadata") or {}
    if meta.get("product") in dcb.product_catalog():
        return meta["product"]
    for item in ((obj.get("items") or {}).get("data") or []):
        found = await dcb.product_for_price_id(
            ((item.get("price") or {}).get("id")) or "")
        if found:
            return found
    return (tenant.get("dataclean_billing") or {}).get("product")


async def _on_subscription_upsert(obj):
    tenant = await _resolve_tenant(
        tenant_id=(obj.get("metadata") or {}).get("tenant_id"),
        subscription_id=obj.get("id"), customer_id=obj.get("customer"))
    if not _is_ours(obj, tenant) or not tenant:
        return
    status = dcb.map_subscription_status(obj.get("status"))
    product = await _subscription_product(obj, tenant)
    fields = {
        "provider": "stripe",
        "subscription_id": obj.get("id"),
        "stripe_customer_id": obj.get("customer"),
        "status": status,
        "current_period_end": dcb.iso_from_epoch(obj.get("current_period_end")),
        "cancel_at_period_end": bool(obj.get("cancel_at_period_end")),
    }
    if product:
        fields["product"] = product
    await _set_record(tenant, fields)
    await _audit_system(tenant["id"], "dataclean_billing.subscription_updated",
                        {"product": product, "status": status,
                         "subscription_id": obj.get("id")})


async def _on_subscription_deleted(obj):
    tenant = await _resolve_tenant(
        tenant_id=(obj.get("metadata") or {}).get("tenant_id"),
        subscription_id=obj.get("id"), customer_id=obj.get("customer"))
    if not _is_ours(obj, tenant) or not tenant:
        return
    await _set_record(tenant, {
        "provider": "stripe",
        "subscription_id": obj.get("id"),
        "stripe_customer_id": obj.get("customer"),
        "status": "canceled",
        "current_period_end": dcb.iso_from_epoch(obj.get("current_period_end")),
        "cancel_at_period_end": False,
    })
    await _audit_system(tenant["id"], "dataclean_billing.subscription_canceled",
                        {"subscription_id": obj.get("id")})


def _invoice_subscription_id(obj):
    if obj.get("subscription"):
        return obj["subscription"]
    details = (obj.get("parent") or {}).get("subscription_details") or {}
    return details.get("subscription")


async def _on_invoice_paid(obj):
    sub_id = _invoice_subscription_id(obj)
    if not sub_id:
        return
    tenant = await _resolve_tenant(subscription_id=sub_id,
                                   customer_id=obj.get("customer"))
    if not tenant:
        return  # not one of this flow's subscriptions
    paid_at = dcb.iso_from_epoch(
        (obj.get("status_transitions") or {}).get("paid_at")
        or obj.get("created"))
    await _set_record(tenant, {
        "status": "active",
        "last_paid_invoice_id": obj.get("id"),
        "last_payment_at": paid_at,
    })
    await _audit_system(tenant["id"], "dataclean_billing.payment_succeeded",
                        {"invoice_id": obj.get("id")})


async def _on_invoice_failed(obj):
    sub_id = _invoice_subscription_id(obj)
    if not sub_id:
        return
    tenant = await _resolve_tenant(subscription_id=sub_id,
                                   customer_id=obj.get("customer"))
    if not tenant:
        return
    await _set_record(tenant, {
        "status": "past_due",
        "last_payment_failed_at": now_iso(),
        "last_failed_invoice_id": obj.get("id"),
    })
    await _audit_system(tenant["id"], "dataclean_billing.payment_failed",
                        {"invoice_id": obj.get("id")})


async def _process_event(event):
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if etype == "checkout.session.completed":
        await _on_checkout_completed(obj)
    elif etype in ("customer.subscription.created",
                   "customer.subscription.updated"):
        await _on_subscription_upsert(obj)
    elif etype == "customer.subscription.deleted":
        await _on_subscription_deleted(obj)
    elif etype == "invoice.payment_succeeded":
        await _on_invoice_paid(obj)
    elif etype == "invoice.payment_failed":
        await _on_invoice_failed(obj)
    else:
        logger.info("dataclean webhook ignored event type=%s id=%s",
                    etype, event.get("id"))


@router.post("/stripe/webhook")
async def dataclean_webhook(request: Request):
    """Public (Stripe server-to-server): signature verification is
    mandatory — without this endpoint's secret it refuses (503).
    Deliveries are claimed by event id before processing; a
    redelivery of a processed event returns 200 without
    reprocessing."""
    if not dcb.webhook_secret():
        raise HTTPException(
            status_code=503,
            detail="Stripe DataClean webhook is not configured. Set "
                   "STRIPE_DATACLEAN_WEBHOOK_SECRET (or STRIPE_WEBHOOK_SECRET).")
    if not dcb.configured():
        raise HTTPException(
            status_code=503,
            detail="Online billing is not configured. Set STRIPE_SECRET_KEY.")
    try:
        payload = await request.body()
        event = dcb.construct_webhook_event(
            payload, request.headers.get("stripe-signature", ""))
    except dcb.WebhookSignatureError:
        logger.warning("dataclean webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    event_id = event.get("id")
    existing = await db.stripe_webhook_events.find_one({"id": event_id})
    if existing and existing.get("status") == "processed":
        return {"received": True, "duplicate": True}
    await db.stripe_webhook_events.update_one(
        {"id": event_id},
        {"$set": {"id": event_id, "type": event.get("type"),
                  "source": dcb.SOURCE, "status": "claimed",
                  "received_at": now_iso()}},
        upsert=True)
    try:
        await _process_event(event)
    except Exception as e:
        await db.stripe_webhook_events.update_one(
            {"id": event_id},
            {"$set": {"status": "error", "error": str(e)[:300],
                      "updated_at": now_iso()}})
        logger.warning("dataclean webhook processing failed event=%s: %s",
                       event_id, type(e).__name__)
        raise HTTPException(status_code=500, detail="Webhook processing failed")
    await db.stripe_webhook_events.update_one(
        {"id": event_id},
        {"$set": {"status": "processed", "processed_at": now_iso()}})
    return {"received": True}
