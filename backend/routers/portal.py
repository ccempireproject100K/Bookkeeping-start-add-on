"""Magic-link client portal: passwordless, expiring links for customers.

The owner generates a link per invoice (from the API: POST /portal/links with
invoice_id or customer_id). The customer opens it with NO login and can:
  - view that invoice (details + amounts only),
  - REPORT a payment they made outside the portal (bank/cash/card). A report
    is recorded as PENDING and posts NOTHING: only when the shop confirms it
    (POST /portal/payment-reports/{id}/confirm, ops:manage) does the journal
    entry post — exactly once — and the invoice totals move. The shop can
    also reject a report, which leaves no journal trace. (Security audit
    2026-10-26, N-1; owner ruling: reports say pending until the shop
    confirms.)
  - pay by card via Stripe Checkout (POST /portal/checkout/{token}); the
    signature-verified webhook (POST /portal/stripe/webhook) fulfills the
    payment exactly once. Stripe is KEY-GATED: without STRIPE_SECRET_KEY the
    checkout endpoint returns a graceful 503 (see STRIPE_SETUP.md).

Links are random tokens (secrets.token_urlsafe); only the SHA-256 hash is stored
in the `portal_links` collection along with the expiry. Invalid/expired/revoked
tokens -> 404. The public endpoints are intentionally unauthenticated.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional
import os

from db import db
from lib.audit import append_audit
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require
from lib import ledger as L
from lib import posting as P
from lib import fx_posting as FXP
from lib.currencies import normalize as normalize_currency
from lib.currencies import get_base_currency, to_minor_units
from lib.mailer import send_email

router = APIRouter(prefix="/portal", tags=["portal"])
logger = logging.getLogger(__name__)

DEFAULT_TTL_HOURS = 24 * 7  # 7 days


async def _audit(user, action, entity, entity_id, details=None):
    await append_audit(db, {
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


async def _pending_reports(tenant_id: str, invoice_id: str) -> list:
    """Customer payment reports still waiting for the shop to confirm.
    Degrades to [] when the collection is unavailable (minimal test
    doubles) — a report can never be recorded without it anyway."""
    coll = getattr(db, "portal_payment_reports", None)
    if coll is None:
        return []
    rows = await coll.find(
        {"tenant_id": tenant_id, "invoice_id": invoice_id,
         "status": "pending"},
        {"_id": 0, "amount_cents": 1, "method": 1, "reference": 1,
         "date": 1, "status": 1}).sort("created_at", 1).to_list(50)
    return rows


async def _pending_total_cents(tenant_id: str, invoice_id: str) -> int:
    return sum(r.get("amount_cents", 0)
               for r in await _pending_reports(tenant_id, invoice_id))


@router.get("/view/{token}")
async def portal_view(token: str):
    """Public: view the linked invoice. No login required. Invoice details + amounts only."""
    link = await _lookup_link(token)
    inv = await db.invoices.find_one(
        {"tenant_id": link["tenant_id"], "id": link["invoice_id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    tenant = await db.tenants.find_one({"id": link["tenant_id"]}, {"_id": 0, "name": 1})
    pending = await _pending_reports(link["tenant_id"], inv["id"])
    # Card payments are key-gated (see the Stripe section below). The
    # publishable key is safe to expose — it is public by design.
    return {"business": (tenant or {}).get("name", ""), "invoice": _strip_invoice(inv),
            "card_payments_enabled": _stripe_configured(),
            "stripe_publishable_key": _stripe_cfg()[1] or None,
            "pending_reports": pending,
            "pending_cents": sum(r.get("amount_cents", 0) for r in pending)}


class PortalPayIn(BaseModel):
    amount: float = Field(gt=0)          # dollars
    payment_method: str = Field(default="bank", pattern=r"^(bank|card|cash)$")
    reference: str = Field(default="", max_length=120)


@router.post("/pay/{token}")
async def portal_pay(token: str, body: PortalPayIn):
    """Public: customer REPORTS a payment made outside the portal.

    The report is recorded as pending and posts NOTHING — no journal
    entry, no change to the invoice totals (audit N-1). The shop sees
    it under GET /portal/payment-reports and either confirms it (the
    journal entry posts then, exactly once) or rejects it. Amounts
    already reported-and-pending count against the balance, so the
    same dollars can't be reported twice."""
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
    pending = await _pending_total_cents(link["tenant_id"], inv["id"])
    if cents > balance - pending:
        if pending > 0:
            raise HTTPException(
                status_code=422,
                detail="That amount is already reported and waiting for "
                       "the shop to confirm it")
        raise HTTPException(status_code=422, detail="Amount exceeds the balance due")

    report = {
        "id": new_id(), "tenant_id": link["tenant_id"],
        "invoice_id": inv["id"], "customer_id": inv.get("customer_id"),
        "token_hash": _digest(token),
        "date": today_str(), "amount_cents": cents,
        "method": body.payment_method, "reference": body.reference,
        "status": "pending", "created_at": now_iso(),
        "entry_number": None, "decided_by": None, "decided_at": None,
        "decision_note": "",
    }
    await db.portal_payment_reports.insert_one(report)
    actor = {"id": "portal", "email": "portal@client", "name": "Client portal",
             "role": "owner", "tenant_id": link["tenant_id"]}
    await _audit(actor, "portal.payment.reported", "invoice", inv["id"],
                 {"amount_cents": cents, "method": body.payment_method,
                  "report_id": report["id"],
                  "tenant_id": link["tenant_id"]})
    return {"ok": True, "status": "pending_confirmation",
            "report_id": report["id"], "amount_cents": cents,
            "paid_cents": paid, "balance_cents": balance,
            "pending_cents": pending + cents}


