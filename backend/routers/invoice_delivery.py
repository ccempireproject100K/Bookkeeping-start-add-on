"""Invoice delivery & online payment — the InvoiceEase fold-in (2026-11-14).

Bookkeeping already ISSUED invoices and posted them to the ledger but sent
nothing. This router adds the missing half, on top of the existing modules
(invoices / recurring_invoices / collections / portal), never duplicating
them:

  SEND        POST /invoices/{id}/send            (Resend email + SMS seam)
  PAY LINK    POST /invoices/{id}/pay-link        (Stripe Checkout, card+ACH)
  WEBHOOK     POST /invoice-payments/stripe/webhook  (signature-verified,
              fail-closed, deduped, tenant-matched; fulfills BOTH this
              router's sessions AND the pre-existing portal checkout
              sessions — the portal webhook in the unregistered
              payments.py router never ran, so portal card payments could
              never fulfill; this webhook closes that hole)
  TRACKING    GET  /c/{token} client page + /c/{token}/open.gif pixel,
              invoice timeline (sent -> viewed -> paid)
  NUDGES      wired into collections autopilot (see collections.py) with
              per-invoice / per-client pause + payment auto-pause
  AUTOPAY     SetupIntent enrollment + off-session charge, recurring hook
  SPLITS      POST /invoices/{id}/split (deposit + balance / milestones)
  CLIENT PAGE GET  /c/{token} — ALL of a client's invoices, no login
  DISPUTES    GET  /invoices/{id}/dispute-packet (zip: PDF + JSON + logs)

Money discipline: NO live charges in this build's proof — Stripe runs in
test mode / stubs. The platform fee is a CONFIG SEAM defaulting to ZERO
(lib/invoice_pay.FEE_DEFAULTS); the owner has not stamped fee numbers.
"""
import io
import json
import os
import zipfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib import ledger as L
from lib import posting as P
from lib import invoice_pay as IP

router = APIRouter(tags=["invoice-delivery"])

SYSTEM_ACTOR = {"id": "system", "email": "system@books", "name": "Invoice delivery",
                "role": "owner"}


def _inv_coll(tenant_id):
    return Scoped(db.invoices, tenant_id)


async def _get_invoice(tenant_id, invoice_id):
    inv = await _inv_coll(tenant_id).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


def _stripe():
    import stripe
    key = os.environ.get("STRIPE_SECRET_KEY") or ""
    if not key or key == "sk_test_emergent":
        raise HTTPException(status_code=501,
                            detail="Online payments aren't connected yet.")
    stripe.api_key = key
    return stripe


# ---------------- Fee / delivery config ----------------
class PayConfigBody(BaseModel):
    fee_percent: Optional[float] = None
    fee_fixed_cents: Optional[int] = None
    fee_methods: Optional[list] = None
    surcharge_enabled: Optional[bool] = None
    surcharge_percent: Optional[float] = None
    surcharge_fixed_cents: Optional[int] = None
    sms_enabled: Optional[bool] = None
    whatsapp_enabled: Optional[bool] = None
    nudges_paused: Optional[bool] = None
    from_name: Optional[str] = None
    early_pay_percent: Optional[float] = None
    early_pay_days: Optional[int] = None


@router.get("/invoice-payments/config")
async def read_pay_config(user: dict = Depends(require("ledger:read"))):
    return await IP.get_pay_config(user["tenant_id"])


@router.put("/invoice-payments/config")
async def write_pay_config(body: PayConfigBody, user: dict = Depends(require("ops:manage"))):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "fee_percent" in patch and not (0 <= patch["fee_percent"] <= 100):
        raise HTTPException(422, "fee_percent out of range")
    if "early_pay_percent" in patch and not (0 <= patch["early_pay_percent"] <= 100):
        raise HTTPException(422, "early_pay_percent out of range")
    if "early_pay_days" in patch and not (0 <= patch["early_pay_days"] <= 365):
        raise HTTPException(422, "early_pay_days out of range")
    cfg = await IP.set_pay_config(user["tenant_id"], patch)
    await audit(user, "invoice_pay.config", "config", "invoice_pay", patch)
    return cfg


# ---------------- SEND ----------------
class SendBody(BaseModel):
    channel: str = Field(default="email",
                         pattern=r"^(email|sms|whatsapp|both|all)$")


@router.post("/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: str, body: SendBody,
                       user: dict = Depends(require("ops:manage"))):
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    if inv["total_cents"] - inv.get("paid_cents", 0) <= 0:
        raise HTTPException(409, "Invoice already fully paid")
    results = []
    if body.channel in ("email", "both", "all"):
        results.append(await IP.send_invoice_email(user["tenant_id"], inv, "invoice", user))
    if body.channel in ("sms", "both", "all"):
        results.append(await IP.send_invoice_sms(user["tenant_id"], inv, "invoice"))
    if body.channel in ("whatsapp", "all"):
        results.append(await IP.send_invoice_whatsapp(user["tenant_id"], inv, "invoice"))
    await audit(user, "invoice.send", "invoice", invoice_id,
                {"channel": body.channel,
                 "statuses": [r["status"] for r in results]})
    return {"invoice_id": invoice_id, "sends": results}


@router.get("/invoices/{invoice_id}/timeline")
async def invoice_timeline(invoice_id: str, user: dict = Depends(require("ledger:read"))):
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    events = [{"event": "issued", "at": inv.get("created_at"), "detail": {}}]
    events += inv.get("timeline", [])
    for p in inv.get("payments", []):
        events.append({"event": "paid", "at": p.get("date"),
                       "detail": {"amount_cents": p.get("amount_cents"),
                                  "method": p.get("method"),
                                  "entry": p.get("entry")}})
    return {"invoice_id": invoice_id, "status": inv.get("status"),
            "sends": inv.get("sends", []), "events": events}


