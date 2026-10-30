"""Autopay for recurring invoices (bake-off #15) — Stripe off-session.

TEST MODE ONLY in this build: charges are refused unless the configured
Stripe key is a test key. The existing BILLING_ALLOW_LIVE gate is
respected and never widened here; no live charge can be made from this
module. Money-movement rules are unchanged.

Flow: a customer explicitly opts in and saves a card (consent record with
timestamp + the exact consent text version shown). When a recurring
invoice is issued for that customer and the tenant has autopay enabled,
the saved method is charged off-session for the invoice balance. Failures
retry on the tenant's retry ladder, then the invoice falls back to the
normal manual/collections path with the failure recorded.

Implemented independently in our stack (behavior study: Invoice Ninja
Help Center auto-billing articles).
"""
import logging
import os
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib import ledger as L, posting as P
from lib.base import Scoped, new_id, now_iso, today_str
from lib.auth import require, get_current_user

logger = logging.getLogger("mindatease")
router = APIRouter(prefix="/autopay", tags=["autopay"])

CONSENT_TEXT_VERSION = "autopay-consent-v1"
DEFAULT_RETRY_DAYS = [1, 3, 7]


# ---------------- stripe plumbing (test-mode fenced) ----------------

def stripe_mode(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    if key.startswith("sk_test_") or key.startswith("rk_test_"):
        return "test"
    if key.startswith("sk_live_") or key.startswith("rk_live_"):
        return "live"
    return None


def _stripe_key() -> Optional[str]:
    return os.environ.get("STRIPE_SECRET_KEY") or None


def billing_allow_live() -> bool:
    return (os.environ.get("BILLING_ALLOW_LIVE") or "").strip().lower() == "true"


def _require_test_stripe():
    """Return the configured stripe module, or raise. This build charges
    test keys only — a live key is refused even if BILLING_ALLOW_LIVE is
    set, because autopay ships in test mode first."""
    key = _stripe_key()
    if not key:
        raise HTTPException(status_code=503,
                            detail="Stripe is not configured (STRIPE_SECRET_KEY unset)")
    mode = stripe_mode(key)
    if mode != "test":
        raise HTTPException(
            status_code=503,
            detail="Autopay is enabled in Stripe TEST MODE only in this build; "
                   "a live key is refused here.")
    try:
        import stripe
    except ImportError:
        raise HTTPException(status_code=503, detail="stripe library unavailable")
    stripe.api_key = key
    return stripe


# ---------------- config ----------------

def _cfg_coll(user):
    return Scoped(db.autopay_configs, user["tenant_id"])


async def get_config(user) -> dict:
    doc = await _cfg_coll(user).find_one({"id": "config"}) or {}
    return {
        "enabled": bool(doc.get("enabled", False)),
        "retry_days": doc.get("retry_days") or list(DEFAULT_RETRY_DAYS),
        "updated_at": doc.get("updated_at"),
    }


class ConfigIn(BaseModel):
    enabled: Optional[bool] = None
    retry_days: Optional[List[int]] = None


@router.get("/config")
async def read_config(user: dict = Depends(get_current_user)):
    return await get_config(user)


@router.put("/config")
async def write_config(body: ConfigIn, user: dict = Depends(require("ops:manage"))):
    cfg = await get_config(user)
    if body.enabled is not None:
        cfg["enabled"] = bool(body.enabled)
    if body.retry_days is not None:
        days = sorted({int(d) for d in body.retry_days if 0 <= int(d) <= 30})
        cfg["retry_days"] = days or list(DEFAULT_RETRY_DAYS)
    cfg["updated_at"] = now_iso()
    coll = _cfg_coll(user)
    if await coll.find_one({"id": "config"}):
        await coll.update_one({"id": "config"}, {"$set": cfg})
    else:
        await coll.insert_one({"id": "config", **cfg})
    return cfg


# ---------------- saved methods + consent ----------------

def _methods(user):
    return Scoped(db.saved_payment_methods, user["tenant_id"])


def _method_view(m: dict) -> dict:
    return {"id": m["id"], "customer_id": m.get("customer_id"),
            "brand": m.get("brand"), "last4": m.get("last4"),
            "exp_month": m.get("exp_month"), "exp_year": m.get("exp_year"),
            "status": m.get("status"),
            "consent": m.get("consent"),
            "created_at": m.get("created_at")}


@router.get("/methods")
async def list_methods(customer_id: Optional[str] = None,
                       user: dict = Depends(require("ledger:read"))):
    q = {"customer_id": customer_id} if customer_id else {}
    rows = await _methods(user).find_list(q, sort=[("created_at", -1)], limit=200)
    return [_method_view(m) for m in rows]


class SaveMethodIn(BaseModel):
    customer_id: str
    payment_method_id: str          # created client-side by Stripe.js
    consent: bool                   # explicit opt-in at save time
    consent_email: Optional[str] = None


@router.post("/methods")
async def save_method(body: SaveMethodIn, user: dict = Depends(require("ops:manage"))):
    """Attach a client-created payment method to a Stripe customer and
    store it WITH its consent record. No consent, no save."""
    if not body.consent:
        raise HTTPException(status_code=422,
                            detail="Autopay requires the customer's explicit consent")
    cust = await Scoped(db.customers, user["tenant_id"]).find_one({"id": body.customer_id})
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    stripe = _require_test_stripe()
    try:
        sc_id = cust.get("stripe_customer_id")
        if not sc_id:
            sc = stripe.Customer.create(
                email=cust.get("email") or None, name=cust.get("name") or None,
                metadata={"tenant_id": user["tenant_id"], "customer_id": cust["id"]})
            sc_id = sc["id"]
            await Scoped(db.customers, user["tenant_id"]).update_one(
                {"id": cust["id"]}, {"$set": {"stripe_customer_id": sc_id}})
        pm = stripe.PaymentMethod.retrieve(body.payment_method_id)
        if pm.get("customer") != sc_id:
            stripe.PaymentMethod.attach(body.payment_method_id, customer=sc_id)
            pm = stripe.PaymentMethod.retrieve(body.payment_method_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)[:200]}")
    card = pm.get("card") or {}
    doc = {
        "id": new_id(), "customer_id": cust["id"],
        "stripe_customer_id": sc_id,
        "stripe_payment_method_id": body.payment_method_id,
        "brand": card.get("brand"), "last4": card.get("last4"),
        "exp_month": card.get("exp_month"), "exp_year": card.get("exp_year"),
        "status": "active",
        "consent": {"granted": True, "at": now_iso(),
                    "email": body.consent_email or cust.get("email"),
                    "text_version": CONSENT_TEXT_VERSION,
                    "recorded_by": user.get("email")},
        "created_at": now_iso(),
    }
    await _methods(user).insert_one(doc)
    return _method_view(doc)


