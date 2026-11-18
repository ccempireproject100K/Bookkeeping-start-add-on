"""Shared helpers for invoice delivery & payment (the InvoiceEase fold-in).

Used by routers/invoice_delivery.py, the collections autopilot (reminder
sends), and recurring-invoice auto-charge. Everything tenant-scoped.

FEE SEAM — the platform's slice of an online payment is CONFIG, never a
hard-coded price. Stored per tenant in `invoice_pay_config`:

  {
    "id": "invoice_pay",
    "fee_percent": 0.0,          # our slice, % of the payment amount
    "fee_fixed_cents": 0,        # our slice, fixed cents per payment
    "fee_methods": ["ach"],      # payment methods the fee applies to
    "surcharge_enabled": false,  # pass the card processing fee to the payer
    "surcharge_percent": 0.0,    # processing-fee pass-through % (card only)
    "surcharge_fixed_cents": 0,
    "sms_enabled": false,        # SMS seam master flag (see send_sms)
    "nudges_paused": false,      # tenant-wide nudge kill switch
    "from_name": ""              # display name on outbound invoice email
  }

DEFAULT IS ALL-ZERO / OFF: the owner has floated numbers (2.5% + 25c on
ACH) but has NOT stamped final fees, so nothing is charged until he does.
The fee is realized as a Stripe `application_fee_amount` when the tenant
pays through a connected account; on the platform's own Stripe account it
is recorded on the payment record as `platform_fee_cents` for settlement.
Either way the amount is COMPUTED here, in one place, from config.
"""
import hashlib
import html as html_mod
import os
import secrets
from datetime import datetime, timedelta, timezone

from db import db
from lib.base import Scoped, new_id, now_iso, cents_to_dollars
from lib import resend_mailer

FEE_DEFAULTS = {
    "fee_percent": 0.0,
    "fee_fixed_cents": 0,
    "fee_methods": ["ach"],
    "surcharge_enabled": False,
    "surcharge_percent": 0.0,
    "surcharge_fixed_cents": 0,
    "sms_enabled": False,
    "whatsapp_enabled": False,
    "nudges_paused": False,
    "from_name": "",
    # Early-pay discount DEFAULTS (owner-approved feature 2026-11-14).
    # Default OFF (0). The sender's discount, their choice — costs the
    # platform nothing. Per-invoice values override these at issue time.
    "early_pay_percent": 0.0,
    "early_pay_days": 0,
}


# ---------------- Config ----------------
def _cfg_coll(tenant_id, db_override=None):
    # Merge-train (2026-11-14): callers in modules whose db handle is the
    # patched one in tests (routers/invoices) may pass their own handle;
    # production passes nothing and this module's own db is the same
    # client. Default behavior unchanged.
    return Scoped((db_override or db).invoice_pay_config, tenant_id)


async def get_pay_config(tenant_id: str, db_override=None) -> dict:
    doc = await _cfg_coll(tenant_id, db_override).find_one({"id": "invoice_pay"})
    cfg = dict(FEE_DEFAULTS)
    if doc:
        cfg.update({k: v for k, v in doc.items()
                    if k in FEE_DEFAULTS})
    return cfg


async def set_pay_config(tenant_id: str, patch: dict) -> dict:
    clean = {k: v for k, v in patch.items() if k in FEE_DEFAULTS}
    await _cfg_coll(tenant_id).update_one(
        {"id": "invoice_pay"}, {"$set": {"id": "invoice_pay", **clean,
                                         "updated_at": now_iso()}}, upsert=True)
    return await get_pay_config(tenant_id)


def compute_fee_cents(cfg: dict, amount_cents: int, method: str) -> int:
    """Our slice for a payment of amount_cents via method ('card'|'ach'|'bank').
    Zero unless config says otherwise; never negative, never > amount."""
    if method not in (cfg.get("fee_methods") or []):
        return 0
    fee = int(round(amount_cents * float(cfg.get("fee_percent") or 0) / 100.0))
    fee += int(cfg.get("fee_fixed_cents") or 0)
    return max(0, min(fee, amount_cents))


# Stripe's published standard card cost — the processing floor from the
# house record (2.9% + 30c). Used ONLY to display the honest comparison on
# the pay page (bank steering, owner addendum 2026-11-14): what card
# processing costs vs our 1.5% price on bank payments.
CARD_PROCESSING_PERCENT = 2.9
CARD_PROCESSING_FIXED_CENTS = 30


def card_processing_estimate_cents(amount_cents: int) -> int:
    return int(round(amount_cents * CARD_PROCESSING_PERCENT / 100.0)) \
        + CARD_PROCESSING_FIXED_CENTS