# ---------------- PAY LINK + auto-record ----------------
class PayLinkBody(BaseModel):
    method: str = Field(default="card", pattern=r"^(card|ach)$")
    origin_url: str = ""
    amount_cents: Optional[int] = None   # default: full balance (split parts pass theirs)


async def create_checkout_for_invoice(tenant_id: str, inv: dict, method: str,
                                      origin_url: str, token: str = "",
                                      amount_cents: int | None = None) -> dict:
    """Stripe Checkout session for an invoice (or one split part). Records a
    payment_transactions row the webhook fulfills exactly once."""
    stripe = _stripe()
    if inv.get("status") == "split":
        raise HTTPException(409, "Invoice was split — pay its parts instead")
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    amount = amount_cents if amount_cents is not None else balance
    if amount <= 0 or amount > balance:
        raise HTTPException(422, "Invalid payment amount for this invoice")
    cfg = await IP.get_pay_config(tenant_id)
    fee = IP.compute_fee_cents(cfg, amount, method)
    surcharge = IP.compute_surcharge_cents(cfg, amount, method)
    charge_amount = amount + surcharge
    tenant = await db.tenants.find_one({"id": tenant_id}, {"_id": 0, "name": 1, "settings": 1})
    currency = str(((tenant or {}).get("settings") or {}).get("currency", "USD")).lower()
    business = (tenant or {}).get("name", "")
    label = f"Invoice {inv.get('issue_entry') or inv['id'][:8]}"
    if business:
        label = f"{business} — {label}"
    origin = (origin_url or IP.site_base()).rstrip("/")
    if not origin.startswith("http"):
        raise HTTPException(422, "origin_url is required")
    pm_types = ["card", "us_bank_account"] if method == "ach" else ["card"]
    kwargs = dict(
        mode="payment",
        line_items=[{"price_data": {"currency": currency, "unit_amount": charge_amount,
                                     "product_data": {"name": label}},
                     "quantity": 1}],
        success_url=f"{origin}/c/{token}?paid=1&session_id={{CHECKOUT_SESSION_ID}}" if token
                    else f"{origin}/invoices?paid=1&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{origin}/c/{token}" if token else f"{origin}/invoices",
        metadata={"kind": "invoice_delivery", "tenant_id": tenant_id,
                  "invoice_id": inv["id"], "method": method,
                  "platform_fee_cents": str(fee), "surcharge_cents": str(surcharge),
                  "invoice_amount_cents": str(amount)},
        payment_method_types=pm_types,
    )
    if fee > 0 and os.environ.get("STRIPE_PLATFORM_FEE_MODE") == "application_fee":
        kwargs["payment_intent_data"] = {"application_fee_amount": fee}
    try:
        session = stripe.checkout.Session.create(**kwargs)
    except Exception as e:
        raise HTTPException(502, f"Card payment unavailable: {e}")
    await Scoped(db.payment_transactions, tenant_id).insert_one({
        "session_id": session.id, "kind": "invoice_delivery",
        "invoice_id": inv["id"], "amount_cents": amount,
        "charge_cents": charge_amount, "platform_fee_cents": fee,
        "surcharge_cents": surcharge, "method": method, "currency": currency,
        "status": "initiated", "payment_status": "pending", "fulfilled": False,
        "created_at": now_iso(), "updated_at": now_iso(),
    })
    return {"checkout_url": session.url, "session_id": session.id,
            "amount_cents": amount, "charge_cents": charge_amount,
            "platform_fee_cents": fee, "surcharge_cents": surcharge}


@router.post("/invoices/{invoice_id}/pay-link")
async def pay_link(invoice_id: str, body: PayLinkBody,
                   user: dict = Depends(require("ops:manage"))):
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    token = await IP.ensure_client_token(user["tenant_id"],
                                         inv.get("customer_id") or "",
                                         inv.get("client_name", ""))
    out = await create_checkout_for_invoice(user["tenant_id"], inv, body.method,
                                            body.origin_url, token, body.amount_cents)
    await audit(user, "invoice.pay_link", "invoice", invoice_id,
                {"method": body.method, "amount_cents": out["amount_cents"]})
    return out


async def record_online_payment(tenant_id: str, invoice_id: str, amount_cents: int,
                                method: str, reference: str, source: str,
                                fee_cents: int = 0) -> dict:
    """Post Dr Stripe-clearing(1050)/bank(1020) / Cr AR(1200) and update the
    invoice through the same totals math as the manual pay path. Clamps to
    the remaining balance; returns the payment summary."""
    inv = await _inv_coll(tenant_id).find_one({"id": invoice_id})
    if not inv:
        raise ValueError("Invoice no longer exists")
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    if balance <= 0:
        raise ValueError("Invoice already fully paid")
    cents = min(amount_cents, balance)
    # Early-pay discount: if this payment is the discounted full balance
    # inside the window, the difference is the sender's discount — posted
    # honestly to 4900 (Discounts & Promos), never a silent total change.
    discount = 0
    ep = IP.early_pay_info(inv, today_str())
    if (ep["active"] and ep["discount_cents"] > 0
            and ep["discounted_balance_cents"] <= cents < balance):
        discount = balance - cents
    actor = {**SYSTEM_ACTOR, "tenant_id": tenant_id}
    cash_code = "1050" if method == "card" else "1020"
    jl = [P.dr(cash_code, cents, "Online payment")]
    if discount > 0:
        jl.append(P.dr("4900", discount, "Early-pay discount"))
    jl.append(P.cr("1200", cents + discount, "AR cleared"))
    entry = await L.post_journal_entry(db, tenant_id, actor, {
        "entry_date": today_str(), "source": source, "source_ref": invoice_id,
        "memo": f"Online payment ({method}) — {inv.get('client_name', '')}",
        "lines": jl,
    }, status="posted")
    new_paid = inv.get("paid_cents", 0) + cents + discount
    status = "paid" if new_paid >= inv["total_cents"] else "partial"
    via_channel = IP.last_send_channel(inv)
    pmt = {"date": today_str(), "amount_cents": cents + discount,
           "cash_cents": cents,
           "credit_cents": 0, "discount_cents": discount,
           "method": method, "processor": "stripe",
           "reference": reference, "platform_fee_cents": fee_cents,
           "via_channel": via_channel,
           "entry": entry["number"], "source": source}
    upd = {"$set": {"status": status, "paid_cents": new_paid},
           "$push": {"payments": pmt}}
    if discount > 0:
        upd["$inc"] = {"discount_applied_cents": discount}
    await _inv_coll(tenant_id).update_one({"id": invoice_id}, upd)
    await IP.add_timeline(tenant_id, invoice_id, "paid",
                          {"amount_cents": cents, "method": method, "source": source,
                           "via_channel": via_channel})
    return {"status": status, "paid_cents": new_paid,
            "balance_cents": inv["total_cents"] - new_paid,
            "entry_number": entry["number"]}


