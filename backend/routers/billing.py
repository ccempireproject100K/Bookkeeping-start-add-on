"""Books subscription billing: Stripe Checkout for the workspace's own
plan (Light / Medium self-serve; Heavy is volume/custom and never sold
online), the Customer Portal for managing it, and the webhook that is
the ONLY writer of subscription-driven billing state.

The tenant's ``billing`` subdoc — provider ids, plan, status, current
period end — plus the stored ``plan`` entitlement change ONLY inside
the signature-verified webhook below. Creating a Checkout Session
changes nothing, and the browser returning to /settings?billing=success
changes nothing: Stripe's servers confirming money is the sole source
of truth. Webhook deliveries are claimed by event id in
``stripe_webhook_events`` before processing (unique index in
server.py), so a redelivery returns 200 without reprocessing; state
application is itself idempotent, and referral payment events are
emitted exactly once per Stripe payment (see _emit_referral_payment).

Key-gated like the portal flow: without STRIPE_SECRET_KEY the
checkout/portal endpoints return an honest 503; without a webhook
secret the webhook refuses every call.
"""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from db import db
from lib.audit import append_audit
from lib import billing, plans
from lib.auth import get_current_user, require
from lib.base import new_id, now_iso

router = APIRouter(prefix="/billing", tags=["billing"])
logger = logging.getLogger(__name__)


def _require_billing():
    if not billing.configured():
        raise HTTPException(
            status_code=503,
            detail="Online billing is not configured. Set STRIPE_SECRET_KEY "
                   "to enable subscriptions (see CHECKOUT_BUILD.md).")


def _origin(body_origin: str, request: Request) -> str:
    return (body_origin or request.headers.get("origin")
            or os.environ.get("APP_URL", "") or "").rstrip("/")


async def _audit_user(user, action, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action,
        "entity": "tenant", "entity_id": user["tenant_id"],
        "details": details or {}, "created_at": now_iso(),
    })


async def _audit_system(tenant_id, action, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": tenant_id,
        "actor_id": "stripe_webhook", "actor_name": "Stripe webhook",
        "actor_role": "system", "action": action,
        "entity": "tenant", "entity_id": tenant_id,
        "details": details or {}, "created_at": now_iso(),
    })


# ---------------------------------------------------------------- status

@router.get("/status")
async def billing_status(user: dict = Depends(get_current_user)):
    """What the billing page renders: the stored plan, the Stripe
    billing state (if any), whether online billing is configured at
    all, and the plan catalog."""
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    b = tenant.get("billing") or {}
    return {
        "configured": billing.configured(),
        "plan": plans.stored_plan(tenant),
        "effective_plan": plans.effective_plan(tenant),
        "trial_active": plans.trial_active(tenant),
        "billing": {
            "status": b.get("status"),
            "plan": b.get("plan"),
            "current_period_end": b.get("current_period_end"),
            "cancel_at_period_end": bool(b.get("cancel_at_period_end")),
            "has_billing_account": bool(b.get("stripe_customer_id")),
        } if b else None,
        "catalog": billing.catalog_public(),
    }


# ---------------------------------------------------------------- checkout

class CheckoutIn(BaseModel):
    plan: str
    origin_url: str = ""  # e.g. https://books.example.com — return-URL base


@router.post("/checkout")
async def subscribe_checkout(body: CheckoutIn, request: Request,
                             user: dict = Depends(require("settings:write"))):
    """Start Stripe Checkout for a self-serve plan. Creates the session
    and NOTHING else — the tenant's plan and billing state change only
    when the webhook confirms the subscription."""
    _require_billing()  # 503 BEFORE any SDK import when keys are absent
    plan = plans.validate_plan(body.plan)  # 422 on an unknown plan
    if plan not in billing.SELF_SERVE_PLANS:
        raise HTTPException(
            status_code=422,
            detail="Books Heavy is volume-based and isn't sold online — "
                   "contact us and we'll set it up with you.")
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    origin = _origin(body.origin_url, request)
    if not origin or not origin.startswith("http"):
        raise HTTPException(status_code=422, detail="origin_url is required")
    try:
        session = billing.create_checkout_session(
            tenant_id=user["tenant_id"], plan=plan,
            user_email=user.get("email") or "",
            customer_id=(tenant.get("billing") or {}).get("stripe_customer_id") or "",
            origin=origin)
    except Exception as e:
        # Log the SDK error server-side; never echo provider internals.
        logger.warning("billing checkout create failed tenant=%s plan=%s %s",
                       user["tenant_id"], plan, type(e).__name__)
        raise HTTPException(status_code=502,
                            detail="Billing is unavailable — please try again.")
    await _audit_user(user, "billing.checkout_started",
                      {"plan": plan, "session_id": session.id})
    return {"checkout_url": session.url, "session_id": session.id}


