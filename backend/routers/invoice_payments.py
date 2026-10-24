"""Invoice payments via Stripe Connect — the member's own Stripe.

Design: Trade Circle docs/invoice-payments.md. An invoice gets a pay
link; the customer pays on a Checkout Session created as a DIRECT
CHARGE on the tenant's connected Express account (tenant = merchant
of record; funds never enter this platform's balance), with our
application fee applied (lib/stripe_connect.py holds the schedule).
The Connect webhook reconciles: fulfillment posts the same
Dr 1050 Stripe Clearing / Cr 1200 AR entry the portal flow uses,
exactly once per session, and every event id is claimed in
stripe_webhook_events before processing so reconciliation is
idempotent by event id. Refunds/disputes are recorded as payment
STATES only — money moves in the member's Stripe, and the existing
/invoices/{id}/refund endpoint remains the ledger path for them.

Key-gated like the portal flow: until STRIPE_SECRET_KEY is set,
member endpoints return a graceful 503 and the webhook refuses
without STRIPE_CONNECT_WEBHOOK_SECRET. Nothing here logs a key or a
secret — log lines carry ids and amounts only.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, now_iso, today_str
from lib.currencies import normalize as normalize_currency
from lib import ledger as L
from lib import posting as P
from lib import fx_posting as FXP
from lib import stripe_connect as SC

logger = logging.getLogger("mindatease.invoice_payments")

router = APIRouter(prefix="/invoice-payments", tags=["invoice-payments"])

WEBHOOK_ACTOR = {"id": "stripe_connect", "email": "stripe-connect@system",
                 "name": "Stripe Connect webhook", "role": "owner"}


# ------------------------------------------------------------------ helpers

def _require_platform():
    if not SC.configured():
        raise HTTPException(
            status_code=503,
            detail="Invoice payments are not configured. Set STRIPE_SECRET_KEY "
                   "to enable them (see STRIPE_CONNECT_SETUP.md).")


def _origin(body_origin: str, request: Request) -> str:
    origin = (body_origin or request.headers.get("origin") or "").rstrip("/")
    if not origin or not origin.startswith("http"):
        raise HTTPException(status_code=422, detail="origin_url is required")
    return origin


async def _tenant(user) -> dict:
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    if not tenant:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return tenant


def _connect_of(tenant: dict) -> dict:
    return (tenant or {}).get("stripe_connect") or {}


def _connect_view(tenant: dict, source: str = "stored") -> dict:
    conn = _connect_of(tenant)
    acct = conn.get("account_id") or ""
    return {
        "configured": SC.configured(),
        "connected": bool(acct),
        "account_tail": acct[-4:] if acct else None,
        "charges_enabled": bool(conn.get("charges_enabled")),
        "payouts_enabled": bool(conn.get("payouts_enabled")),
        "details_submitted": bool(conn.get("details_submitted")),
        "state_source": source,
    }


async def _persist_account_state(tenant_id: str, account) -> dict:
    state = SC.account_state(account)
    state["updated_at"] = now_iso()
    existing = await db.tenants.find_one({"id": tenant_id}, {"_id": 0})
    onboarded_at = _connect_of(existing or {}).get("onboarded_at")
    if onboarded_at:
        state["onboarded_at"] = onboarded_at
    await db.tenants.update_one(
        {"id": tenant_id}, {"$set": {"stripe_connect": state}})
    return state


async def _refresh_from_stripe(tenant: dict) -> dict:
    """Best-effort live state: retrieve the account and persist it.
    Falls back to the stored state when Stripe can't be reached —
    the answer says which one it is."""
    conn = _connect_of(tenant)
    if not conn.get("account_id") or not SC.configured():
        return _connect_view(tenant)
    try:
        account = SC.retrieve_account(conn["account_id"])
    except Exception as e:
        logger.warning("connect retrieve failed tenant=%s: %s",
                       tenant["id"], type(e).__name__)
        return _connect_view(tenant)
    state = await _persist_account_state(tenant["id"], account)
    return _connect_view({**tenant, "stripe_connect": state}, source="stripe")


# ------------------------------------------------------------------ Connect onboarding

class OriginIn(BaseModel):
    origin_url: str = ""


@router.post("/connect/start")
async def connect_start(body: OriginIn, request: Request,
                        user: dict = Depends(require("ledger:admin"))):
    """Create the tenant's Express account (once) and return a fresh
    Stripe-hosted onboarding link."""
    _require_platform()
    origin = _origin(body.origin_url, request)
    tenant = await _tenant(user)
    conn = _connect_of(tenant)
    account_id = conn.get("account_id")
    if not account_id:
        try:
            account = SC.create_account(tenant["id"], user.get("email", ""))
        except Exception as e:
            logger.warning("connect account create failed tenant=%s: %s",
                           tenant["id"], type(e).__name__)
            raise HTTPException(status_code=502,
                                detail="Stripe couldn't start onboarding — try again in a bit.")
        account_id = account["id"] if isinstance(account, dict) else account.id
        await db.tenants.update_one(
            {"id": tenant["id"]},
            {"$set": {"stripe_connect": {
                "account_id": account_id, "charges_enabled": False,
                "payouts_enabled": False, "details_submitted": False,
                "onboarded_at": now_iso(), "updated_at": now_iso()}}})
        await audit(user, "invoice_payments.connect_started", "tenant",
                    tenant["id"], {"account_tail": account_id[-4:]})
    try:
        link = SC.create_account_link(account_id, origin)
    except Exception as e:
        logger.warning("connect account link failed tenant=%s: %s",
                       tenant["id"], type(e).__name__)
        raise HTTPException(status_code=502,
                            detail="Stripe couldn't create the onboarding link — try again in a bit.")
    url = link["url"] if isinstance(link, dict) else link.url
    return {"url": url, "account_tail": account_id[-4:],
            "state": await _refresh_from_stripe(
                {**tenant, "stripe_connect": {**conn, "account_id": account_id}})}


@router.post("/connect/link")
async def connect_link(body: OriginIn, request: Request,
                       user: dict = Depends(require("ledger:admin"))):
    """A fresh onboarding link for a tenant that already has an account
    (links expire; this is also how an interrupted onboarding resumes)."""
    _require_platform()
    origin = _origin(body.origin_url, request)
    tenant = await _tenant(user)
    account_id = _connect_of(tenant).get("account_id")
    if not account_id:
        raise HTTPException(status_code=409,
                            detail="No Stripe account yet — start onboarding first.")
    try:
        link = SC.create_account_link(account_id, origin)
    except Exception as e:
        logger.warning("connect account link failed tenant=%s: %s",
                       tenant["id"], type(e).__name__)
        raise HTTPException(status_code=502,
                            detail="Stripe couldn't create the onboarding link — try again in a bit.")
    url = link["url"] if isinstance(link, dict) else link.url
    return {"url": url, "account_tail": account_id[-4:]}


@router.get("/connect/status")
async def connect_status(user: dict = Depends(require("ledger:admin"))):
    tenant = await _tenant(user)
    return await _refresh_from_stripe(tenant)


@router.post("/connect/sync")
async def connect_sync(user: dict = Depends(require("ledger:admin"))):
    _require_platform()
    tenant = await _tenant(user)
    if not _connect_of(tenant).get("account_id"):
        raise HTTPException(status_code=409,
                            detail="No Stripe account connected yet.")
    return await _refresh_from_stripe(tenant)


# ------------------------------------------------------------------ pay links

class PayLinkIn(BaseModel):
    rail: str = Field(default="card", pattern=r"^(card|ach)$")
    origin_url: str = ""


async def _set_invoice_payment(tid: str, inv_id: str, payment: dict):
    await db.invoices.update_one(
        {"tenant_id": tid, "id": inv_id}, {"$set": {"payment": payment}})


@router.post("/invoices/{invoice_id}/pay-link")
async def create_pay_link(invoice_id: str, body: PayLinkIn, request: Request,
                          user: dict = Depends(require("ops:manage"))):
    """Create a Checkout Session (direct charge) for the invoice's
    outstanding balance on the tenant's connected account, with the
    application fee for the chosen rail applied."""
    _require_platform()
    origin = _origin(body.origin_url, request)
    tid = user["tenant_id"]
    inv = await Scoped(db.invoices, tid).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    if balance <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    currency = normalize_currency(inv.get("currency") or "USD")
    if currency != "USD":
        raise HTTPException(
            status_code=422,
            detail="Pay links are USD-only for now — this invoice is in "
                   f"{currency}. Record its payments manually.")
    tenant = await _tenant(user)
    view = await _refresh_from_stripe(tenant)
    if not view["connected"]:
        raise HTTPException(status_code=409, detail={
            "message": "Connect your Stripe first — pay links run on your "
                       "own Stripe account.",
            "connect": view})
    if not view["charges_enabled"]:
        raise HTTPException(status_code=409, detail={
            "message": "Your Stripe onboarding isn't finished — charges "
                       "aren't enabled yet, so a pay link can't take money.",
            "connect": view})
    conn = _connect_of(tenant)
    account_id = conn["account_id"]
    fees = SC.fees_for(body.rail, balance)
    tenant_name = tenant.get("name", "")
    label = f"Invoice {inv.get('issue_entry') or inv['id']}"
    if tenant_name:
        label = f"{tenant_name} — {label}"
    try:
        session = SC.create_checkout_session(
            account_id=account_id, amount_cents=balance, currency="usd",
            label=label, description=f"Balance due: {inv.get('client_name', '')}",
            rail=body.rail,
            application_fee_cents=fees["application_fee_cents"],
            origin_url=origin, invoice_id=inv["id"], tenant_id=tid)
    except Exception as e:
        logger.warning("connect checkout create failed invoice=%s amount=%s: %s",
                       inv["id"], balance, type(e).__name__)
        raise HTTPException(status_code=502,
                            detail="Payment link unavailable — please try again.")
    sess_id = session["id"] if isinstance(session, dict) else session.id
    sess_url = session["url"] if isinstance(session, dict) else session.url
    await db.payment_transactions.insert_one({
        "session_id": sess_id, "tenant_id": tid, "kind": "connect_invoice",
        "invoice_id": inv["id"], "account_id": account_id, "rail": body.rail,
        "amount_cents": balance, "currency": "usd",
        "application_fee_cents": fees["application_fee_cents"],
        "stripe_fee_estimate_cents": fees["stripe_fee_estimate_cents"],
        "status": "initiated", "payment_status": "pending", "fulfilled": False,
        "created_at": now_iso(), "updated_at": now_iso(),
    })
    payment = {
        **(inv.get("payment") or {}),
        "state": "link_created", "rail": body.rail, "session_id": sess_id,
        "pay_url": sess_url, "link_created_at": now_iso(),
        "amount_cents": balance,
        "application_fee_cents": fees["application_fee_cents"],
        "stripe_fee_estimate_cents": fees["stripe_fee_estimate_cents"],
        "updated_at": now_iso(),
    }
    await _set_invoice_payment(tid, inv["id"], payment)
    await audit(user, "invoice_payments.pay_link_created", "invoice", inv["id"],
                {"session_id": sess_id, "rail": body.rail,
                 "amount_cents": balance,
                 "application_fee_cents": fees["application_fee_cents"]})
    logger.info("connect checkout created session=%s invoice=%s amount=%s rail=%s",
                sess_id, inv["id"], balance, body.rail)
    return {"pay_url": sess_url, "session_id": sess_id,
            "payment_state": "link_created", "rail": body.rail,
            "amount_cents": balance, "fees": fees}


@router.get("/invoices/{invoice_id}")
async def invoice_payment_state(invoice_id: str,
                                user: dict = Depends(require("ledger:read"))):
    inv = await Scoped(db.invoices, user["tenant_id"]).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    paid = inv.get("paid_cents", 0)
    return {
        "invoice_id": inv["id"], "status": inv.get("status"),
        "total_cents": inv["total_cents"], "paid_cents": paid,
        "balance_cents": inv["total_cents"] - paid,
        "payment": inv.get("payment") or {"state": "none"},
    }


# ------------------------------------------------------------------ webhook reconciliation

async def _set_payment_state(tid: str, inv_id: str, **fields):
    inv = await db.invoices.find_one({"tenant_id": tid, "id": inv_id})
    if not inv:
        return
    payment = {**(inv.get("payment") or {}), **fields, "updated_at": now_iso()}
    await _set_invoice_payment(tid, inv_id, payment)


async def fulfill_connect_invoice(session_id: str) -> None:
    """Exactly-once (the portal claim pattern): post Dr 1050 / Cr 1200,
    append the payment, flip the invoice's payment state to paid."""
    rec = await db.payment_transactions.find_one_and_update(
        {"session_id": session_id, "kind": "connect_invoice",
         "payment_status": "paid", "fulfilled": False, "fulfilling": {"$ne": True}},
        {"$set": {"fulfilling": True, "updated_at": now_iso()}})
    if not rec:
        return
    err, tid, inv_id = "", rec["tenant_id"], rec["invoice_id"]
    try:
        inv = await db.invoices.find_one({"tenant_id": tid, "id": inv_id})
        if not inv:
            raise ValueError("Invoice no longer exists")
        balance = inv["total_cents"] - inv.get("paid_cents", 0)
        if balance <= 0:
            raise ValueError("Invoice was already paid in full — refund the "
                             "card payment manually in Stripe")
        cents = min(rec["amount_cents"], balance)
        actor = {**WEBHOOK_ACTOR, "tenant_id": tid}
        entry = await L.post_journal_entry(db, tid, actor, {
            "entry_date": today_str(), "source": "stripe_connect",
            "source_ref": inv_id,
            "memo": f"Stripe Connect payment — {inv.get('client_name', '')}",
            "lines": [P.dr("1050", cents, "Stripe Connect payment"),
                      P.cr("1200", cents, "AR cleared")],
            "currency": "USD",
        }, status="posted")
        await FXP.post_realized_fx(
            db=db, tenant_id=tid, user=actor, kind="invoice", doc=inv,
            pay_currency="USD", pay_amount_minor=cents,
            applied_doc_minor=cents, pay_date=today_str())
        new_paid = inv.get("paid_cents", 0) + cents
        status = "paid" if new_paid >= inv["total_cents"] else "partial"
        pmt = {"date": today_str(), "amount_cents": cents, "cash_cents": cents,
               "credit_cents": 0,
               "method": "bank" if rec.get("rail") == "ach" else "card",
               "processor": "stripe", "reference": session_id,
               "entry": entry["number"], "source": "stripe_connect",
               "application_fee_cents": rec.get("application_fee_cents")}
        await db.invoices.update_one(
            {"tenant_id": tid, "id": inv_id},
            {"$set": {"status": status, "paid_cents": new_paid},
             "$push": {"payments": pmt}})
        await _set_payment_state(
            tid, inv_id, state="paid", paid_at=now_iso(),
            paid_session_id=session_id)
        await audit(actor, "invoice_payments.fulfilled", "invoice", inv_id,
                    {"session_id": session_id, "amount_cents": cents,
                     "entry_number": entry["number"]})
    except Exception as e:
        err = str(e)
    await db.payment_transactions.update_one(
        {"session_id": session_id},
        {"$set": {"fulfilled": not err, "fulfilling": False, "error": err,
                  "updated_at": now_iso()}})