# ---------------- Stripe webhook ----------------
@router.post("/invoice-payments/stripe/webhook")
async def invoice_stripe_webhook(request: Request):
    """Signature-verified, fail-closed, deduped. Handles:
      - checkout.session.completed (kind=invoice_delivery -> record payment;
        kind=portal_invoice -> existing portal fulfillment; mode=setup ->
        autopay enrollment)
      - checkout.session.async_payment_succeeded (ACH)
      - payment_intent.payment_failed with autopay metadata -> collections
    Tenant always comes from the stored payment_transactions record /
    verified session metadata, never from the request body alone."""
    import stripe
    from pymongo.errors import DuplicateKeyError
    secret = (os.environ.get("STRIPE_INVOICE_WEBHOOK_SECRET")
              or os.environ.get("STRIPE_WEBHOOK_SECRET") or "")
    if not secret:
        raise HTTPException(503, "Webhook not configured")
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY") or ""
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, request.headers.get("stripe-signature", ""), secret)
    except Exception:
        raise HTTPException(400, "Invalid signature")
    from lib.stripe_resources import stripe_dict
    event = stripe_dict(event)
    obj, etype = event["data"]["object"], event["type"]
    try:
        await db.stripe_events.insert_one({
            "id": event["id"], "type": etype, "created": event.get("created"),
            "received_at": now_iso(), "lane": "invoice_delivery"})
    except DuplicateKeyError:
        return {"status": "duplicate"}

    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        meta = obj.get("metadata") or {}
        kind = meta.get("kind")
        if obj.get("mode") == "setup" or kind == "autopay_setup":
            await _fulfill_autopay_setup(obj)
        elif kind == "portal_invoice":
            await db.payment_transactions.update_one(
                {"session_id": obj["id"]},
                {"$set": {"payment_status": "paid", "status": "completed",
                          "updated_at": now_iso()}})
            from routers.portal import fulfill_portal_invoice
            await fulfill_portal_invoice(obj["id"])
        elif kind == "invoice_delivery":
            await _fulfill_invoice_session(obj)
    elif etype == "payment_intent.payment_failed":
        meta = obj.get("metadata") or {}
        if meta.get("kind") == "autopay" and meta.get("tenant_id") and meta.get("invoice_id"):
            await _autopay_failure(meta["tenant_id"], meta["invoice_id"],
                                   obj.get("last_payment_error", {}).get("message", "payment failed"))
        elif meta.get("tenant_id") and meta.get("invoice_id"):
            await IP.add_timeline(meta["tenant_id"], meta["invoice_id"],
                                  "payment_failed",
                                  {"error": obj.get("last_payment_error", {}).get("message", ""),
                                   "event": etype})
    elif etype in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        await db.payment_transactions.update_one(
            {"session_id": obj.get("id")},
            {"$set": {"status": "failed", "payment_status": "failed",
                      "updated_at": now_iso()}})
        if etype == "checkout.session.async_payment_failed":
            rec = await db.payment_transactions.find_one({"session_id": obj.get("id")})
            if rec and rec.get("invoice_id") and rec.get("tenant_id"):
                await IP.add_timeline(rec["tenant_id"], rec["invoice_id"],
                                      "payment_failed",
                                      {"error": "Bank payment failed after checkout",
                                       "event": etype})
    elif etype == "charge.dispute.created":
        # Hold/dispute visibility (owner addendum 2026-11-14): we do NOT
        # promise no holds — processors hold funds for their own risk
        # checks. What the tenant gets is the state, in the timeline.
        rec = await db.payment_transactions.find_one(
            {"payment_intent": obj.get("payment_intent")})
        if rec and rec.get("invoice_id") and rec.get("tenant_id"):
            await IP.add_timeline(rec["tenant_id"], rec["invoice_id"],
                                  "dispute_opened",
                                  {"dispute_id": obj.get("id"),
                                   "reason": obj.get("reason", ""),
                                   "amount_cents": obj.get("amount")})
    return {"status": "ok"}


async def _fulfill_invoice_session(obj: dict):
    rec = await db.payment_transactions.find_one_and_update(
        {"session_id": obj["id"], "kind": "invoice_delivery",
         "fulfilled": False, "fulfilling": {"$ne": True}},
        {"$set": {"fulfilling": True, "payment_status": "paid",
                  "payment_intent": obj.get("payment_intent"),
                  "status": "completed", "updated_at": now_iso()}})
    if not rec:
        return
    err = ""
    try:
        await record_online_payment(
            rec["tenant_id"], rec["invoice_id"], rec["amount_cents"],
            rec.get("method", "card"), obj["id"], "stripe_invoice",
            fee_cents=rec.get("platform_fee_cents", 0))
    except Exception as e:
        err = str(e)
    await db.payment_transactions.update_one(
        {"session_id": obj["id"]},
        {"$set": {"fulfilled": not err, "fulfilling": False, "error": err,
                  "updated_at": now_iso()}})