class PortalSessionIn(BaseModel):
    origin_url: str = ""


@router.post("/portal-session")
async def billing_portal_session(body: PortalSessionIn, request: Request,
                                 user: dict = Depends(require("settings:write"))):
    """A Stripe Customer Portal session for the tenant's billing
    account: cancel, switch plan, and update the card all happen there."""
    _require_billing()
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    customer_id = (tenant.get("billing") or {}).get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(
            status_code=409,
            detail="This workspace has no Stripe billing account yet — "
                   "subscribe to a plan first.")
    origin = _origin(body.origin_url, request)
    if not origin or not origin.startswith("http"):
        raise HTTPException(status_code=422, detail="origin_url is required")
    try:
        session = billing.create_portal_session(
            customer_id=customer_id, return_url=f"{origin}/settings")
    except Exception as e:
        logger.warning("billing portal create failed tenant=%s %s",
                       user["tenant_id"], type(e).__name__)
        raise HTTPException(status_code=502,
                            detail="Billing is unavailable — please try again.")
    await _audit_user(user, "billing.portal_opened", {})
    return {"url": session.url}


# ---------------------------------------------------------------- webhook state

async def _resolve_tenant(tenant_id=None, subscription_id=None, customer_id=None):
    """Find the tenant a Stripe object belongs to: explicit metadata
    first, then the ids earlier events stamped on the tenant doc."""
    if tenant_id:
        t = await db.tenants.find_one({"id": tenant_id})
        if t:
            return t
    if subscription_id:
        t = await db.tenants.find_one({"billing.subscription_id": subscription_id})
        if t:
            return t
    if customer_id:
        t = await db.tenants.find_one({"billing.stripe_customer_id": customer_id})
        if t:
            return t
    return None


async def _set_billing(tenant, fields, plan=None):
    """Merge fields into the tenant's billing subdoc (whole-subdoc $set,
    read-modify-write like the Connect state) and optionally move the
    stored plan entitlement. Webhook handlers are the only callers."""
    merged = dict(tenant.get("billing") or {})
    for k, v in fields.items():
        if v is not None:
            merged[k] = v
    merged["updated_at"] = now_iso()
    update = {"billing": merged}
    if plan:
        update["plan"] = plan
    await db.tenants.update_one({"id": tenant["id"]}, {"$set": update})
    return merged


async def _on_checkout_completed(obj):
    meta = obj.get("metadata") or {}
    if meta.get("kind") == "books_card_setup" or obj.get("mode") == "setup":
        # Card trial door: a card was put on file (setup mode, no
        # charge). Record its presence; the trial auto-conversion
        # sweep (lib/trial.py) is what later charges it.
        tenant = await _resolve_tenant(tenant_id=meta.get("tenant_id"),
                                       customer_id=obj.get("customer"))
        if not tenant:
            logger.warning("billing card setup for unknown tenant=%s",
                           meta.get("tenant_id"))
            return
        pm_id = ""
        try:
            pm_id = billing.setup_intent_payment_method(obj.get("setup_intent"))
        except Exception:
            logger.warning("card setup: payment method lookup failed "
                           "tenant=%s", tenant["id"], exc_info=True)
        await _set_billing(tenant, {
            "provider": "stripe",
            "stripe_customer_id": obj.get("customer"),
            "card_on_file": True,
            "card_payment_method_id": pm_id or None,
            "setup_intent_id": obj.get("setup_intent"),
        })
        await _audit_system(tenant["id"], "billing.card_on_file",
                            {"session_id": obj.get("id")})
        return
    if meta.get("kind") != "books_subscription":
        return  # some other flow sharing the Stripe account
    tenant = await _resolve_tenant(tenant_id=meta.get("tenant_id"))
    if not tenant:
        logger.warning("billing checkout completed for unknown tenant=%s",
                       meta.get("tenant_id"))
        return
    plan = meta.get("plan") if meta.get("plan") in billing.SELF_SERVE_PLANS else None
    paid = obj.get("payment_status") == "paid"
    fields = {
        "provider": "stripe",
        "stripe_customer_id": obj.get("customer"),
        "subscription_id": obj.get("subscription"),
        "checkout_session_id": obj.get("id"),
        "status": "active" if paid else "incomplete",
    }
    if plan:
        fields["plan"] = plan
    # The intended plan is recorded on the billing subdoc at once, but
    # the stored entitlement moves only when money has actually landed;
    # subscription/invoice events govern from there.
    await _set_billing(tenant, fields, plan=plan if paid else None)
    if paid and tenant.get("first_month_discount_percent"):
        # The card-door conversion landed: the welcome coupon rode
        # this session, so the first-month discount terms are spent.
        await db.tenants.update_one(
            {"id": tenant["id"]},
            {"$set": {"first_month_discount_applied": True}})
    await _audit_system(tenant["id"], "billing.subscription_started",
                        {"plan": plan, "session_id": obj.get("id"),
                         "payment_status": obj.get("payment_status")})


