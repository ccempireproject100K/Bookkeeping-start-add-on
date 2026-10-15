"""Paid add-ons (inventory + AI) via a Stripe subscription — TEST MODE ONLY unless BILLING_ALLOW_LIVE=true.

Entitlements are written only from data read from Stripe: verified webhooks (signature checked in payments.py,
de-duplicated by event id) or a server-side Stripe API read in /billing/sync. Out-of-order events are ignored by
comparing Stripe's event `created` timestamp. Demo workspaces (tenant.access == 'demo') are never billed.
"""

import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException

from lib.audit import audit
from lib.auth import Principal, entitlements, get_principal, require
from lib.dates import now_iso
from lib.db import db
from lib.stripe_resources import stripe_dict
from models.ledger import BillingCheckoutIn, BillingCheckoutOut, BillingStatus, BillingSyncIn

router = APIRouter()
PRICE_CENTS = int(os.environ.get("BILLING_PRICE_CENTS", "2900"))


def _test_mode() -> bool:
    return not (os.environ.get("STRIPE_SECRET_KEY") or "").startswith("sk_live")


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


async def apply_subscription(tenant_id: str, sub: dict, created: int, event_type: str) -> bool:
    """Idempotent + order-safe: only applies if this event is not older than the last one applied."""
    sub = stripe_dict(sub)
    period_end = sub.get("current_period_end") or ((sub.get("items") or {}).get("data") or [{}])[0].get("current_period_end")
    patch = {"billing.status": sub.get("status", "none"), "billing.subscription_id": sub.get("id"),
             "billing.customer_id": sub.get("customer"), "billing.current_period_end": _iso(period_end),
             "billing.cancel_at_period_end": bool(sub.get("cancel_at_period_end")), "billing.last_event_created": created,
             "billing.last_event_type": event_type, "billing.updated_at": now_iso()}
    res = await db.tenants.update_one({"id": tenant_id, "$or": [{"billing.last_event_created": {"$lte": created}},
                                                                {"billing.last_event_created": {"$exists": False}}]}, {"$set": patch})
    if res.modified_count:
        await audit(None, "billing.subscription_" + str(sub.get("status")), "tenant", tenant_id, {"event": event_type}, tenant_id=tenant_id, system=True)
    return bool(res.modified_count)


async def _tenant_for(sub: dict) -> str | None:
    tid = (sub.get("metadata") or {}).get("tenant_id")
    if tid:
        return tid
    t = await db.tenants.find_one({"billing.subscription_id": sub.get("id")}, {"id": 1})
    return t["id"] if t else None


async def handle_billing_event(event: dict) -> None:
    t, obj, created = event["type"], event["data"]["object"], int(event.get("created") or 0)
    if t.startswith("customer.subscription."):
        tid = await _tenant_for(obj)
        if tid:
            await apply_subscription(tid, obj, created, t)
    elif t == "checkout.session.completed" and obj.get("subscription"):
        tid = obj.get("client_reference_id")
        sub = stripe.Subscription.retrieve(obj["subscription"])
        if tid:
            await apply_subscription(tid, stripe_dict(sub), created, t)
    elif t == "invoice.payment_failed":
        sub_id = obj.get("subscription") or ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if sub_id:
            await db.tenants.update_one({"billing.subscription_id": sub_id}, {"$set": {"billing.last_payment_failed_at": now_iso()}})


@router.get("/billing", response_model=BillingStatus)
async def billing_status(p: Principal = Depends(get_principal)):
    t = await db.tenants.find_one({"id": p.tenant_id}, {"_id": 0})
    b = t.get("billing") or {}
    ent = entitlements(t)
    access = "demo" if t.get("access") == "demo" else ("paid" if any(ent.values()) else "none")
    return BillingStatus(access=access, status=b.get("status", "none"), entitlements=ent, current_period_end=b.get("current_period_end"),  # type: ignore[arg-type]
                         cancel_at_period_end=bool(b.get("cancel_at_period_end")), price=PRICE_CENTS / 100, test_mode=_test_mode(),
                         last_event_type=b.get("last_event_type"))


@router.post("/billing/checkout", response_model=BillingCheckoutOut)
async def billing_checkout(body: BillingCheckoutIn, p: Principal = Depends(require("billing:write"))):
    if not _test_mode() and os.environ.get("BILLING_ALLOW_LIVE", "false").lower() != "true":
        raise HTTPException(403, "Live billing is disabled until the owner approves it (BILLING_ALLOW_LIVE)")
    if p.access == "demo":
        raise HTTPException(400, "This is a demo workspace — add-ons are already included and it is never billed")
    origin = body.origin_url.rstrip("/")
    try:
        s = stripe.checkout.Session.create(
            mode="subscription", client_reference_id=p.tenant_id,
            line_items=[{"price_data": {"currency": "usd", "unit_amount": PRICE_CENTS, "recurring": {"interval": "month"},
                                        "product_data": {"name": "Barber's Ledger Pro add-ons (inventory + AI)"}}, "quantity": 1}],
            subscription_data={"metadata": {"tenant_id": p.tenant_id}}, metadata={"tenant_id": p.tenant_id, "kind": "subscription"},
            success_url=f"{origin}/settings?billing=success&session_id={{CHECKOUT_SESSION_ID}}", cancel_url=f"{origin}/settings?billing=cancel")
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Billing unavailable: {e.user_message or 'Stripe error'}")
    await audit(p, "billing.checkout_started", "tenant", p.tenant_id, {"session_id": s.id})
    return BillingCheckoutOut(checkout_url=s.url, session_id=s.id)


@router.post("/billing/sync", response_model=BillingStatus)
async def billing_sync(body: BillingSyncIn, p: Principal = Depends(require("billing:write"))):
    """Return-from-checkout fallback when webhooks can't reach this host: reads the session from Stripe server-side."""
    try:
        s = stripe_dict(stripe.checkout.Session.retrieve(body.session_id))
    except stripe.error.StripeError:
        raise HTTPException(404, "Checkout session not found")
    if s.get("client_reference_id") != p.tenant_id:
        raise HTTPException(404, "Checkout session not found")
    if s.get("subscription"):
        sub = stripe.Subscription.retrieve(s["subscription"])
        await apply_subscription(p.tenant_id, stripe_dict(sub), int(datetime.now(timezone.utc).timestamp()), "sync.api_read")
    return await billing_status(p)