async def _autopay_failure(tenant_id: str, invoice_id: str, message: str):
    """Failed auto-charge lands in the collections pipeline, never silent:
    flag on the invoice + a reminder-store record + timeline + audit."""
    await _inv_coll(tenant_id).update_one(
        {"id": invoice_id},
        {"$set": {"autopay_last_error": message, "autopay_failed_at": now_iso()}})
    inv = await _inv_coll(tenant_id).find_one({"id": invoice_id})
    await Scoped(db.payment_reminders, tenant_id).insert_one({
        "id": new_id(), "invoice_id": invoice_id,
        "client_name": (inv or {}).get("client_name"),
        "message": f"Automatic payment failed: {message}", "stage": "autopay_failed",
        "via": "autopay", "created_at": now_iso()})
    await IP.add_timeline(tenant_id, invoice_id, "autopay_failed", {"error": message})
    await audit({**SYSTEM_ACTOR, "tenant_id": tenant_id},
                "invoice.autopay_failed", "invoice", invoice_id, {"error": message})


# ---------------- CLIENT PAGE (tokenized, no login) ----------------
def _public_invoice(inv: dict) -> dict:
    paid = inv.get("paid_cents", 0)
    return {
        "id": inv["id"], "ref": inv.get("issue_entry") or inv["id"][:8],
        "client_name": inv.get("client_name"), "due_date": inv.get("due_date"),
        "terms": inv.get("terms"), "status": inv.get("status"),
        "created_at": inv.get("created_at"),
        "lines": [{"name": ln.get("name"), "kind": ln.get("kind"),
                   "amount_cents": to_cents(ln.get("amount"))}
                  for ln in inv.get("lines", []) or []],
        "subtotal_cents": inv.get("subtotal_cents", 0),
        "tax_cents": inv.get("tax_cents", 0),
        "total_cents": inv.get("total_cents", 0), "paid_cents": paid,
        "balance_cents": inv.get("total_cents", 0) - paid,
        "split_label": inv.get("split_label"), "is_split_part": bool(inv.get("split_parent_id")),
        # Contract + sign (self-built e-sign, audit trail on the record)
        "agreement": ({
            "required": IP.signature_required(inv),
            "text": inv.get("agreement_text") or "",
            "signed": bool(inv.get("signature")),
            "signer_name": (inv.get("signature") or {}).get("signer_name"),
            "signed_at": (inv.get("signature") or {}).get("signed_at"),
        } if (inv.get("agreement_text") or "").strip() else None),
        # Job photos (metadata only; bytes served via the photo endpoint)
        "photos": [{"id": p["id"], "caption": p.get("caption", ""),
                    "kind": p.get("kind", "job")}
                   for p in inv.get("photos", [])],
        # Early-pay discount offer (sender-set; default off)
        "early_pay": IP.early_pay_info(inv),
    }


async def _client_invoices(link: dict) -> list:
    tid = link["tenant_id"]
    if link.get("customer_id"):
        rows = await _inv_coll(tid).find_list({"customer_id": link["customer_id"]},
                                              sort=[("created_at", -1)], limit=200)
    else:
        rows = await _inv_coll(tid).find_list({"client_name": link.get("client_name")},
                                              sort=[("created_at", -1)], limit=200)
    return rows


@router.get("/c/{token}")
async def client_page(token: str):
    """Public: ALL of this client's invoices (open + history) in one place,
    no login. Token is unguessable, tenant-scoped, revocable."""
    link = await IP.resolve_client_token(token)
    if not link:
        raise HTTPException(404, "Link not found")
    tenant = await db.tenants.find_one({"id": link["tenant_id"]},
                                       {"_id": 0, "name": 1, "settings": 1,
                                        "branding": 1})
    invs = await _client_invoices(link)
    # Split parents are containers — their parts carry the balances. Listing
    # the parent would double-count the total and offer a bogus pay button.
    invs = [i for i in invs if i.get("status") != "split"]
    cfg = await IP.get_pay_config(link["tenant_id"])
    open_invs = []
    for i in invs:
        if i["total_cents"] - i.get("paid_cents", 0) <= 0:
            continue
        pub = _public_invoice(i)
        bal = pub["balance_cents"]
        # BANK STEERING (owner addition 2026-11-14): card vs bank shown
        # side by side with the payer's total under each. The platform's
        # bank price comes from the fee seam (owner-directed 1.5% on ACH,
        # set in config at deploy); card carries the surcharge pass-through
        # when the tenant enables it.
        pub["pay_options"] = [
            {"method": "card", "label": "Card",
             "charge_cents": bal + IP.compute_surcharge_cents(cfg, bal, "card"),
             "platform_fee_cents": IP.compute_fee_cents(cfg, bal, "card"),
             "processing_cents": IP.card_processing_estimate_cents(bal)},
            {"method": "ach", "label": "Bank (ACH)",
             "charge_cents": bal + IP.compute_surcharge_cents(cfg, bal, "ach"),
             "platform_fee_cents": IP.compute_fee_cents(cfg, bal, "ach"),
             "processing_cents": 0},
        ]
        open_invs.append(pub)
    history = [_public_invoice(i) for i in invs
               if i["total_cents"] - i.get("paid_cents", 0) <= 0]
    autopay = await Scoped(db.autopay_enrollments, link["tenant_id"]).find_one(
        {"customer_id": link.get("customer_id") or "", "active": True})
    _br = (tenant or {}).get("branding") or {}
    return {"business": (tenant or {}).get("name", ""),
            # Invoice Design Shop: the business's face on the client page.
            "branding": {
                "firm_name": _br.get("firm_name", ""),
                "logo_url": _br.get("logo_url", ""),
                "accent_color": _br.get("accent_color", ""),
                "tagline": _br.get("tagline", ""),
                "layout": _br.get("layout") or "classic",
                "banner_url": _br.get("banner_url", ""),
                "default_message": _br.get("default_message", ""),
                "footer_note": _br.get("footer_note", ""),
            },
            "client_name": link.get("client_name"),
            "open": open_invs, "history": history,
            "total_open_cents": sum(i["balance_cents"] for i in open_invs),
            # FINAL fee-note wording, owner-assembled word by word 2026-11-14.
            "fee_note": ("We're always trying to keep fees low for our "
                         "customers. Please let us know if you have any "
                         "ideas of how we can."),
            "help": {"label": "Payment problem or question?",
                     "desk_configured": bool(os.environ.get("SUPPORT_DESK_URL"))},
            "autopay": {"enrolled": bool(autopay),
                        "brand": (autopay or {}).get("pm_brand"),
                        "last4": (autopay or {}).get("pm_last4")}}