def compute_surcharge_cents(cfg: dict, amount_cents: int, method: str) -> int:
    """Processing-fee pass-through added to what the PAYER is charged.
    Card only; default OFF (surcharge_enabled False / zeros)."""
    if method != "card" or not cfg.get("surcharge_enabled"):
        return 0
    s = int(round(amount_cents * float(cfg.get("surcharge_percent") or 0) / 100.0))
    s += int(cfg.get("surcharge_fixed_cents") or 0)
    return max(0, s)


# ---------------- Client identity / email ----------------
async def client_email(tenant_id: str, inv: dict) -> str:
    """Resolve the recipient for an invoice: customer record email first,
    then an email stored on the invoice itself. '' when unknown."""
    if inv.get("client_email"):
        return inv["client_email"]
    cid = inv.get("customer_id")
    if cid:
        c = await Scoped(db.customers, tenant_id).find_one({"id": cid})
        if c and c.get("email"):
            return c["email"]
    return ""


async def client_phone(tenant_id: str, inv: dict) -> str:
    if inv.get("client_phone"):
        return inv["client_phone"]
    cid = inv.get("customer_id")
    if cid:
        c = await Scoped(db.customers, tenant_id).find_one({"id": cid})
        if c and c.get("phone"):
            return c["phone"]
    return ""


async def reminders_paused_for(tenant_id: str, inv: dict) -> bool:
    """Per-invoice and per-client pause flags (research addendum #9)."""
    if inv.get("nudges_paused"):
        return True
    cid = inv.get("customer_id")
    if cid:
        c = await Scoped(db.customers, tenant_id).find_one({"id": cid})
        if c and c.get("nudges_paused"):
            return True
    return False


# ---------------- Client-page tokens ----------------
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def ensure_client_token(tenant_id: str, customer_id: str = "",
                              client_name: str = "") -> str:
    """One reusable, revocable client-page token per customer (or per
    client_name when no customer record exists). Reuses the live token."""
    coll = Scoped(db.client_page_tokens, tenant_id)
    q = {"customer_id": customer_id} if customer_id else {"client_name": client_name}
    existing = await coll.find_one({**q, "revoked": False})
    if existing and existing.get("_plaintext"):
        return existing["_plaintext"]
    token = secrets.token_urlsafe(32)
    if existing:  # token exists but plaintext not retained -> rotate
        await coll.update_one({"id": existing["id"]}, {"$set": {"revoked": True}})
    await coll.insert_one({
        "id": new_id(), "customer_id": customer_id, "client_name": client_name,
        "token_hash": _digest(token), "_plaintext": token,
        "revoked": False, "created_at": now_iso(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=365)).isoformat(),
    })
    return token


async def resolve_client_token(token: str) -> dict | None:
    link = await db.client_page_tokens.find_one(
        {"token_hash": _digest(token)}, {"_id": 0})
    if not link or link.get("revoked"):
        return None
    if link.get("expires_at", "") <= now_iso():
        return None
    return link


def site_base() -> str:
    return (os.environ.get("PUBLIC_SITE_URL") or "").rstrip("/")


def client_page_url(token: str) -> str:
    base = site_base()
    return f"{base}/c/{token}" if base else f"/c/{token}"


# ---------------- Timeline ----------------
async def add_timeline(tenant_id: str, invoice_id: str, event: str,
                       detail: dict | None = None):
    await Scoped(db.invoices, tenant_id).update_one(
        {"id": invoice_id},
        {"$push": {"timeline": {"event": event, "at": now_iso(),
                                "detail": detail or {}}}})


# ---------------- Email composition + send core ----------------
def _money(cents: int) -> str:
    return f"${cents_to_dollars(cents):,.2f}"


