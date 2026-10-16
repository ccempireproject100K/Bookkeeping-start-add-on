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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional
import os

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require
from lib import ledger as L
from lib import posting as P
from lib import fx_posting as FXP
from lib.currencies import normalize as normalize_currency
from lib.currencies import get_base_currency, to_minor_units
from lib.mailer import send_email

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
    send_email: bool = False
    origin_url: str = ""  # e.g. https://books.example.com — used to build the link in the email


class LinkSendIn(BaseModel):
    origin_url: str = ""
    to_email: str = ""  # optional override; defaults to the customer's email on file


async def _shop_name_async(user) -> str:
    try:
        tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0, "name": 1})
    except Exception:
        tenant = None
    return ((tenant or {}).get("name") or "").strip() or "Mind at Ease Solutions"


async def _portal_recipient(user, inv: dict) -> Optional[str]:
    """Customer email for an invoice, or None when there is none on file.
    Same approach as collections._reminder_recipient."""
    cid = inv.get("customer_id")
    if cid:
        try:
            c = await Scoped(db.customers, user["tenant_id"]).find_one({"id": cid})
        except Exception:
            c = None
        if c and (c.get("email") or "").strip():
            return c["email"].strip()
    return None


def _portal_origin(body_origin: str, request: Request) -> str:
    origin = (body_origin or request.headers.get("origin")
              or os.environ.get("APP_URL", "") or "").rstrip("/")
    return origin


def _build_portal_email(shop_name: str, portal_url: str, invoice_ref: str,
                        client_name: str, balance_cents: int,
                        expires_at: str) -> tuple[str, str, str]:
    """(subject, text_body, html_body) for a portal-link invitation email.
    Branded with the shop name; no third-party residue."""
    balance = f"${balance_cents / 100:,.2f}"
    subject = f"{shop_name} — view & pay invoice {invoice_ref} online"
    text = (
        f"Hi {client_name or 'there'},\n\n"
        f"{shop_name} has shared a secure link where you can view and pay "
        f"invoice {invoice_ref} ({balance} due):\n\n"
        f"{portal_url}\n\n"
        f"This link expires on {expires_at[:10]}. No account or login needed.\n\n"
        f"— {shop_name}\n"
        f"Sent via Mind at Ease Solutions"
    )
    html = (
        f"<div style=\"font-family:sans-serif;max-width:560px;color:#1a1a1a\">"
        f"<h2 style=\"color:#b8860b\">{shop_name}</h2>"
        f"<p>Hi {client_name or 'there'},</p>"
        f"<p>{shop_name} has shared a secure link where you can view and pay "
        f"invoice <strong>{invoice_ref}</strong> (<strong>{balance}</strong> due):</p>"
        f"<p><a href=\"{portal_url}\" "
        f"style=\"display:inline-block;padding:12px 24px;background:#b8860b;color:#fff;"
        f"text-decoration:none;border-radius:6px\">View &amp; pay invoice</a></p>"
        f"<p style=\"color:#666;font-size:13px\">Or open this link:<br/>"
        f"<a href=\"{portal_url}\">{portal_url}</a></p>"
        f"<p style=\"color:#666;font-size:13px\">This link expires on {expires_at[:10]}. "
        f"No account or login needed.</p>"
        f"<hr style=\"border:none;border-top:1px solid #ddd\"/>"
        f"<p style=\"color:#999;font-size:12px\">Sent via Mind at Ease Solutions</p>"
        f"</div>"
    )
    return subject, text, html