@router.get("/c/{token}/view/{invoice_id}")
async def client_view_invoice(token: str, invoice_id: str):
    """Public: one invoice's detail; records the VIEW (open tracking)."""
    link = await IP.resolve_client_token(token)
    if not link:
        raise HTTPException(404, "Link not found")
    inv = await _inv_coll(link["tenant_id"]).find_one({"id": invoice_id})
    if not inv or (link.get("customer_id") and inv.get("customer_id") != link["customer_id"]):
        raise HTTPException(404, "Invoice not found")
    if not inv.get("first_viewed_at"):
        await _inv_coll(link["tenant_id"]).update_one(
            {"id": invoice_id}, {"$set": {"first_viewed_at": now_iso()}})
    await IP.add_timeline(link["tenant_id"], invoice_id, "viewed",
                          {"via": "client_page",
                           "after_channel": IP.last_send_channel(inv)})
    return {"invoice": _public_invoice(inv)}


_PIXEL = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000"
    "010001000002024401003b")


@router.get("/c/{token}/open.gif")
async def client_open_pixel(token: str):
    """Email open-tracking pixel: opening the invoice email marks the
    client's most recent open invoice as viewed."""
    link = await IP.resolve_client_token(token)
    if link:
        invs = await _client_invoices(link)
        open_inv = next((i for i in invs
                         if i["total_cents"] - i.get("paid_cents", 0) > 0), None)
        if open_inv:
            if not open_inv.get("first_viewed_at"):
                await _inv_coll(link["tenant_id"]).update_one(
                    {"id": open_inv["id"]}, {"$set": {"first_viewed_at": now_iso()}})
            await IP.add_timeline(link["tenant_id"], open_inv["id"], "viewed",
                                  {"via": "email_pixel",
                                   "after_channel": IP.last_send_channel(open_inv)})
    return Response(content=_PIXEL, media_type="image/gif",
                    headers={"Cache-Control": "no-store"})


class ClientPayBody(BaseModel):
    method: str = Field(default="card", pattern=r"^(card|ach)$")
    origin_url: str = ""
    amount_cents: Optional[int] = None


@router.post("/c/{token}/pay/{invoice_id}")
async def client_pay(token: str, invoice_id: str, body: ClientPayBody):
    link = await IP.resolve_client_token(token)
    if not link:
        raise HTTPException(404, "Link not found")
    inv = await _inv_coll(link["tenant_id"]).find_one({"id": invoice_id})
    if not inv or (link.get("customer_id") and inv.get("customer_id") != link["customer_id"]):
        raise HTTPException(404, "Invoice not found")
    # Contract+sign gate: an unsigned agreement blocks payment (the
    # deposit step cannot complete before signing — owner flow rule).
    if IP.signature_required(inv):
        raise HTTPException(
            409, "Please review and sign the agreement before paying.")
    return await create_checkout_for_invoice(link["tenant_id"], inv, body.method,
                                             body.origin_url, token, body.amount_cents)


@router.post("/c/{token}/revoke")
async def revoke_client_token(token: str, user: dict = Depends(require("ops:manage"))):
    r = await Scoped(db.client_page_tokens, user["tenant_id"]).update_one(
        {"token_hash": IP._digest(token)}, {"$set": {"revoked": True}})
    if not r.matched_count:
        raise HTTPException(404, "Token not found")
    return {"ok": True}


# ---------------- DEPOSITS / SPLIT BILLING ----------------
class SplitPart(BaseModel):
    label: str
    amount: float                      # dollars
    due_date: Optional[str] = None


class SplitBody(BaseModel):
    parts: list[SplitPart]


@router.post("/invoices/{invoice_id}/split")
async def split_invoice(invoice_id: str, body: SplitBody,
                        user: dict = Depends(require("ops:manage"))):
    """Split an issued invoice into independently sendable/payable parts
    (deposit + balance, or milestones). Each part is a real invoice issued
    through the existing issue path (balanced ledger entries); the parent
    is marked split and excluded from direct payment."""
    from routers.invoices import issue_invoice, InvoiceBody, InvLine
    parent = await _get_invoice(user["tenant_id"], invoice_id)
    if parent.get("split_children"):
        raise HTTPException(409, "Invoice is already split")
    if parent.get("paid_cents", 0) > 0:
        raise HTTPException(409, "Cannot split an invoice with payments")
    if len(body.parts) < 2:
        raise HTTPException(422, "A split needs at least two parts")
    total = sum(to_cents(p.amount) for p in body.parts)
    if abs(total - parent["total_cents"]) > 1:
        raise HTTPException(422, "Split parts must add up to the invoice total")
    children = []
    for idx, p in enumerate(body.parts):
        child = await issue_invoice(InvoiceBody(
            client_name=parent["client_name"], customer_id=parent.get("customer_id"),
            lines=[InvLine(kind="service",
                           name=f"{p.label} — invoice {parent.get('issue_entry') or parent['id'][:8]}",
                           amount=p.amount)],
            due_date=p.due_date, terms=parent.get("terms")), user)
        child_set = {"split_parent_id": invoice_id, "split_label": p.label}
        # Contract+sign: the agreement rides on the FIRST part only (the
        # deposit) — the client signs once, before paying the deposit.
        if idx == 0 and (parent.get("agreement_text") or "").strip():
            child_set["agreement_text"] = parent["agreement_text"]
        await _inv_coll(user["tenant_id"]).update_one(
            {"id": child["id"]},
            {"$set": child_set})
        children.append({"invoice_id": child["id"], "label": p.label,
                         "total_cents": child["total_cents"]})
    await _inv_coll(user["tenant_id"]).update_one(
        {"id": invoice_id},
        {"$set": {"split_children": [c["invoice_id"] for c in children],
                  "status": "split"}})
    await IP.add_timeline(user["tenant_id"], invoice_id, "split",
                          {"parts": len(children)})
    await audit(user, "invoice.split", "invoice", invoice_id, {"parts": children})
    return {"parent_invoice_id": invoice_id, "parts": children}


