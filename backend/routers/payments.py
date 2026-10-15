"""Card payments at Quick sell via Stripe Checkout (claimable sandbox). Stock + income are posted only once Stripe says paid."""

import logging
import os
import uuid

import stripe
from pymongo.errors import DuplicateKeyError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from lib.auth import Principal, principal_for_user, require
from lib.dates import now_iso
from lib.db import db
from lib.money import D, discounted_unit_price, line_total, money, msum
from lib.stripe_resources import stripe_dict
from lib.repo import Scoped
from models.inventory import CheckoutIn

router = APIRouter()
logger = logging.getLogger(__name__)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY") or "sk_test_emergent"
TAX_MODE = "calc_only"  # physical retail goods: Stripe calculates tax at checkout, the shop files returns


class CardCheckoutIn(CheckoutIn):
    origin_url: str = Field(min_length=8, max_length=300)


class CardCheckoutOut(BaseModel):
    checkout_url: str
    session_id: str


class PaymentStatus(BaseModel):
    currency: str = "USD"
    session_id: str
    status: str
    payment_status: str
    fulfilled: bool = False
    error: str = ""
    total: float = 0


@router.post("/payments/quick-sell", response_model=CardCheckoutOut)
async def card_checkout(body: CardCheckoutIn, p: Principal = Depends(require("stock:write"))):
    if not p.settings.get("inventory_enabled"):
        raise HTTPException(404, "Not found")
    names, items, total = [], [], 0.0
    pct = 0.0
    if body.discount_code:  # check only; it's consumed when the sale is posted
        from routers.clients import check_discount
        dc = await check_discount(body.discount_code, p)
        if not dc.valid:
            raise HTTPException(400, f"Discount code: {dc.reason}")
        pct = dc.pct
    for ln in body.lines:
        prod = await Scoped("products", p).find_one({"id": ln.product_id, "active": True})
        if not prod:
            raise HTTPException(404, "Product not found")
        if prod["quantity_on_hand"] < ln.quantity and not p.settings.get("allow_negative_stock"):
            raise HTTPException(409, f"Not enough stock of {prod['name']}")
        unit = discounted_unit_price(ln.unit_price, pct)
        total = msum([total, line_total(ln.quantity, unit)])
        names.append(prod["name"])
        items.append({"price_data": {"currency": p.settings.get("currency", "USD").lower(), "unit_amount": int(D(unit) * 100), "tax_behavior": "exclusive",
                                     "product_data": {"name": prod["name"], "tax_code": "txcd_99999999"}}, "quantity": ln.quantity})
    if total <= 0:
        raise HTTPException(400, "Nothing to charge")
    origin = body.origin_url.rstrip("/")
    kwargs = dict(mode="payment", line_items=items, success_url=f"{origin}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
                  cancel_url=f"{origin}/payment/cancel", metadata={"tenant_id": p.tenant_id, "kind": "quick_sell"})
    try:
        session = stripe.checkout.Session.create(**kwargs, automatic_tax={"enabled": True}, billing_address_collection="required")
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Card payment unavailable: {e.user_message or 'Stripe error'}")
    cart = body.model_dump(exclude={"origin_url"})
    cart["payment_method"] = "card"
    await db.payment_transactions.insert_one({
        "session_id": session.id, "tenant_id": p.tenant_id, "user_id": p.user_id, "amount": money(total), "currency": p.settings.get("currency", "USD").lower(),
        "cart": cart, "status": "initiated", "payment_status": "pending", "fulfilled": False, "created_at": now_iso(), "updated_at": now_iso()})
    return CardCheckoutOut(checkout_url=session.url, session_id=session.id)