async def _mark_session_paid(session, event) -> None:
    session_id = session["id"] if isinstance(session, dict) else session.id
    metadata = (session.get("metadata") if isinstance(session, dict)
                else getattr(session, "metadata", None)) or {}
    if metadata.get("kind") != "connect_invoice":
        return  # not ours — some other flow on the platform account
    payment_status = (session.get("payment_status") if isinstance(session, dict)
                      else getattr(session, "payment_status", None))
    tx = await db.payment_transactions.find_one({"session_id": session_id})
    if not tx:
        logger.warning("connect webhook: unknown session=%s", session_id)
        return
    pi = (session.get("payment_intent") if isinstance(session, dict)
          else getattr(session, "payment_intent", None))
    if pi:
        await db.payment_transactions.update_one(
            {"session_id": session_id}, {"$set": {"payment_intent_id": pi}})
    if payment_status != "paid":
        # ACH in flight: checkout completed, funds not yet settled.
        await _set_payment_state(tx["tenant_id"], tx["invoice_id"],
                                 state="pending",
                                 last_event_id=event.get("id"))
        logger.info("connect session=%s pending (async rail)", session_id)
        return
    await db.payment_transactions.update_one(
        {"session_id": session_id},
        {"$set": {"payment_status": "paid", "status": "paid",
                  "updated_at": now_iso()}})
    await fulfill_connect_invoice(session_id)