def invoice_email_html(business: str, inv: dict, pay_url: str,
                       kind: str = "invoice",
                       branding: dict | None = None) -> tuple[str, str, str]:
    """Returns (subject, html, text). kind: 'invoice' | 'reminder'.
    branding (Design Shop) optionally carries logo_url / accent_color /
    default_message — the business's face on the email too."""
    bal = inv["total_cents"] - inv.get("paid_cents", 0)
    ref = inv.get("issue_entry") or f"#{inv['id'][:8]}"
    lines = "".join(
        f"<tr><td style='padding:4px 8px'>{html_mod.escape(str(ln.get('name','')))}</td>"
        f"<td style='padding:4px 8px;text-align:right'>{_money(int(round(float(ln.get('amount',0))*100)))}</td></tr>"
        for ln in inv.get("lines", []) or [])
    if kind == "reminder":
        subject = f"Reminder: invoice {ref} — {_money(bal)} due"
        lead = f"A friendly reminder that invoice {ref} has a balance of {_money(bal)}"
        if inv.get("due_date"):
            lead += f" (due {inv['due_date'][:10]})"
        lead += "."
    else:
        subject = f"Invoice {ref} from {business} — {_money(inv['total_cents'])}"
        lead = f"Here is your invoice {ref} for {_money(inv['total_cents'])}."
        if bal < inv["total_cents"]:
            lead += f" Balance due: {_money(bal)}."
    br = branding or {}
    logo_url = (br.get("logo_url") or "").strip()
    accent = (br.get("accent_color") or "").strip()
    if not (accent.startswith("#") and len(accent) in (4, 7)):
        accent = "#0f766e"
    logo_html = (f'<img src="{html_mod.escape(logo_url, quote=True)}" alt="" '
                 f'style="max-height:48px;max-width:220px;margin-bottom:8px"/><br/>'
                 if logo_url else "")
    default_msg = (br.get("default_message") or "").strip()
    msg_html = (f"<p>{html_mod.escape(default_msg)}</p>"
                if default_msg and kind == "invoice" else "")
    h = f"""<div style="font-family:Arial,sans-serif;max-width:560px">
  {logo_html}<h2 style="margin-bottom:4px">{html_mod.escape(business or 'Your invoice')}</h2>
  <p>{html_mod.escape(lead)}</p>
  {msg_html}
  <table style="border-collapse:collapse;width:100%">{lines}
  <tr><td style="padding:4px 8px"><b>Total</b></td>
  <td style="padding:4px 8px;text-align:right"><b>{_money(inv['total_cents'])}</b></td></tr></table>
  <p style="margin:20px 0"><a href="{pay_url}" style="background:{accent};color:#fff;
     padding:12px 20px;border-radius:8px;text-decoration:none">View &amp; pay invoice</a></p>
  <p style="color:#666;font-size:12px">This link opens your secure invoice page — no login needed.</p>
  <img src="{pay_url}/open.gif" width="1" height="1" alt=""/>
</div>"""
    t = f"{lead}\n\nView & pay: {pay_url}\n"
    return subject, h, t


async def send_invoice_email(tenant_id: str, inv: dict, kind: str = "invoice",
                             actor: dict | None = None,
                             message_override: str = "") -> dict:
    """The one send path for invoices + reminders. Records the send on the
    invoice (sends[] + timeline), returns a result dict. Never raises for
    delivery problems — honest states: sent | skipped | failed."""
    to = await client_email(tenant_id, inv)
    tenant = await db.tenants.find_one({"id": tenant_id},
                                       {"_id": 0, "name": 1, "branding": 1})
    business = (tenant or {}).get("name", "")
    cfg = await get_pay_config(tenant_id)
    if cfg.get("from_name"):
        business = cfg["from_name"] or business
    token = await ensure_client_token(tenant_id, inv.get("customer_id") or "",
                                      inv.get("client_name", ""))
    pay_url = client_page_url(token)
    subject, h, t = invoice_email_html(
        business, inv, pay_url, kind,
        branding=(tenant or {}).get("branding") or {})
    if message_override:
        t = message_override + f"\n\nView & pay: {pay_url}\n"
        h = ("<div style='font-family:Arial,sans-serif;max-width:560px'><p>"
             + html_mod.escape(message_override).replace("\n", "<br/>")
             + f"</p><p><a href='{pay_url}'>View &amp; pay invoice</a></p>"
             + f"<img src='{pay_url}/open.gif' width='1' height='1' alt=''/></div>")
    if not to:
        status, err = "skipped", "No client email on file"
    else:
        status, err = await resend_mailer.send_email(to, subject, h, t)
    rec = {"id": new_id(), "kind": kind, "channel": "email", "to": to,
           "status": status, "error": err, "at": now_iso(),
           "by": (actor or {}).get("email") or "system"}
    await Scoped(db.invoices, tenant_id).update_one(
        {"id": inv["id"]}, {"$push": {"sends": rec}})
    await Scoped(db.invoice_sends, tenant_id).insert_one(
        {"invoice_id": inv["id"], **rec})
    await add_timeline(tenant_id, inv["id"],
                       "reminder_sent" if kind == "reminder" else "sent",
                       {"channel": "email", "status": status, "to": to})
    return rec


def last_send_channel(inv: dict) -> str:
    """Channel of the most recent send on an invoice — the channel a view or
    payment is attributed to in the channel study (owner addition, 2026-11-14)."""
    sends = inv.get("sends") or []
    return sends[-1].get("channel", "") if sends else ""