# ------------------------------------- shop side: payment-report decisions
#
# A reported payment becomes real only here. Confirm claims the report
# atomically (pending -> confirmed) BEFORE posting, the same claim
# pattern as the password-reset fix (audit F-1): two confirms racing —
# or a double-click — produce exactly one journal entry; the loser is
# answered from the winner's state. Rejecting a pending report leaves
# no journal trace and frees the reported amount for a fresh report
# or a card checkout.

def _report_view(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ("_id", "token_hash")}


@router.get("/payment-reports")
async def list_payment_reports(status: str = "pending",
                               user: dict = Depends(require("ops:manage"))):
    """Payment reports for this shop (default: still pending), newest
    first, with the invoice ref + client name attached for the UI."""
    q = {} if status == "all" else {"status": status}
    rows = await Scoped(db.portal_payment_reports, user["tenant_id"]).find_list(
        q, sort=[("created_at", -1)], limit=200)
    inv_coll = Scoped(db.invoices, user["tenant_id"])
    for r in rows:
        inv = await inv_coll.find_one({"id": r["invoice_id"]})
        r["invoice_ref"] = (inv or {}).get("issue_entry") or r["invoice_id"]
        r["client_name"] = (inv or {}).get("client_name", "")
    return [_report_view(r) for r in rows]


class ReportDecisionIn(BaseModel):
    note: str = Field(default="", max_length=300)