# ---------------- AUTOPAY ----------------
class AutopayEnrollBody(BaseModel):
    customer_id: str
    payment_method_id: str             # from a completed Stripe SetupIntent
    pm_brand: str = ""
    pm_last4: str = ""


@router.post("/autopay/enroll")
async def autopay_enroll(body: AutopayEnrollBody,
                         user: dict = Depends(require("ops:manage"))):
    """Record a saved payment method for a customer (the SetupIntent itself
    is completed on Stripe's hosted page; see /autopay/setup-link)."""
    cust = await Scoped(db.customers, user["tenant_id"]).find_one({"id": body.customer_id})
    if not cust:
        raise HTTPException(404, "Customer not found")
    doc = {"id": new_id(), "customer_id": body.customer_id,
           "client_name": cust.get("name"), "payment_method_id": body.payment_method_id,
           "pm_brand": body.pm_brand, "pm_last4": body.pm_last4,
           "active": True, "created_at": now_iso()}
    await Scoped(db.autopay_enrollments, user["tenant_id"]).insert_one(doc)
    await audit(user, "invoice.autopay_enroll", "customer", body.customer_id,
                {"last4": body.pm_last4})
    doc.pop("_id", None)
    return doc


class AutopaySetupBody(BaseModel):
    customer_id: str
    origin_url: str = ""


@router.post("/autopay/setup-link")
async def autopay_setup_link(body: AutopaySetupBody,
                             user: dict = Depends(require("ops:manage"))):
    """Stripe Checkout in setup mode: client saves a card/ACH for autopay.
    The webhook (mode=setup) completes enrollment."""
    stripe = _stripe()
    cust = await Scoped(db.customers, user["tenant_id"]).find_one({"id": body.customer_id})
    if not cust:
        raise HTTPException(404, "Customer not found")
    origin = (body.origin_url or IP.site_base()).rstrip("/")
    if not origin.startswith("http"):
        raise HTTPException(422, "origin_url is required")
    try:
        session = stripe.checkout.Session.create(
            mode="setup", payment_method_types=["card", "us_bank_account"],
            success_url=f"{origin}/invoices?autopay=done",
            cancel_url=f"{origin}/invoices",
            metadata={"kind": "autopay_setup", "tenant_id": user["tenant_id"],
                      "customer_id": body.customer_id})
    except Exception as e:
        raise HTTPException(502, f"Autopay setup unavailable: {e}")
    return {"checkout_url": session.url, "session_id": session.id}


async def _fulfill_autopay_setup(obj: dict):
    meta = obj.get("metadata") or {}
    tid, cid = meta.get("tenant_id"), meta.get("customer_id")
    if not tid or not cid:
        return
    pm_id, brand, last4 = "", "", ""
    try:
        import stripe
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY") or ""
        si = stripe.SetupIntent.retrieve(obj.get("setup_intent") or "")
        pm_id = si.get("payment_method") or ""
        if pm_id:
            pm = stripe.PaymentMethod.retrieve(pm_id)
            card = pm.get("card") or {}
            brand, last4 = card.get("brand", ""), card.get("last4", "")
    except Exception:
        pass
    await Scoped(db.autopay_enrollments, tid).insert_one({
        "id": new_id(), "customer_id": cid, "payment_method_id": pm_id,
        "pm_brand": brand, "pm_last4": last4, "active": True,
        "created_at": now_iso()})


@router.post("/invoices/{invoice_id}/autopay/charge")
async def autopay_charge(invoice_id: str, user: dict = Depends(require("ops:manage"))):
    """Charge the client's saved payment method off-session for the invoice
    balance. Success records the payment; failure lands in collections."""
    stripe = _stripe()
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    if balance <= 0:
        raise HTTPException(409, "Invoice already fully paid")
    enr = await Scoped(db.autopay_enrollments, user["tenant_id"]).find_one(
        {"customer_id": inv.get("customer_id") or "", "active": True})
    if not enr:
        raise HTTPException(409, "Client has not enrolled in autopay")
    cfg = await IP.get_pay_config(user["tenant_id"])
    method = "ach" if (enr.get("pm_brand") or "").lower() in ("us_bank_account", "ach") else "card"
    fee = IP.compute_fee_cents(cfg, balance, method)
    try:
        pi = stripe.PaymentIntent.create(
            amount=balance, currency="usd", confirm=True, off_session=True,
            payment_method=enr["payment_method_id"],
            metadata={"kind": "autopay", "tenant_id": user["tenant_id"],
                      "invoice_id": invoice_id})
    except Exception as e:
        await _autopay_failure(user["tenant_id"], invoice_id, str(e)[:200])
        raise HTTPException(402, f"Autopay charge failed: {e}")
    status = pi.get("status") if isinstance(pi, dict) else getattr(pi, "status", "")
    if status != "succeeded":
        await _autopay_failure(user["tenant_id"], invoice_id, f"PaymentIntent status {status}")
        raise HTTPException(402, f"Autopay charge did not succeed ({status})")
    out = await record_online_payment(user["tenant_id"], invoice_id, balance,
                                      method, pi.get("id", ""), "stripe_autopay",
                                      fee_cents=fee)
    await _inv_coll(user["tenant_id"]).update_one(
        {"id": invoice_id}, {"$set": {"autopay_last_error": None}})
    await audit(user, "invoice.autopay_charge", "invoice", invoice_id,
                {"amount_cents": balance, "fee_cents": fee})
    return out