async def fulfill(session_id: str) -> None:
    """Exactly-once: claim the paid record, then post the sale (stock + income). If stock vanished meanwhile, refund."""
    rec = await db.payment_transactions.find_one_and_update(
        {"session_id": session_id, "payment_status": "paid", "fulfilled": False, "fulfilling": {"$ne": True}}, {"$set": {"fulfilling": True, "updated_at": now_iso()}})
    if not rec:
        return
    from routers.movements import do_checkout
    user = await db.users.find_one({"id": rec["user_id"], "tenant_id": rec["tenant_id"]}, {"_id": 0})
    p = await principal_for_user(user) if user else None
    err, txn = "", None
    try:
        if not p:
            raise HTTPException(409, "The cashier account no longer exists")
        res = await do_checkout(CheckoutIn(**rec["cart"]), p, f"stripe:{session_id}", None, processor="stripe")
        txn = res.transaction_id
        if txn:
            await db.transactions.update_one({"id": txn}, {"$set": {"stripe_session_id": session_id,
                                                                     "stripe_payment_intent_id": rec.get("stripe_payment_intent_id")}})
    except HTTPException as e:
        err = str(e.detail)
        try:
            if rec.get("stripe_payment_intent_id"):
                stripe.Refund.create(payment_intent=rec["stripe_payment_intent_id"])
                err += " — card refunded automatically"
        except stripe.error.StripeError as se:
            err += f" — refund failed, refund manually in Stripe ({se.user_message})"
    await db.payment_transactions.update_one({"session_id": session_id}, {"$set": {"fulfilled": not err, "fulfilling": False, "error": err,
                                                                                    "transaction_id": txn, "updated_at": now_iso()}})


async def _mark_paid(session_id: str, pi: str | None) -> None:
    await db.payment_transactions.update_one({"session_id": session_id, "payment_status": {"$ne": "paid"}},
                                             {"$set": {"status": "completed", "payment_status": "paid", "stripe_payment_intent_id": pi, "updated_at": now_iso()}})


@router.get("/payments/status/{session_id}", response_model=PaymentStatus)
async def payment_status(session_id: str):
    rec = await db.payment_transactions.find_one({"session_id": session_id})
    if not rec:
        raise HTTPException(404, "Transaction not found")
    if rec["payment_status"] != "paid":
        try:
            s = stripe_dict(stripe.checkout.Session.retrieve(session_id))
            if s.get("payment_status") == "paid":
                await _mark_paid(session_id, s.get("payment_intent"))
            elif s.get("status") == "expired":
                await db.payment_transactions.update_one({"session_id": session_id}, {"$set": {"status": "expired", "payment_status": "expired"}})
        except stripe.error.StripeError:
            pass
    await fulfill(session_id)
    rec = await db.payment_transactions.find_one({"session_id": session_id})
    return PaymentStatus(session_id=session_id, status=rec["status"], payment_status=rec["payment_status"], fulfilled=rec.get("fulfilled", False),
                         error=rec.get("error", ""), total=rec["amount"], currency=rec.get("currency", "usd").upper())


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), os.environ.get("STRIPE_WEBHOOK_SECRET", ""))
    except (stripe.error.SignatureVerificationError, ValueError):
        raise HTTPException(400, "Invalid signature")
    # Stripe 16 objects are not mappings. Normalize only after signature verification.
    event = stripe_dict(event)
    obj, t = event["data"]["object"], event["type"]
    try:  # redelivered events are acknowledged but never processed twice
        await db.stripe_events.insert_one({"id": event["id"], "type": t, "created": event.get("created"), "received_at": now_iso()})
    except DuplicateKeyError:
        return {"status": "duplicate"}
    if t.startswith(("customer.subscription.", "invoice.")) or (t == "checkout.session.completed" and obj.get("mode") == "subscription"):
        from routers.billing import handle_billing_event
        await handle_billing_event(event)
        return {"status": "ok"}
    if t in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and obj.get("payment_status") == "paid":
        await _mark_paid(obj["id"], obj.get("payment_intent"))
        await fulfill(obj["id"])
    elif t in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        await db.payment_transactions.update_one({"session_id": obj["id"]}, {"$set": {"status": t.rsplit(".", 1)[1], "payment_status": "failed"}})
    elif t == "charge.refunded":
        await db.payment_transactions.update_one({"stripe_payment_intent_id": obj.get("payment_intent")}, {"$set": {"status": "refunded", "payment_status": "refunded"}})
    return {"status": "ok"}


_ = uuid  # keep import for future idempotency keys