async def _deliver_link_email(user, link_doc: dict, inv: dict, token: str,
                              origin_url: str, to_override: str = "") -> dict:
    """Best-effort email delivery for a portal link. Key-gated in lib.mailer:
    without SMTP_HOST this returns ("skipped", ...) and the link is marked
    'queued' so the owner can retry from the UI once SMTP is configured.
    Returns the email fields to persist on the link doc."""
    shop = await _shop_name_async(user)
    origin = (origin_url or os.environ.get("APP_URL", "") or "").rstrip("/")
    to = (to_override or "").strip() or await _portal_recipient(user, inv)
    if not to:
        return {"email_to": None, "email_status": "no_recipient",
                "email_error": "Customer has no email on file", "email_sent_at": None}
    if not origin:
        return {"email_to": to, "email_status": "queued",
                "email_error": "Could not determine the site URL — retry with an origin",
                "email_sent_at": None}
    portal_url = f"{origin}/p/{token}"
    ref = inv.get("issue_entry") or inv["id"]
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    subject, text, html = _build_portal_email(
        shop, portal_url, ref, inv.get("client_name", ""), balance,
        link_doc["expires_at"])
    try:
        status, err = await send_email(to, subject, text, html)
    except Exception as exc:  # belt-and-braces; mailer already catches
        status, err = "skipped", str(exc)
    if status == "sent":
        return {"email_to": to, "email_status": "sent", "email_error": None,
                "email_sent_at": now_iso(), "portal_url": portal_url}
    if err == "SMTP not configured":
        return {"email_to": to, "email_status": "queued", "email_error": err,
                "email_sent_at": None, "portal_url": portal_url}
    return {"email_to": to, "email_status": "failed", "email_error": err,
            "email_sent_at": None, "portal_url": portal_url}


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
async def create_link(body: LinkIn, user: dict = Depends(require("ops:manage")),
                      request: Request = None):
    """Generate a magic link. Pass invoice_id, or customer_id to link that
    customer's most recent unpaid invoice.

    With send_email=true the link is also emailed to the customer's address
    on file (key-gated: without SMTP configured it stays queued for a later
    retry via POST /portal/links/{id}/send)."""
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
    link_id = new_id()
    link_doc = {
        "id": link_id, "token_hash": _digest(token), "tenant_id": user["tenant_id"],
        "invoice_id": inv["id"], "customer_id": inv.get("customer_id"),
        "created_by": user.get("name"), "created_at": now_iso(),
        "expires_at": expires_at, "revoked": False, "used": False,
        "email_to": None, "email_status": "not_sent", "email_error": None,
        "email_sent_at": None,
    }
    await db.portal_links.insert_one(link_doc)
    email_fields = {}
    if body.send_email:
        origin = _portal_origin(body.origin_url, request) if request else body.origin_url.rstrip("/")
        email_fields = await _deliver_link_email(user, link_doc, inv, token, origin)
        await Scoped(db.portal_links, user["tenant_id"]).update_one(
            {"id": link_id}, {"$set": email_fields})
    await _audit(user, "portal.link.create", "invoice", inv["id"],
                 {"customer_id": inv.get("customer_id"), "expires_at": expires_at,
                  "email_status": email_fields.get("email_status", "not_sent")})
    out = {"token": token, "expires_at": expires_at, "invoice_id": inv["id"],
           "client_name": inv.get("client_name"), "link_id": link_id,
           "email_status": email_fields.get("email_status", "not_sent")}
    if email_fields.get("portal_url"):
        out["portal_url"] = email_fields["portal_url"]
    return out


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


@router.post("/links/{link_id}/send")
async def send_link(link_id: str, body: LinkSendIn,
                    user: dict = Depends(require("ops:manage")),
                    request: Request = None):
    """(Re)send a portal link by email. Generates a FRESH token (the previous
    URL stops working — only the hash is stored, so resends must rotate),
    keeping the original expiry. Key-gated like collections: without SMTP the
    link is marked 'queued' for a later retry."""
    coll = Scoped(db.portal_links, user["tenant_id"])
    link = await coll.find_one({"id": link_id})
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    if link.get("revoked"):
        raise HTTPException(status_code=409, detail="Link is revoked")
    if link.get("used"):
        raise HTTPException(status_code=409, detail="Link was already used")
    inv = await Scoped(db.invoices, user["tenant_id"]).find_one({"id": link["invoice_id"]})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    token = secrets.token_urlsafe(32)
    link["token_hash"] = _digest(token)
    origin = _portal_origin(body.origin_url, request) if request else body.origin_url.rstrip("/")
    email_fields = await _deliver_link_email(user, link, inv, token, origin,
                                             to_override=body.to_email)
    sent_count = (link.get("email_sent_count") or 0) + (1 if email_fields["email_status"] == "sent" else 0)
    await coll.update_one({"id": link_id}, {"$set": {
        "token_hash": _digest(token),
        **email_fields,
        "email_sent_count": sent_count,
    }})
    await _audit(user, "portal.link.send", "portal_link", link_id,
                 {"email_status": email_fields["email_status"],
                  "email_to": email_fields.get("email_to")})
    out = {"ok": True, "token": token, "email_status": email_fields["email_status"],
           "email_to": email_fields.get("email_to"),
           "email_error": email_fields.get("email_error"),
           "email_sent_count": sent_count}
    if email_fields.get("portal_url"):
        out["portal_url"] = email_fields["portal_url"]
    return out


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
    inv_currency = normalize_currency(inv.get("currency") or "USD")
    entry = await L.post_journal_entry(db, link["tenant_id"], actor, {
        "entry_date": today_str(), "source": "portal_payment", "source_ref": inv["id"],
        "memo": f"Client portal payment — {inv['client_name']}",
        "lines": [P.dr(cash, cents, "Client portal payment"),
                  P.cr("1200", cents, "AR cleared")],
        "currency": inv_currency,
    }, status="posted")
    # Realized FX vs the invoice currency (best-effort; never breaks payment).
    await FXP.post_realized_fx(
        db=db, tenant_id=link["tenant_id"], user=actor, kind="invoice",
        doc=inv, pay_currency=inv_currency, pay_amount_minor=cents,
        applied_doc_minor=cents, pay_date=today_str())

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


# ---------------------------------------------------------------- Stripe card checkout (live payments)


class PortalCheckoutIn(BaseModel):
    origin_url: str = ""  # e.g. https://books.example.com — used for success/cancel URLs


def _stripe_configured() -> bool:
    key = os.environ.get("STRIPE_SECRET_KEY") or ""
    return bool(key) and key != "sk_test_emergent"