@router.post("/payment-reports/{report_id}/confirm")
async def confirm_payment_report(report_id: str,
                                 user: dict = Depends(require("ops:manage"))):
    """Confirm a customer-reported payment: post the journal entry
    (Dr cash / Cr AR 1200), move the invoice totals, burn the portal
    link if the invoice is now paid in full. Idempotent — confirming
    an already-confirmed report returns its recorded result and posts
    nothing more."""
    coll = Scoped(db.portal_payment_reports, user["tenant_id"])
    report = await coll.find_one({"id": report_id})
    if not report:
        raise HTTPException(status_code=404, detail="Payment report not found")
    if report["status"] == "confirmed":
        return {"ok": True, "already_confirmed": True,
                "status": "confirmed", "report_id": report_id,
                "entry_number": report.get("entry_number"),
                "invoice_status": report.get("invoice_status")}
    if report["status"] == "rejected":
        raise HTTPException(status_code=409,
                            detail="This report was rejected — ask the "
                                   "customer to report the payment again")
    inv = await Scoped(db.invoices, user["tenant_id"]).find_one(
        {"id": report["invoice_id"]})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    paid = inv.get("paid_cents", 0)
    balance = inv["total_cents"] - paid
    cents = report["amount_cents"]
    if cents > balance:
        raise HTTPException(
            status_code=409,
            detail="The report exceeds the invoice's remaining balance "
                   "— reject it and ask the customer to report again")

    claim = await coll.update_one(
        {"id": report_id, "status": "pending"},
        {"$set": {"status": "confirmed", "decided_by": user.get("name"),
                  "decided_at": now_iso()}})
    if not claim.matched_count:
        # Lost the race — answer from the winner's state, post nothing.
        fresh = await coll.find_one({"id": report_id})
        if fresh and fresh["status"] == "confirmed":
            return {"ok": True, "already_confirmed": True,
                    "status": "confirmed", "report_id": report_id,
                    "entry_number": fresh.get("entry_number"),
                    "invoice_status": fresh.get("invoice_status")}
        raise HTTPException(status_code=409,
                            detail="This report was just decided")

    cash = P._cash_account(report["method"])
    inv_currency = normalize_currency(inv.get("currency") or "USD")
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "portal_payment",
        "source_ref": inv["id"],
        "memo": f"Client portal payment — {inv['client_name']} "
                f"(customer-reported, shop-confirmed)",
        "lines": [P.dr(cash, cents, "Client portal payment"),
                  P.cr("1200", cents, "AR cleared")],
        "currency": inv_currency,
    }, status="posted")
    # Realized FX vs the invoice currency (best-effort; never breaks payment).
    await FXP.post_realized_fx(
        db=db, tenant_id=user["tenant_id"], user=user, kind="invoice",
        doc=inv, pay_currency=inv_currency, pay_amount_minor=cents,
        applied_doc_minor=cents, pay_date=today_str())

    new_paid = paid + cents
    inv_status = "paid" if new_paid >= inv["total_cents"] else "partial"
    pmt = {"date": today_str(), "amount_cents": cents, "cash_cents": cents,
           "credit_cents": 0, "method": report["method"],
           "reference": report.get("reference", ""), "entry": entry["number"],
           "source": "portal", "report_id": report_id,
           "confirmed_by": user.get("name")}
    await db.invoices.update_one(
        {"tenant_id": user["tenant_id"], "id": inv["id"]},
        {"$set": {"status": inv_status, "paid_cents": new_paid},
         "$push": {"payments": pmt}})
    if inv_status == "paid" and report.get("token_hash"):
        await db.portal_links.update_one(
            {"token_hash": report["token_hash"]}, {"$set": {"used": True}})
    await coll.update_one(
        {"id": report_id},
        {"$set": {"entry_number": entry["number"],
                  "invoice_status": inv_status}})
    await _audit(user, "portal.payment.confirmed", "invoice", inv["id"],
                 {"amount_cents": cents, "method": report["method"],
                  "report_id": report_id, "entry_number": entry["number"]})
    return {"ok": True, "already_confirmed": False, "status": "confirmed",
            "report_id": report_id, "entry_number": entry["number"],
            "invoice_status": inv_status, "paid_cents": new_paid,
            "balance_cents": inv["total_cents"] - new_paid}


@router.post("/payment-reports/{report_id}/reject")
async def reject_payment_report(report_id: str,
                                body: Optional[ReportDecisionIn] = None,
                                user: dict = Depends(require("ops:manage"))):
    """Reject a reported payment. Nothing was ever posted for it, so
    rejection leaves no journal trace; the reported amount stops
    counting against the balance immediately."""
    coll = Scoped(db.portal_payment_reports, user["tenant_id"])
    report = await coll.find_one({"id": report_id})
    if not report:
        raise HTTPException(status_code=404, detail="Payment report not found")
    if report["status"] == "rejected":
        return {"ok": True, "already_rejected": True,
                "status": "rejected", "report_id": report_id}
    if report["status"] == "confirmed":
        raise HTTPException(status_code=409,
                            detail="This report was already confirmed "
                                   "and posted")
    claim = await coll.update_one(
        {"id": report_id, "status": "pending"},
        {"$set": {"status": "rejected", "decided_by": user.get("name"),
                  "decided_at": now_iso(),
                  "decision_note": (body.note if body else "")[:300]}})
    if not claim.matched_count:
        fresh = await coll.find_one({"id": report_id})
        if fresh and fresh["status"] == "rejected":
            return {"ok": True, "already_rejected": True,
                    "status": "rejected", "report_id": report_id}
        raise HTTPException(status_code=409,
                            detail="This report was just decided")
    await _audit(user, "portal.payment.rejected", "invoice",
                 report["invoice_id"],
                 {"amount_cents": report["amount_cents"],
                  "method": report["method"], "report_id": report_id,
                  "note": (body.note if body else "")[:300]})
    return {"ok": True, "already_rejected": False, "status": "rejected",
            "report_id": report_id}