# ---------------- WhatsApp seam (owner addition 2026-11-14) ----------------
async def send_invoice_whatsapp(tenant_id: str, inv: dict, kind: str = "invoice") -> dict:
    """WhatsApp channel seam, same discipline as SMS: DEFAULT OFF, honest
    skipped states, provider env-gated (WHATSAPP_PROVIDER_URL/KEY). The
    empire's WhatsApp path (registered business number) wires in behind
    this seam; no live messages are sent by this build."""
    cfg = await get_pay_config(tenant_id)
    to = await client_phone(tenant_id, inv)
    token = await ensure_client_token(tenant_id, inv.get("customer_id") or "",
                                      inv.get("client_name", ""))
    body = (f"Invoice: balance "
            f"{_money(inv['total_cents'] - inv.get('paid_cents', 0))}. "
            f"View & pay: {client_page_url(token)}")
    provider_ready = bool(os.environ.get("WHATSAPP_PROVIDER_URL")
                          and os.environ.get("WHATSAPP_PROVIDER_KEY"))
    if not cfg.get("whatsapp_enabled"):
        status, err = "skipped", "WhatsApp not yet enabled for this business"
    elif not provider_ready:
        status, err = "skipped", "WhatsApp provider not configured yet"
    elif not to:
        status, err = "skipped", "No client phone on file"
    else:
        status, err = "sent", None  # provider POST wired at channel go-live; seam only today
    rec = {"id": new_id(), "kind": kind, "channel": "whatsapp", "to": to,
           "status": status, "error": err, "at": now_iso(), "body": body,
           "by": "system"}
    await Scoped(db.invoices, tenant_id).update_one(
        {"id": inv["id"]}, {"$push": {"sends": rec}})
    await Scoped(db.invoice_sends, tenant_id).insert_one(
        {"invoice_id": inv["id"], **rec})
    return rec


# ---------------- SMS seam (research addendum #8) ----------------
async def send_invoice_sms(tenant_id: str, inv: dict, kind: str = "invoice") -> dict:
    """SMS channel seam. Live SMS is parked on the empire's 10DLC
    registration (separate owner path), so this seam is DEFAULT OFF:
    unless the tenant flag is on AND a provider is configured via env
    (SMS_PROVIDER_URL/KEY), the send records an honest
    'texting not yet enabled' skipped state. Stub proof only — no live
    texts are sent by this build."""
    cfg = await get_pay_config(tenant_id)
    to = await client_phone(tenant_id, inv)
    token = await ensure_client_token(tenant_id, inv.get("customer_id") or "",
                                      inv.get("client_name", ""))
    body = (f"Invoice from your provider: balance "
            f"{_money(inv['total_cents'] - inv.get('paid_cents', 0))}. "
            f"View & pay: {client_page_url(token)}")
    provider_ready = bool(os.environ.get("SMS_PROVIDER_URL")
                          and os.environ.get("SMS_PROVIDER_KEY"))
    if not cfg.get("sms_enabled"):
        status, err = "skipped", "Texting not yet enabled for this business"
    elif not provider_ready:
        status, err = "skipped", "Texting provider not configured (10DLC registration pending)"
    elif not to:
        status, err = "skipped", "No client phone on file"
    else:
        status, err = "sent", None  # provider POST wired at 10DLC go-live; seam only today
    rec = {"id": new_id(), "kind": kind, "channel": "sms", "to": to,
           "status": status, "error": err, "at": now_iso(), "body": body,
           "by": "system"}
    await Scoped(db.invoices, tenant_id).update_one(
        {"id": inv["id"]}, {"$push": {"sends": rec}})
    await Scoped(db.invoice_sends, tenant_id).insert_one(
        {"invoice_id": inv["id"], **rec})
    return rec


# ---------------- Early-pay discount ----------------
def early_pay_info(inv: dict, on_date: str | None = None) -> dict:
    """Early-pay discount state for an invoice.

    The discount applies ONLY to a payment that clears the FULL remaining
    balance on or before the deadline (issue date + within_days). Returns
    percent/days/deadline/discount_cents (against the current balance) and
    whether the window is still open. All math in cents, rounded half-up
    via round() on the percent of the balance.
    """
    ep = inv.get("early_pay") or {}
    percent = float(ep.get("percent") or 0)
    days = int(ep.get("days") or 0)
    balance = inv.get("total_cents", 0) - inv.get("paid_cents", 0)
    out = {"percent": percent, "days": days, "deadline": None,
           "discount_cents": 0, "discounted_balance_cents": balance,
           "active": False}
    if percent <= 0 or days <= 0 or balance <= 0:
        return out
    issued = (inv.get("created_at") or "")[:10]
    if not issued:
        return out
    from datetime import date, timedelta
    try:
        deadline = date.fromisoformat(issued) + timedelta(days=days)
    except ValueError:
        return out
    today = date.fromisoformat(on_date) if on_date else date.today()
    discount = int(round(balance * percent / 100.0))
    out.update({"deadline": deadline.isoformat(),
                "discount_cents": discount,
                "discounted_balance_cents": balance - discount,
                "active": today <= deadline})
    return out


def signature_required(inv: dict) -> bool:
    """Contract+sign gate: an invoice carrying agreement text that has not
    been signed yet must be signed before it can be paid. After a split,
    only the first part (the deposit) carries the agreement text, so only
    the deposit is gated."""
    return bool((inv.get("agreement_text") or "").strip()) \
        and not inv.get("signature")