def _subscription_plan(obj, tenant):
    meta = obj.get("metadata") or {}
    if meta.get("plan") in billing.SELF_SERVE_PLANS:
        return meta["plan"]
    for item in ((obj.get("items") or {}).get("data") or []):
        found = billing.plan_for_price_id(((item.get("price") or {}).get("id")) or "")
        if found:
            return found
    return (tenant.get("billing") or {}).get("plan")


async def _on_subscription_upsert(obj):
    tenant = await _resolve_tenant(
        tenant_id=(obj.get("metadata") or {}).get("tenant_id"),
        subscription_id=obj.get("id"), customer_id=obj.get("customer"))
    if not tenant:
        return
    status = billing.map_subscription_status(obj.get("status"))
    plan = _subscription_plan(obj, tenant)
    fields = {
        "provider": "stripe",
        "subscription_id": obj.get("id"),
        "stripe_customer_id": obj.get("customer"),
        "status": status,
        "current_period_end": billing.iso_from_epoch(obj.get("current_period_end")),
        "cancel_at_period_end": bool(obj.get("cancel_at_period_end")),
    }
    if plan:
        fields["plan"] = plan
    # A canceled subscription falls back to Light (the default tier a
    # fresh workspace starts on); live states carry their plan.
    new_plan = "light" if status == "canceled" else (
        plan if status in ("active", "past_due") else None)
    await _set_billing(tenant, fields, plan=new_plan)
    if new_plan and new_plan != tenant.get("plan"):
        await _audit_system(tenant["id"], "billing.plan_changed",
                            {"plan": new_plan, "status": status,
                             "subscription_id": obj.get("id")})


async def _on_subscription_deleted(obj):
    tenant = await _resolve_tenant(
        tenant_id=(obj.get("metadata") or {}).get("tenant_id"),
        subscription_id=obj.get("id"), customer_id=obj.get("customer"))
    if not tenant:
        return
    await _set_billing(tenant, {
        "provider": "stripe",
        "subscription_id": obj.get("id"),
        "stripe_customer_id": obj.get("customer"),
        "status": "canceled",
        "current_period_end": billing.iso_from_epoch(obj.get("current_period_end")),
        "cancel_at_period_end": False,
    }, plan="light")
    await _audit_system(tenant["id"], "billing.subscription_canceled",
                        {"subscription_id": obj.get("id")})


def _invoice_subscription_id(obj):
    if obj.get("subscription"):
        return obj["subscription"]
    details = (obj.get("parent") or {}).get("subscription_details") or {}
    return details.get("subscription")


async def _on_invoice_paid(obj, event):
    sub_id = _invoice_subscription_id(obj)
    if not sub_id:
        return  # not a subscription invoice — nothing here drives it
    tenant = await _resolve_tenant(subscription_id=sub_id,
                                   customer_id=obj.get("customer"))
    if not tenant:
        return
    plan = (tenant.get("billing") or {}).get("plan")
    paid_at = billing.iso_from_epoch(
        (obj.get("status_transitions") or {}).get("paid_at") or obj.get("created"))
    await _set_billing(tenant, {
        "status": "active",
        "last_paid_invoice_id": obj.get("id"),
        "last_payment_at": paid_at,
    }, plan=plan if plan in billing.SELF_SERVE_PLANS else None)
    if not tenant.get("referral_active"):
        # Conversion activates referrals for tenants who didn't get
        # them instantly at the card opt-in (trial door 1's default).
        await db.tenants.update_one(
            {"id": tenant["id"]},
            {"$set": {"referral_active": True,
                      "referral_active_source": "conversion"}})
    await _emit_referral_payment(tenant, obj, event, paid_at)