# ---------------------------------------------------------------- Stripe card checkout (live payments)
#
# Setup (env vars; see repo-root STRIPE_SETUP.md):
#   STRIPE_SECRET_KEY       - from the Stripe dashboard (sk_test_… / sk_live_…)
#   STRIPE_PUBLISHABLE_KEY  - public key (pk_…); exposed to the portal page
#   STRIPE_WEBHOOK_SECRET   - whsec_… for the /portal/stripe/webhook endpoint
#
# Key-gated like Plaid (routers/bankfeed.py): until STRIPE_SECRET_KEY is set,
# checkout returns a graceful 503 and the portal page shows "card payments
# not configured" instead of a dead button. Keys are NEVER hardcoded and
# NEVER logged — log lines carry amounts and ids only.


class PortalCheckoutIn(BaseModel):
    origin_url: str = ""  # e.g. https://books.example.com — used for success/cancel URLs


def _stripe_cfg():
    secret = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    publishable = (os.environ.get("STRIPE_PUBLISHABLE_KEY") or "").strip()
    webhook_secret = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    return secret, publishable, webhook_secret


def _stripe_configured() -> bool:
    return bool(_stripe_cfg()[0])


def _require_stripe() -> str:
    secret = _stripe_cfg()[0]
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="Card payments are not configured. Set STRIPE_SECRET_KEY to enable them (see STRIPE_SETUP.md).")
    return secret


def _stripe():
    """Lazily import + configure the Stripe SDK. Call ONLY after the
    key-gate has passed — importing before the config check (the old bug)
    turned every unconfigured checkout call into a 500."""
    import stripe
    stripe.api_key = _stripe_cfg()[0]
    return stripe


@router.post("/checkout/{token}")
async def portal_checkout(token: str, body: PortalCheckoutIn, request: Request):
    """Public: create a Stripe Checkout session for the invoice's balance.

    The customer pays on Stripe; the signature-verified webhook fulfills it
    (ledger entry + invoice totals) exactly once per session. Creating
    another session for the same invoice just makes another payment link —
    fulfillment clamps to the remaining balance.
    """
    _require_stripe()  # 503 BEFORE any SDK import when keys are absent
    stripe = _stripe()
    link = await _lookup_link(token)
    tid = link["tenant_id"]
    inv = await db.invoices.find_one({"tenant_id": tid, "id": link["invoice_id"]})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    # Dollars under a pending payment report are spoken for: never
    # charge a card for money the customer says is already on its way.
    # If the shop rejects the report, checkout opens back up in full.
    pending = await _pending_total_cents(tid, inv["id"])
    balance = inv["total_cents"] - inv.get("paid_cents", 0) - pending
    if balance <= 0:
        if pending > 0 and inv.get("paid_cents", 0) < inv["total_cents"]:
            raise HTTPException(
                status_code=409,
                detail="A payment report is waiting for the shop to "
                       "confirm it — card checkout opens again if the "
                       "shop declines it")
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
        # Log the SDK error server-side; never echo provider internals
        # (or anything key-adjacent) to an unauthenticated caller.
        logger.warning("stripe checkout create failed invoice=%s amount=%s %s: %s",
                       inv["id"], stripe_amount, stripe_currency, type(e).__name__)
        raise HTTPException(status_code=502, detail="Card payment unavailable — please try again or use another payment method.")
    await db.payment_transactions.insert_one({
        "session_id": session.id, "tenant_id": tid, "kind": "portal_invoice",
        "invoice_id": inv["id"], "token_hash": _digest(token),
        "amount_cents": balance, "currency": stripe_currency,
        "status": "initiated", "payment_status": "pending", "fulfilled": False,
        "created_at": now_iso(), "updated_at": now_iso(),
    })
    logger.info("stripe checkout created session=%s invoice=%s amount=%s %s",
                session.id, inv["id"], stripe_amount, stripe_currency)
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


