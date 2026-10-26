"""Plan tiers: what the workspace is on, what's unlocked, owner plan changes.

Billing lives elsewhere (Momo add-on billing or standalone checkout); this
router only records which plan the workspace is entitled to and reports the
effective plan (trial-aware). Owner-only changes are audit-logged.
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from db import db
from lib.audit import append_audit
from lib import plans
from lib.auth import get_current_user, require
from lib.base import new_id, now_iso

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("/current")
async def current_plan(user: dict = Depends(get_current_user)):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    eff = plans.effective_plan(tenant)
    trial = plans.trial_active(tenant)
    return {
        "plan": plans.stored_plan(tenant),        # purchased plan on record
        "effective_plan": eff,                     # governs access (trial-aware)
        "effective_plan_name": plans.PLAN_INFO[eff]["name"],
        "trial_active": trial,
        "trial_days_remaining": plans.trial_days_remaining(tenant),
        "trial_ends_at": tenant.get("trial_ends_at"),
        "features": plans.features_for(eff),
        "plans": plans.PLAN_INFO,
        "features_by_plan": {
            p: plans.features_for(p) for p in plans.PLANS
        },
    }


class PlanChangeBody(BaseModel):
    plan: str


@router.post("/change")
async def change_plan(body: PlanChangeBody, request: Request,
                      user: dict = Depends(require("settings:write"))):
    """Owner sets the workspace plan (the actual charge happens in the billing
    system; this records entitlement). Downgrading takes effect immediately —
    features above the new plan return 402 from that moment on."""
    new_plan = plans.validate_plan(body.plan)
    await db.tenants.update_one(
        {"id": user["tenant_id"]}, {"$set": {"plan": new_plan}})
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": "plan.changed",
        "entity": "tenant", "entity_id": user["tenant_id"],
        "details": {"plan": new_plan, "ip": getattr(request.client, "host", None)},
        "created_at": now_iso(),
    })
    return {
        "ok": True,
        "plan": new_plan,
        "note": "Entitlement updated. Any charge or refund is handled by billing, "
                "not here.",
    }