async def _handle_charge_event(event, *, state: str, amount_field: str = None):
    obj = event["data"]["object"]
    pi = obj.get("payment_intent") if isinstance(obj, dict) else getattr(obj, "payment_intent", None)
    if not pi:
        return
    tx = await db.payment_transactions.find_one({"payment_intent_id": pi})
    if not tx:
        return  # a charge we didn't originate — record nothing
    fields = {"state": state, "last_event_id": event.get("id")}
    if amount_field:
        amt = obj.get(amount_field) if isinstance(obj, dict) else getattr(obj, amount_field, None)
        if amt is not None:
            fields["refunded_cents" if state == "refunded" else "disputed_cents"] = amt
    await _set_payment_state(tx["tenant_id"], tx["invoice_id"], **fields)
    logger.info("connect %s recorded invoice=%s (state only)", state, tx["invoice_id"])


async def _process_event(event) -> None:
    etype = event["type"]
    obj = event["data"]["object"]
    if etype in ("checkout.session.completed",
                 "checkout.session.async_payment_succeeded"):
        await _mark_session_paid(obj, event)
    elif etype == "checkout.session.async_payment_failed":
        session_id = obj["id"] if isinstance(obj, dict) else obj.id
        tx = await db.payment_transactions.find_one({"session_id": session_id})
        if tx:
            await db.payment_transactions.update_one(
                {"session_id": session_id},
                {"$set": {"payment_status": "failed", "status": "failed",
                          "updated_at": now_iso()}})
            await _set_payment_state(tx["tenant_id"], tx["invoice_id"],
                                     state="failed",
                                     last_event_id=event.get("id"))
    elif etype == "charge.refunded":
        await _handle_charge_event(event, state="refunded",
                                   amount_field="amount_refunded")
    elif etype == "charge.dispute.created":
        await _handle_charge_event(event, state="disputed",
                                   amount_field="amount")
    elif etype == "account.updated":
        account_id = obj["id"] if isinstance(obj, dict) else obj.id
        tenant = await db.tenants.find_one(
            {"stripe_connect.account_id": account_id}, {"_id": 0, "id": 1})
        if tenant:
            await _persist_account_state(tenant["id"], obj)
    elif etype == "account.application.deauthorized":
        account_id = event.get("account")
        if account_id:
            tenant = await db.tenants.find_one(
                {"stripe_connect.account_id": account_id}, {"_id": 0, "id": 1})
            if tenant:
                await db.tenants.update_one(
                    {"id": tenant["id"]},
                    {"$set": {"stripe_connect": {
                        "account_id": None, "charges_enabled": False,
                        "payouts_enabled": False, "details_submitted": False,
                        "deauthorized_at": now_iso(),
                        "updated_at": now_iso()}}})
    else:
        logger.info("connect webhook ignored event type=%s id=%s",
                    etype, event.get("id"))