@router.post("/methods/{method_id}/revoke")
async def revoke_method(method_id: str, user: dict = Depends(require("ops:manage"))):
    m = await _methods(user).find_one({"id": method_id})
    if not m:
        raise HTTPException(status_code=404, detail="Saved method not found")
    await _methods(user).update_one(
        {"id": method_id},
        {"$set": {"status": "revoked", "revoked_at": now_iso(),
                  "revoked_by": user.get("email")}})
    return {"id": method_id, "status": "revoked"}


# ---------------- charging ----------------

def next_retry_date(attempt_no: int, retry_days: list, from_date: str) -> Optional[str]:
    """attempt_no is 1-based count of failures so far. Returns the date of
    the next retry, or None when the ladder is exhausted."""
    if attempt_no < 1 or attempt_no > len(retry_days):
        return None
    d = date.fromisoformat(from_date) + timedelta(days=int(retry_days[attempt_no - 1]))
    return d.isoformat()


async def _apply_payment(tenant_id: str, inv: dict, cents: int, pi_id: str) -> dict:
    actor = {"id": "autopay", "email": "autopay@system", "name": "Autopay",
             "role": "owner", "tenant_id": tenant_id}
    entry = await L.post_journal_entry(db, tenant_id, actor, {
        "entry_date": today_str(), "source": "stripe_autopay", "source_ref": inv["id"],
        "memo": f"Autopay card payment — {inv.get('client_name', '')}",
        "lines": [P.dr("1050", cents, "Stripe autopay settlement"),
                  P.cr("1200", cents, "AR cleared")],
    }, status="posted")
    new_paid = inv.get("paid_cents", 0) + cents
    status = "paid" if new_paid >= inv["total_cents"] else "partial"
    pmt = {"date": today_str(), "amount_cents": cents, "cash_cents": cents,
           "credit_cents": 0, "method": "card_autopay", "processor": "stripe",
           "reference": pi_id, "entry": entry["number"], "source": "stripe_autopay"}
    await db.invoices.update_one(
        {"tenant_id": tenant_id, "id": inv["id"]},
        {"$set": {"status": status, "paid_cents": new_paid,
                  "autopay_status": "paid" if status == "paid" else "partial"},
         "$push": {"payments": pmt}})
    return {"entry": entry["number"], "status": status}