@router.post("/checkout/{token}")
async def portal_checkout(token: str, body: PortalCheckoutIn, request: Request):
    """Public: create a Stripe Checkout session for the invoice's balance.

    The customer pays on Stripe; the `checkout.session.completed` webhook
    fulfills it (ledger entry + invoice totals). Idempotent-ish: creating
    another session for the same invoice just makes another link — the
    fulfill step clamps to the remaining balance exactly once per session.
    """
    import stripe
    if not _stripe_configured():
        raise HTTPException(
            status_code=501,
            detail="Card payments aren't connected yet — ask the business for another way to pay.")
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    link = await _lookup_link(token)
    tid = link["tenant_id"]
    inv = await db.invoices.find_one({"tenant_id": tid, "id": link["invoice_id"]})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    if balance <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    origin = (body.origin_url or request.headers.get("origin") or "").rstrip("/")
    if not origin or not origin.startswith("http"):
        raise HTTPException(status_code=422, detail="origin_url is required")
    tenant = await db.tenants.find_one({"id": tid}, {"_id": 0, "name": 1})
    # Multi-currency Phase 4: charge in the INVOICE's currency, not the
    # workspace base currency. Invoice amounts are stored x100; convert to
    # the currency's proper minor units (JPY has no cents).
    inv_currency = normalize_currency(inv.get("currency") or "USD")
    stripe_currency = inv_currency.lower()
    stripe_amount = to_minor_units(balance / 100.0, inv_currency)
    if stripe_amount <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    business = (tenant or {}).get("name", "")
    label = f"Invoice {inv.get('issue_entry') or inv['id']}"
    if business:
        label = f"{business} — {label}"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": stripe_currency,
                    "unit_amount": stripe_amount,
                    "product_data": {"name": label,
                                     "description": f"Balance due: {inv.get('client_name', '')}"},
                },
                "quantity": 1,
            }],
            success_url=f"{origin}/p/{token}?paid=1&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/p/{token}",
            metadata={"kind": "portal_invoice", "tenant_id": tid,
                      "invoice_id": inv["id"], "token_hash": _digest(token)},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Card payment unavailable: {e}")
    await db.payment_transactions.insert_one({
        "session_id": session.id, "tenant_id": tid, "kind": "portal_invoice",
        "invoice_id": inv["id"], "token_hash": _digest(token),
        "amount_cents": balance, "currency": stripe_currency,
        "status": "initiated", "payment_status": "pending", "fulfilled": False,
        "created_at": now_iso(), "updated_at": now_iso(),
    })
    return {"checkout_url": session.url, "session_id": session.id}


async def fulfill_portal_invoice(session_id: str) -> None:
    """Exactly-once: claim the paid session, post Dr 1050 / Cr 1200 AR,
    update the invoice totals, and mark the portal link used when paid."""
    rec = await db.payment_transactions.find_one_and_update(
        {"session_id": session_id, "kind": "portal_invoice",
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
            raise ValueError("Invoice was already paid in full — refund the card payment manually in Stripe")
        cents = min(rec["amount_cents"], balance)
        actor = {"id": "portal", "email": "portal@client", "name": "Client portal",
                 "role": "owner", "tenant_id": tid}
        inv_currency = normalize_currency(inv.get("currency") or "USD")
        pay_currency = normalize_currency(rec.get("currency") or inv_currency)
        entry = await L.post_journal_entry(db, tid, actor, {
            "entry_date": today_str(), "source": "stripe_portal", "source_ref": inv_id,
            "memo": f"Stripe card payment — {inv.get('client_name', '')}",
            "lines": [P.dr("1050", cents, "Stripe card payment"),
                      P.cr("1200", cents, "AR cleared")],
            "currency": inv_currency,
        }, status="posted")
        # Realized FX: Stripe settled in pay_currency vs the invoice currency.
        await FXP.post_realized_fx(
            db=db, tenant_id=tid, user=actor, kind="invoice", doc=inv,
            pay_currency=pay_currency, pay_amount_minor=cents,
            applied_doc_minor=cents, pay_date=today_str())
        new_paid = inv.get("paid_cents", 0) + cents
        status = "paid" if new_paid >= inv["total_cents"] else "partial"
        pmt = {"date": today_str(), "amount_cents": cents, "cash_cents": cents,
               "credit_cents": 0, "method": "card", "processor": "stripe",
               "reference": rec["session_id"], "entry": entry["number"],
               "source": "stripe_portal"}
        await db.invoices.update_one(
            {"tenant_id": tid, "id": inv_id},
            {"$set": {"status": status, "paid_cents": new_paid},
             "$push": {"payments": pmt}})
        if status == "paid":
            await db.portal_links.update_one(
                {"token_hash": rec.get("token_hash")}, {"$set": {"used": True}})
    except Exception as e:
        err = str(e)
    await db.payment_transactions.update_one(
        {"session_id": session_id},
        {"$set": {"fulfilled": not err, "fulfilling": False, "error": err,
                  "updated_at": now_iso()}})