@router.post("/autopay/{enrollment_id}/disable")
async def autopay_disable(enrollment_id: str, user: dict = Depends(require("ops:manage"))):
    r = await Scoped(db.autopay_enrollments, user["tenant_id"]).update_one(
        {"id": enrollment_id}, {"$set": {"active": False}})
    if not r.matched_count:
        raise HTTPException(404, "Enrollment not found")
    return {"ok": True}


# ---------------- DISPUTE EVIDENCE PACKET (research addendum #11) ----------------
def _pdf_bytes(title: str, lines: list[str]) -> bytes:
    """Minimal dependency-free PDF (one text stream, multi-page safe)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")[:110]
    pages, chunk = [], []
    for ln in lines:
        chunk.append(ln)
        if len(chunk) >= 46:
            pages.append(chunk); chunk = []
    if chunk or not pages:
        pages.append(chunk)
    content_ids = []
    for pg in pages:
        text = "BT /F1 10 Tf 50 770 Td 14 TL " + " ".join(
            f"({esc(ln)}) Tj T*" for ln in pg) + " ET"
        content_ids.append(text)
    n = 3 + len(pages) * 2
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    def w(data: bytes):
        offsets.append(buf.tell()); buf.write(data)
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(pages)))
    w(b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n")
    w(f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {len(pages)}>>endobj\n".encode())
    w(b"3 0 obj<</Type/Font/Subtype/Type1/BaseFont/Courier>>endobj\n")
    for i, text in enumerate(content_ids):
        w(f"{4 + i * 2} 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
          f"/Contents {5 + i * 2} 0 R/Resources<</Font<</F1 3 0 R>>>>>>endobj\n".encode())
        stream = text.encode("latin-1", "replace")
        w(f"{5 + i * 2} 0 obj<</Length {len(stream)}>>stream\n".encode() + stream + b"\nendstream endobj\n")
    xref = buf.tell()
    buf.write(f"xref\n0 {n + 1}\n0000000000 65535 f \n".encode())
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer<</Size {n + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF".encode())
    return buf.getvalue()


@router.get("/invoices/{invoice_id}/dispute-packet")
async def dispute_packet(invoice_id: str, user: dict = Depends(require("ledger:read"))):
    """One-click export for a payment dispute: invoice + timeline
    (sent -> viewed -> paid) + send/nudge log + payment records, as a zip
    holding a human-readable PDF and a machine-readable JSON."""
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0, "name": 1})
    sends = await Scoped(db.invoice_sends, user["tenant_id"]).find_list(
        {"invoice_id": invoice_id}, sort=[("at", 1)], limit=500)
    ref = inv.get("issue_entry") or invoice_id[:8]
    lines = [f"DISPUTE EVIDENCE PACKET — Invoice {ref}",
             f"Business: {(tenant or {}).get('name', '')}",
             f"Client: {inv.get('client_name', '')}",
             f"Issued: {inv.get('created_at', '')}  Due: {inv.get('due_date', '')}",
             f"Total: ${cents_to_dollars(inv['total_cents']):,.2f}  "
             f"Paid: ${cents_to_dollars(inv.get('paid_cents', 0)):,.2f}  "
             f"Status: {inv.get('status', '')}", "", "LINE ITEMS:"]
    for ln in inv.get("lines", []) or []:
        lines.append(f"  {ln.get('name', '')} — ${float(ln.get('amount', 0)):,.2f}")
    lines += ["", "TIMELINE (sent -> viewed -> paid):"]
    lines.append(f"  issued — {inv.get('created_at', '')}")
    for ev in inv.get("timeline", []):
        lines.append(f"  {ev.get('event')} — {ev.get('at', '')} {json.dumps(ev.get('detail', {}))[:60]}")
    lines += ["", "SEND / NUDGE LOG:"]
    for s in sends:
        lines.append(f"  {s.get('at', '')} {s.get('kind')} via {s.get('channel')} "
                     f"to {s.get('to', '')} — {s.get('status')}")
    lines += ["", "PAYMENTS:"]
    for p in inv.get("payments", []):
        lines.append(f"  {p.get('date', '')} ${cents_to_dollars(p.get('amount_cents', 0)):,.2f} "
                     f"via {p.get('method', '')} (entry {p.get('entry', '')}, ref {p.get('reference', '')})")
    packet = {"invoice": {k: v for k, v in inv.items() if k != "_id"},
              "sends": sends, "business": (tenant or {}).get("name", "")}
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"dispute-packet-{ref}.pdf", _pdf_bytes("Dispute packet", lines))
        z.writestr(f"dispute-packet-{ref}.json", json.dumps(packet, indent=2, default=str))
    await audit(user, "invoice.dispute_packet", "invoice", invoice_id, {})
    return Response(content=zbuf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="dispute-packet-{ref}.zip"'})


# ---------------- FEE SUGGESTIONS (owner addition 2026-11-14) ----------------
class FeeSuggestionBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/c/{token}/fee-suggestion")
async def fee_suggestion(token: str, body: FeeSuggestionBody):
    """Public: the pay page's suggestion box — a client's idea for cutting
    fees, stored where the business (and the platform) can read it."""
    link = await IP.resolve_client_token(token)
    if not link:
        raise HTTPException(404, "Link not found")
    await Scoped(db.fee_suggestions, link["tenant_id"]).insert_one({
        "id": new_id(), "customer_id": link.get("customer_id") or "",
        "client_name": link.get("client_name") or "",
        "text": body.text.strip(), "created_at": now_iso()})
    return {"ok": True}


@router.get("/invoice-payments/fee-suggestions")
async def list_fee_suggestions(user: dict = Depends(require("ledger:read"))):
    return await Scoped(db.fee_suggestions, user["tenant_id"]).find_list(
        {}, sort=[("created_at", -1)], limit=200)


# ---------------- CHANNEL STUDY (owner addition 2026-11-14) ----------------
@router.get("/invoice-payments/channel-study")
async def channel_study(user: dict = Depends(require("ledger:read"))):
    """Which channel does each client actually respond to? A view or a
    payment is attributed to the channel of the most recent send before it
    (recorded as after_channel / via_channel on the timeline + payments).
    Returns per-channel aggregates and a per-client best channel."""
    tid = user["tenant_id"]
    sends = await Scoped(db.invoice_sends, tid).find_list({}, limit=5000)
    invs = await _inv_coll(tid).find_list({}, limit=2000)
    channels = ["email", "sms", "whatsapp"]

    def blank():
        return {c: {"sends": 0, "views": 0, "payments": 0} for c in channels}

    agg = blank()
    for s in sends:
        if s.get("channel") in agg and s.get("status") in ("sent", "skipped"):
            agg[s["channel"]]["sends"] += 1
    per_client: dict[str, dict] = {}
    for inv in invs:
        name = inv.get("client_name") or ""
        pc = per_client.setdefault(name, blank())
        for s in inv.get("sends", []):
            if s.get("channel") in pc:
                pc[s["channel"]]["sends"] += 1
        viewed_channels = set()
        for ev in inv.get("timeline", []):
            if ev.get("event") == "viewed":
                ch = (ev.get("detail") or {}).get("after_channel") or ""
                if ch in agg and ch not in viewed_channels:
                    agg[ch]["views"] += 1
                    if ch in pc:
                        pc[ch]["views"] += 1
                    viewed_channels.add(ch)
        for p in inv.get("payments", []):
            ch = p.get("via_channel") or ""
            if ch in agg:
                agg[ch]["payments"] += 1
                if ch in pc:
                    pc[ch]["payments"] += 1

    def rate(d):
        return round(d["payments"] / d["sends"], 3) if d["sends"] else 0.0

    aggregate = [{"channel": c, **agg[c], "response_rate": rate(agg[c])}
                 for c in channels]
    clients = []
    for name, pc in sorted(per_client.items()):
        best, best_score = "", -1.0
        for c in channels:
            score = rate(pc[c]) if pc[c]["sends"] else -1.0
            if score > best_score:
                best, best_score = c, score
        clients.append({"client_name": name,
                        "best_channel": best if best_score >= 0 else "",
                        "channels": {c: {**pc[c], "response_rate": rate(pc[c])}
                                     for c in channels}})
    return {"aggregate": aggregate, "clients": clients}


# ---------------- PAYMENT HELP / SUPPORT DESK SEAM (owner addendum 2026-11-14) --
class PaymentHelpBody(BaseModel):
    invoice_id: Optional[str] = None
    message: str = Field(default="", max_length=2000)


@router.post("/c/{token}/payment-help")
async def payment_help(token: str, body: PaymentHelpBody):
    """'Payment problem or question?' — opens help with the invoice context
    attached so the customer never re-explains. Wiring: a support ticket is
    ALWAYS recorded in Books (tenant-visible); when SUPPORT_DESK_URL is
    configured the response also carries the desk deep link with context.
    Honest posture (owner-approved): we do NOT promise no holds — processors
    hold funds for their own risk checks; we provide visibility (hold /
    payout / dispute state lands in the invoice timeline from Stripe
    events) and this fast help door. Money topics follow the standing
    support triage: AI-first, money disputes escalate to the human queue.
    DEPLOY GATE: the desk's public route was dead at the 2026-11-14 audit —
    set SUPPORT_DESK_URL when it is live; until then the ticket record +
    tenant contact is the working path."""
    link = await IP.resolve_client_token(token)
    if not link:
        raise HTTPException(404, "Link not found")
    tid = link["tenant_id"]
    inv = None
    if body.invoice_id:
        cand = await _inv_coll(tid).find_one({"id": body.invoice_id})
        if cand:
            owns = (cand.get("customer_id") == link.get("customer_id")
                    if link.get("customer_id")
                    else cand.get("client_name") == link.get("client_name"))
            inv = cand if owns else None
    state = None
    if inv:
        state = {"invoice_id": inv["id"], "client_name": inv.get("client_name"),
                 "total_cents": inv.get("total_cents"),
                 "paid_cents": inv.get("paid_cents", 0),
                 "status": inv.get("status"),
                 "last_events": [e.get("event") for e in inv.get("timeline", [])][-5:]}
    ticket = {"id": new_id(), "customer_id": link.get("customer_id") or "",
              "client_name": link.get("client_name") or "",
              "invoice_context": state, "message": body.message.strip(),
              "status": "open", "queue": "ai_first_support",
              "escalation": "money_dispute_to_human",
              "created_at": now_iso()}
    await Scoped(db.support_tickets, tid).insert_one(ticket)
    if inv:
        await IP.add_timeline(tid, inv["id"], "help_requested",
                              {"ticket_id": ticket["id"]})
    desk_url = os.environ.get("SUPPORT_DESK_URL") or ""
    return {"ok": True, "ticket_id": ticket["id"],
            "desk_configured": bool(desk_url),
            "desk_url": (f"{desk_url}?ticket={ticket['id']}" if desk_url else None),
            "note": ("Support request recorded — the business can see it now."
                     if not desk_url else
                     "Opening support with your invoice details attached.")}


@router.get("/invoice-payments/support-tickets")
async def list_support_tickets(user: dict = Depends(require("ledger:read"))):
    return await Scoped(db.support_tickets, user["tenant_id"]).find_list(
        {}, sort=[("created_at", -1)], limit=200)