@router.post("/stripe/webhook")
async def connect_webhook(request: Request):
    """Public (Stripe server-to-server): signature verification is
    mandatory — without STRIPE_CONNECT_WEBHOOK_SECRET the endpoint
    refuses (503): an unverifiable webhook never moves money.

    Idempotent by event id: the id is claimed in stripe_webhook_events
    before processing; a redelivery of a processed event returns 200
    without reprocessing, and fulfillment itself is claim-guarded, so
    even a reprocessed event cannot double-post."""
    if not SC.connect_webhook_secret():
        raise HTTPException(
            status_code=503,
            detail="Stripe Connect webhook is not configured. Set "
                   "STRIPE_CONNECT_WEBHOOK_SECRET (see STRIPE_CONNECT_SETUP.md).")
    try:
        payload = await request.body()
        event = SC.construct_webhook_event(
            payload, request.headers.get("stripe-signature", ""))
    except SC.ConnectNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except SC.WebhookSignatureError:
        logger.warning("connect webhook signature verification failed")
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
                  "account": event.get("account"), "status": "claimed",
                  "received_at": now_iso()}},
        upsert=True)
    try:
        await _process_event(event)
    except Exception as e:
        await db.stripe_webhook_events.update_one(
            {"id": event_id},
            {"$set": {"status": "error", "error": str(e)[:300],
                      "updated_at": now_iso()}})
        logger.warning("connect webhook processing failed event=%s: %s",
                       event_id, type(e).__name__)
        raise HTTPException(status_code=500, detail="Webhook processing failed")
    await db.stripe_webhook_events.update_one(
        {"id": event_id},
        {"$set": {"status": "processed", "processed_at": now_iso()}})
    return {"received": True}
