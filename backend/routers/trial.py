"""Trial doors (owner ruling 2026-10-31): the two ways into the
14-day free trial, the card opt-in and everything that rides it, the
conversion paths, and cancellation with its one-tap reasons.

Door 1 "nocard": 14 days free + the DataClean welcome gift; no card,
no auto-conversion. Door 2 "card": the same, plus the tax-estimate
welcome gift, 50% off the first paid month, and instant referral
activation; the card auto-converts at trial end (lib/trial.py's lazy
sweep creates the Stripe subscription off-session when a card is on
file; without one, the state is labeled and nothing is faked).

Cancellation: completes immediately, reason or no reason — the reason
buttons are fully optional and there are no dark patterns. A reason,
when given, is recorded verbatim (tenant, timestamp, door, trial
state) in db.trial_cancellations for owner reporting. A tenant with a
live Stripe subscription cancels the subscription itself in the
Stripe Customer Portal (the product's existing machinery) — the
reason is still captured here first, and this endpoint says so.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db import db
from lib import billing as billing_lib
from lib import plans, trial as trial_lib
from lib.audit import append_audit
from lib.auth import require
from lib.base import new_id, now_iso

router = APIRouter(prefix="/trial", tags=["trial"])
logger = logging.getLogger(__name__)


async def _tenant(user) -> dict:
    return await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}


async def _audit(user, action, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action,
        "entity": "tenant", "entity_id": user["tenant_id"],
        "details": details or {}, "created_at": now_iso(),
    })


def _require_billing():
    if not billing_lib.configured():
        raise HTTPException(
            status_code=503,
            detail="Online billing is not configured. Set STRIPE_SECRET_KEY "
                   "to enable card capture and conversion.")


@router.get("")
async def get_trial(user: dict = Depends(require("ops:manage"))):
    tenant = await _tenant(user)
    conversion = await trial_lib.maybe_auto_convert(db, tenant)
    if conversion == "initiated":
        tenant = await _tenant(user)  # re-read: conversion was stamped
    state = trial_lib.public_state(tenant)
    state["conversion_check"] = conversion
    return state


class DoorIn(BaseModel):
    door: str


@router.post("/door")
async def set_door(body: DoorIn, user: dict = Depends(require("ops:manage"))):
    """Choose or change the trial door. Upgrading to the card door IS
    the card opt-in: it stamps the opt-in, unlocks the estimate gift,
    records the first-month discount terms, and activates referrals —
    effective immediately, mid-trial included. Downgrading before
    conversion withdraws the opt-in and everything that rides it."""
    door = (body.door or "").strip().lower()
    if door not in plans.TRIAL_DOORS:
        raise HTTPException(status_code=422,
                            detail=f"door must be one of {list(plans.TRIAL_DOORS)}")
    tenant = await _tenant(user)
    if trial_lib.subscribed(tenant):
        raise HTTPException(
            status_code=409,
            detail="This workspace already has a paid subscription — "
                   "the trial doors no longer apply.")
    if door == trial_lib.door_of(tenant) and trial_lib.card_opted_in(tenant) == (door == "card"):
        return trial_lib.public_state(tenant)  # no-op, idempotent
    if door == "card":
        update = {
            "trial_door": "card",
            "card_opt_in_at": now_iso(),
            "referral_active": True,
            "referral_active_source": "trial_card_opt_in",
            "first_month_discount_percent": plans.FIRST_MONTH_DISCOUNT_PERCENT,
        }
    else:
        update = {
            "trial_door": "nocard",
            "card_opt_in_at": None,
            "referral_active": False,
            "referral_active_source": None,
            "first_month_discount_percent": None,
        }
    await db.tenants.update_one({"id": user["tenant_id"]}, {"$set": update})
    await _audit(user, "trial.door_changed", {"door": door})
    return trial_lib.public_state(await _tenant(user))


class CardSessionIn(BaseModel):
    origin_url: str = ""


@router.post("/card-session")
async def card_session(body: CardSessionIn, request: Request,
                       user: dict = Depends(require("ops:manage"))):
    """Put a card on file (Stripe Checkout, setup mode — no charge)
    for a card-door tenant. Honest 503 when billing isn't configured:
    the opt-in itself is already recorded; only capture waits."""
    tenant = await _tenant(user)
    if trial_lib.door_of(tenant) != "card":
        raise HTTPException(
            status_code=422,
            detail="Choose the card trial door first — the card on file "
                   "is what auto-converts at trial end.")
    _require_billing()
    origin = (body.origin_url or request.headers.get("origin") or "").rstrip("/")
    if not origin.startswith("http"):
        raise HTTPException(status_code=422, detail="origin_url is required")
    try:
        session = billing_lib.create_card_setup_session(
            tenant_id=user["tenant_id"], user_email=user.get("email") or "",
            customer_id=(tenant.get("billing") or {}).get("stripe_customer_id") or "",
            origin=origin)
    except Exception as e:
        logger.warning("card setup session failed tenant=%s %s",
                       user["tenant_id"], type(e).__name__)
        raise HTTPException(status_code=502,
                            detail="Billing is unavailable — please try again.")
    await _audit(user, "trial.card_session_started", {"session_id": session.id})
    return {"checkout_url": session.url, "session_id": session.id}


class ConvertIn(BaseModel):
    plan: str
    origin_url: str = ""


@router.post("/convert")
async def convert(body: ConvertIn, request: Request,
                  user: dict = Depends(require("ops:manage"))):
    """Convert now through Stripe Checkout. The card opt-in path
    carries the first-month welcome coupon (50% off the first invoice,
    Stripe's coupon mechanics); any other conversion is at list. As
    everywhere in this product, the plan changes only when the
    webhook confirms payment."""
    tenant = await _tenant(user)
    _require_billing()
    plan = plans.validate_plan(body.plan)
    if plan not in billing_lib.SELF_SERVE_PLANS:
        raise HTTPException(
            status_code=422,
            detail="Books Heavy is volume-based and isn't sold online — "
                   "contact us and we'll set it up with you.")
    origin = (body.origin_url or request.headers.get("origin") or "").rstrip("/")
    if not origin.startswith("http"):
        raise HTTPException(status_code=422, detail="origin_url is required")
    await db.tenants.update_one({"id": user["tenant_id"]},
                                {"$set": {"trial_plan_choice": plan}})
    coupon_id = ""
    if trial_lib.card_opted_in(tenant):
        coupon_id = billing_lib.ensure_first_month_coupon()
    try:
        session = billing_lib.create_checkout_session(
            tenant_id=user["tenant_id"], plan=plan,
            user_email=user.get("email") or "",
            customer_id=(tenant.get("billing") or {}).get("stripe_customer_id") or "",
            origin=origin, coupon_id=coupon_id)
    except Exception as e:
        logger.warning("trial convert checkout failed tenant=%s %s",
                       user["tenant_id"], type(e).__name__)
        raise HTTPException(status_code=502,
                            detail="Billing is unavailable — please try again.")
    await _audit(user, "trial.conversion_started",
                 {"plan": plan, "coupon_applied": bool(coupon_id),
                  "session_id": session.id})
    return {"checkout_url": session.url, "session_id": session.id,
            "coupon_applied": bool(coupon_id)}


class CancelIn(BaseModel):
    reason: str | None = None      # one of lib.trial.CANCEL_REASONS
    note: str | None = None        # optional free text


@router.post("/cancel")
async def cancel(body: CancelIn, user: dict = Depends(require("ops:manage"))):
    """Cancel — effective immediately, with or without a reason. No
    dark patterns: this endpoint never refuses, never requires the
    reason, and ends a trial on the spot. For a live subscription the
    subscription itself is canceled in the Stripe Customer Portal
    (existing machinery); the reason is recorded here either way."""
    tenant = await _tenant(user)
    state_before = trial_lib.trial_state(tenant)
    reason = body.reason if body.reason in trial_lib.CANCEL_REASONS else None
    note = (body.note or "").strip()
    if body.reason and reason is None:
        # An off-list reason is kept verbatim as free text, never lost.
        note = (body.reason.strip() + (f" — {note}" if note else "")).strip()
    doc = {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "user_id": user["id"], "user_email": user.get("email"),
        "reason": reason, "note": note or None,
        "door": trial_lib.door_of(tenant), "trial_state": state_before,
        "canceled_at": now_iso(),
    }
    try:
        await db.trial_cancellations.insert_one(doc)
    except Exception:
        logger.warning("trial cancellation record failed tenant=%s",
                       user["tenant_id"], exc_info=True)
    await _audit(user, "trial.canceled",
                 {"reason": reason, "door": doc["door"], "state": state_before})
    if state_before == "converted":
        return {"canceled": True, "scope": "reason_recorded",
                "next": "portal",
                "message": "Thanks — your reason is recorded. Your "
                           "subscription itself is canceled in the "
                           "billing portal, which is where Stripe "
                           "confirms it."}
    if state_before == "trialing":
        await db.tenants.update_one(
            {"id": user["tenant_id"]},
            {"$set": {"trial_canceled_at": now_iso(),
                      "trial_ends_at": now_iso()}})
    return {"canceled": True, "scope": "trial",
            "state": trial_lib.trial_state(await _tenant(user))}