async def _mark_session_paid(session) -> None:
    """Record a verified paid Checkout session and fulfill it exactly once.

    Shared by `checkout.session.completed` (immediate payment methods) and
    `checkout.session.async_payment_succeeded` (delayed methods, where the
    completed event fires while payment_status is still 'unpaid')."""
    session_id = session["id"]
    metadata = session.get("metadata") or {}
    if metadata.get("kind") != "portal_invoice":
        return  # not ours — some other integration on the same Stripe account
    if session.get("payment_status") != "paid":
        logger.info("stripe session=%s not yet paid (status=%s); awaiting async success",
                    session_id, session.get("payment_status"))
        return
    await db.payment_transactions.update_one(
        {"session_id": session_id},
        {"$set": {"payment_status": "paid", "status": "paid",
                  "amount_total": session.get("amount_total"),
                  "updated_at": now_iso()}})
    logger.info("stripe session=%s paid amount_total=%s currency=%s",
                session_id, session.get("amount_total"), session.get("currency"))
    await fulfill_portal_invoice(session_id)


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Public (Stripe server-to-server): the ONLY unauthenticated write
    surface in this integration, so signature verification is mandatory —
    the raw body + Stripe-Signature header are checked against
    STRIPE_WEBHOOK_SECRET before anything is trusted. Without the secret
    the endpoint refuses (503): an unverifiable webhook must never move
    money. Stripe retries non-2xx, and fulfillment is exactly-once, so
    duplicate deliveries are safe."""
    _, _, webhook_secret = _stripe_cfg()
    if not webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook is not configured. Set STRIPE_WEBHOOK_SECRET (see STRIPE_SETUP.md).")
    stripe = _stripe()
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    except stripe.error.SignatureVerificationError:
        logger.warning("stripe webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    if event["type"] in ("checkout.session.completed",
                         "checkout.session.async_payment_succeeded"):
        await _mark_session_paid(event["data"]["object"])
    else:
        logger.info("stripe webhook ignored event type=%s id=%s",
                    event["type"], event.get("id"))
    return {"received": True}


# ---------------- Estimate links + portal quote acceptance (bake-off #4) ----------------
# Implemented independently in our stack (behavior study: Invoice Ninja
# docs — the customer approves/rejects the quote in the portal, approval
# is timestamped, and acceptance converts to a payable invoice).

class EstimateLinkIn(BaseModel):
    estimate_id: str
    expires_in_hours: int = Field(default=DEFAULT_TTL_HOURS, ge=1, le=24 * 90)


@router.post("/estimate-links")
async def create_estimate_link(body: EstimateLinkIn, user: dict = Depends(require("ops:manage"))):
    est = await Scoped(db.estimates, user["tenant_id"]).find_one({"id": body.estimate_id})
    if not est:
        raise HTTPException(status_code=404, detail="Estimate not found")
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=body.expires_in_hours)).isoformat()
    link_doc = {
        "id": new_id(), "token_hash": _digest(token), "tenant_id": user["tenant_id"],
        "kind": "estimate", "estimate_id": est["id"], "invoice_id": None,
        "customer_id": est.get("customer_id"),
        "created_by": user.get("name"), "created_at": now_iso(),
        "expires_at": expires_at, "revoked": False, "used": False,
        "email_to": None, "email_status": "not_sent", "email_error": None,
        "email_sent_at": None,
    }
    await db.portal_links.insert_one(link_doc)
    await _audit(user, "portal.estimate_link.create", "estimate", est["id"],
                 {"expires_at": expires_at})
    return {"token": token, "expires_at": expires_at, "estimate_id": est["id"],
            "link_id": link_doc["id"]}


def _strip_estimate(est: dict) -> dict:
    return {
        "id": est.get("id"), "number": est.get("number"),
        "client_name": est.get("client_name"), "status": est.get("status"),
        "lines": [{"name": l.get("name"), "kind": l.get("kind"),
                   "amount_cents": to_cents(l.get("amount"))} for l in est.get("lines", [])],
        "subtotal_cents": est.get("subtotal_cents", 0),
        "tax_cents": est.get("tax_cents", 0),
        "total_cents": est.get("total_cents", 0),
        "deposit_cents": est.get("deposit_cents", 0),
        "deposit_due_date": est.get("deposit_due_date"),
        "notes": est.get("notes", ""),
        "portal_response": est.get("portal_response"),
        "invoice_id": est.get("invoice_id"),
    }


async def _lookup_estimate_link(token: str) -> dict:
    link = await _lookup_link(token)
    if link.get("kind") != "estimate" or not link.get("estimate_id"):
        raise HTTPException(status_code=404, detail="Link not found")
    return link


@router.get("/estimate-view/{token}")
async def portal_estimate_view(token: str):
    """Public: view the linked estimate/quote. No login required."""
    link = await _lookup_estimate_link(token)
    est = await db.estimates.find_one(
        {"tenant_id": link["tenant_id"], "id": link["estimate_id"]}, {"_id": 0})
    if not est:
        raise HTTPException(status_code=404, detail="Estimate not found")
    tenant = await db.tenants.find_one({"id": link["tenant_id"]}, {"_id": 0, "name": 1})
    return {"business": (tenant or {}).get("name", ""), "estimate": _strip_estimate(est)}


class EstimateRespondIn(BaseModel):
    decision: str = Field(pattern=r"^(accept|decline)$")
    name: str = Field(min_length=1, max_length=120)   # who is responding
    terms_accepted: bool = False


@router.post("/estimate/{token}/respond")
async def portal_estimate_respond(token: str, body: EstimateRespondIn):
    """Public: the customer accepts or declines the quote. Acceptance is
    timestamped and recorded on the estimate, and auto-converts it to a
    payable invoice through the existing conversion path."""
    link = await _lookup_estimate_link(token)
    est_coll = Scoped(db.estimates, link["tenant_id"])
    est = await est_coll.find_one({"id": link["estimate_id"]})
    if not est:
        raise HTTPException(status_code=404, detail="Estimate not found")
    if est.get("portal_response"):
        raise HTTPException(status_code=409, detail="This quote was already answered")
    if est.get("status") not in ("sent", "draft"):
        raise HTTPException(status_code=409,
                            detail=f"Quote is {est.get('status')} and cannot be answered")
    if body.decision == "accept" and not body.terms_accepted:
        raise HTTPException(status_code=422,
                            detail="Terms must be accepted to approve the quote")
    response = {"decision": body.decision, "name": body.name.strip(),
                "terms_accepted": body.terms_accepted, "at": now_iso(),
                "via": "portal"}
    if body.decision == "decline":
        await est_coll.update_one({"id": est["id"]}, {"$set": {
            "status": "declined", "portal_response": response}})
        return {"status": "declined", "estimate_id": est["id"]}
    # accept: record first (the acceptance record stands even if conversion
    # hiccups), then convert through the existing owner-side path as a
    # portal system user of the link's tenant.
    await est_coll.update_one({"id": est["id"]}, {"$set": {
        "status": "accepted", "portal_response": response}})
    from routers.estimates import convert_to_invoice
    portal_user = {"id": "portal", "tenant_id": link["tenant_id"],
                   "email": "portal@system", "name": "Client portal", "role": "portal"}
    inv = await convert_to_invoice(est["id"], portal_user)
    await db.portal_links.update_one(
        {"id": link["id"]}, {"$set": {"used": True}})
    return {"status": "accepted", "estimate_id": est["id"],
            "invoice_id": inv.get("id"), "invoice": inv}