async def _on_invoice_failed(obj):
    sub_id = _invoice_subscription_id(obj)
    if not sub_id:
        return
    tenant = await _resolve_tenant(subscription_id=sub_id,
                                   customer_id=obj.get("customer"))
    if not tenant:
        return
    await _set_billing(tenant, {
        "status": "past_due",
        "last_payment_failed_at": now_iso(),
        "last_failed_invoice_id": obj.get("id"),
    })
    await _audit_system(tenant["id"], "billing.payment_failed",
                        {"invoice_id": obj.get("id")})


async def _emit_referral_payment(tenant, invoice, event, paid_at):
    """One durable referral payment event per successful subscription
    payment, in the empire ledger's ingest shape (scope doc: product,
    referred account, payment id, amount, currency, paid_at). Emission
    ONLY — the 25% / window / unlock computation lives in the ledger,
    a later stage. Exactly-once is belt-and-braces: the webhook claim
    stops a redelivered event, and the (product, payment_id) check
    stops a second event for the same invoice."""
    amount = invoice.get("amount_paid") or 0
    payment_id = invoice.get("id")
    if amount <= 0 or not payment_id:
        return
    existing = await db.referral_payment_events.find_one(
        {"product": "books", "payment_id": payment_id})
    if existing:
        return
    owner = await db.users.find_one(
        {"tenant_id": tenant["id"], "role": "owner"}, {"_id": 0, "email": 1})
    doc = {
        "id": new_id(),
        "type": "referral.payment",
        "product": "books",
        "tenant_id": tenant["id"],
        "referred_account_id": tenant["id"],
        "referred_email": (owner or {}).get("email"),
        "referral_code": tenant.get("referred_by_code"),
        "payment_id": payment_id,
        "stripe_event_id": event.get("id"),
        "amount_cents": amount,
        "currency": (invoice.get("currency") or "usd").lower(),
        "paid_at": paid_at or now_iso(),
        "emitted_at": now_iso(),
    }
    try:
        await db.referral_payment_events.insert_one(doc)
    except DuplicateKeyError:
        logger.info("referral payment already emitted invoice=%s", payment_id)


async def _process_event(event):
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if etype == "checkout.session.completed":
        await _on_checkout_completed(obj)
    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        await _on_subscription_upsert(obj)
    elif etype == "customer.subscription.deleted":
        await _on_subscription_deleted(obj)
    elif etype == "invoice.payment_succeeded":
        await _on_invoice_paid(obj, event)
    elif etype == "invoice.payment_failed":
        await _on_invoice_failed(obj)
    else:
        logger.info("billing webhook ignored event type=%s id=%s",
                    etype, event.get("id"))


@router.post("/stripe/webhook")
async def billing_webhook(request: Request):
    """Public (Stripe server-to-server): signature verification is
    mandatory — without a webhook secret the endpoint refuses (503).
    Deliveries are claimed by event id before processing; a redelivery
    of a processed event returns 200 without reprocessing."""
    if not billing.webhook_secret():
        raise HTTPException(
            status_code=503,
            detail="Stripe billing webhook is not configured. Set "
                   "STRIPE_BILLING_WEBHOOK_SECRET (see CHECKOUT_BUILD.md).")
    if not billing.configured():
        raise HTTPException(
            status_code=503,
            detail="Online billing is not configured. Set STRIPE_SECRET_KEY "
                   "(see CHECKOUT_BUILD.md).")
    try:
        payload = await request.body()
        event = billing.construct_webhook_event(
            payload, request.headers.get("stripe-signature", ""))
    except billing.WebhookSignatureError:
        logger.warning("billing webhook signature verification failed")
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
                  "source": "books_billing", "status": "claimed",
                  "received_at": now_iso()}},
        upsert=True)
    try:
        await _process_event(event)
    except Exception as e:
        await db.stripe_webhook_events.update_one(
            {"id": event_id},
            {"$set": {"status": "error", "error": str(e)[:300],
                      "updated_at": now_iso()}})
        logger.warning("billing webhook processing failed event=%s: %s",
                       event_id, type(e).__name__)
        raise HTTPException(status_code=500, detail="Webhook processing failed")
    await db.stripe_webhook_events.update_one(
        {"id": event_id},
        {"$set": {"status": "processed", "processed_at": now_iso()}})
    return {"received": True}