async def _charge_invoice(stripe, user, inv: dict, method: dict) -> dict:
    tid = user["tenant_id"]
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    attempts = await Scoped(db.autopay_attempts, tid).find_list(
        {"invoice_id": inv["id"]}, sort=[("created_at", -1)], limit=50)
    failures = sum(1 for a in attempts if a["result"] == "failed")
    cfg = await get_config(user)
    attempt = {"id": new_id(), "invoice_id": inv["id"], "customer_id": inv["customer_id"],
               "method_id": method["id"], "amount_cents": balance,
               "created_at": now_iso(), "attempt_no": failures + 1}
    try:
        pi = stripe.PaymentIntent.create(
            amount=balance, currency=(inv.get("currency") or "usd").lower(),
            customer=method["stripe_customer_id"],
            payment_method=method["stripe_payment_method_id"],
            off_session=True, confirm=True,
            description=f"Autopay for invoice {inv.get('number') or inv['id']}",
            metadata={"tenant_id": tid, "invoice_id": inv["id"],
                      "kind": "autopay_recurring"})
    except Exception as e:
        attempt.update({"result": "failed", "error": str(e)[:300]})
        await Scoped(db.autopay_attempts, tid).insert_one(attempt)
        nxt = next_retry_date(failures + 1, cfg["retry_days"], today_str())
        await Scoped(db.invoices, tid).update_one(
            {"id": inv["id"]},
            {"$set": {"autopay_status": "retry_scheduled" if nxt else "failed",
                      "autopay_next_retry": nxt,
                      "autopay_last_error": attempt["error"]}})
        return {"invoice_id": inv["id"], "result": "failed",
                "error": attempt["error"], "next_retry": nxt}
    if pi.get("status") == "succeeded":
        applied = await _apply_payment(tid, inv, balance, pi["id"])
        attempt.update({"result": "succeeded", "payment_intent": pi["id"]})
        await Scoped(db.autopay_attempts, tid).insert_one(attempt)
        return {"invoice_id": inv["id"], "result": "succeeded", **applied}
    attempt.update({"result": "failed",
                    "error": f"PaymentIntent status {pi.get('status')}"})
    await Scoped(db.autopay_attempts, tid).insert_one(attempt)
    nxt = next_retry_date(failures + 1, cfg["retry_days"], today_str())
    await Scoped(db.invoices, tid).update_one(
        {"id": inv["id"]},
        {"$set": {"autopay_status": "retry_scheduled" if nxt else "failed",
                  "autopay_next_retry": nxt,
                  "autopay_last_error": attempt["error"]}})
    return {"invoice_id": inv["id"], "result": "failed",
            "error": attempt["error"], "next_retry": nxt}


@router.post("/run-due")
async def run_due(user: dict = Depends(require("ops:manage"))):
    """Charge every due, unpaid invoice whose customer has an active
    consented method. Only invoices generated from recurring templates
    (template_id set) are eligible — autopay never touches ad-hoc invoices.
    Retry-dated invoices wait for their retry date."""
    return await run_due_for_user(user)


async def run_due_for_user(user) -> dict:
    cfg = await get_config(user)
    if not cfg["enabled"]:
        return {"enabled": False, "charged": 0, "results": []}
    stripe = _require_test_stripe()
    tid = user["tenant_id"]
    today = today_str()
    invs = await Scoped(db.invoices, tid).find_list(
        {"status": {"$in": ["issued", "partial"]}}, limit=1000)
    results = []
    for inv in invs:
        if not inv.get("template_id"):
            continue    # recurring-generated only
        due = (inv.get("due_date") or inv.get("issue_date") or today)[:10]
        if due > today:
            continue
        nxt = inv.get("autopay_next_retry")
        if inv.get("autopay_status") == "retry_scheduled" and nxt and nxt > today:
            continue
        if inv.get("autopay_status") in ("paid", "failed"):
            continue
        method = await _methods(user).find_one(
            {"customer_id": inv["customer_id"], "status": "active"})
        if not method or not (method.get("consent") or {}).get("granted"):
            continue
        results.append(await _charge_invoice(stripe, user, inv, method))
    return {"enabled": True,
            "charged": sum(1 for r in results if r["result"] == "succeeded"),
            "failed": sum(1 for r in results if r["result"] == "failed"),
            "results": results}


async def run_autopay_all() -> dict:
    """Scheduler entry: every tenant with autopay enabled. Test-mode fence
    lives in _require_test_stripe — with a live/unset key this records
    skips, never charges."""
    outcome = {"tenants": 0, "charged": 0, "failed": 0, "skipped": []}
    tenants = await db.tenants.find({}, {"id": 1}).to_list(1000)
    for t in tenants:
        user = {"id": "scheduler", "tenant_id": t["id"],
                "email": "scheduler@system", "name": "Autopay scheduler"}
        try:
            cfg = await get_config(user)
            if not cfg["enabled"]:
                continue
            res = await run_due_for_user(user)
            outcome["tenants"] += 1
            outcome["charged"] += res.get("charged", 0)
            outcome["failed"] += res.get("failed", 0)
        except HTTPException as e:
            outcome["skipped"].append({"tenant_id": t["id"], "reason": str(e.detail)[:120]})
        except Exception as e:
            outcome["skipped"].append({"tenant_id": t["id"], "reason": str(e)[:120]})
    return outcome
