"""Service-shop packaging: plan tiers, feature entitlements, trial handling.

Commercial packaging (billing lives elsewhere — this module only decides who
may use what):

  Books Light   $10/mo add-on  / $18.99/mo standalone   -> "light"
  Books Medium  $20/mo add-on  / $39.99/mo standalone   -> "medium"
  Books Heavy   volume-based   / custom                 -> "heavy"

A tenant stores its purchased ``plan`` ("light"/"medium"/"heavy"). Every new
workspace gets a 14-day Medium trial (``trial_ends_at``; owner ruling
2026-10-31 — the term is 14 days, replacing the unruled 30-day scaffold);
while the trial is active the *effective* plan is "medium" regardless of
the stored plan, and it quietly falls back to the stored plan when the
trial expires — no scheduler needed. Tenants created before plans existed
have no ``plan`` key and are grandfathered at "medium".

The trial is offered through TWO DOORS (owner ruling 2026-10-31, see
lib/trial.py): "nocard" — 14 days free + the DataClean welcome gift, no
card, no auto-conversion; "card" — the same trial + gift, PLUS the
tax-estimate welcome gift, 50% off the first paid month, and instant
referral activation, with the card auto-converting at trial end.

Enforcement: routers add ``Depends(require_feature("bank_feeds"))`` — either
router-wide via ``APIRouter(..., dependencies=[...])`` or on individual
routes — alongside the existing ``require()`` RBAC checks. A locked route
raises 402 with an "upgrade required" payload the UI can render.
"""
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException

from db import db
from lib.auth import get_current_user
from lib.base import now_iso

PLANS = ("light", "medium", "heavy")

TRIAL_DAYS = 14  # owner ruling 2026-10-31 (was an unruled 30 in scaffolding)

# Trial doors (owner ruling 2026-10-31): see lib/trial.py for the machine.
TRIAL_DOORS = ("nocard", "card")
FIRST_MONTH_DISCOUNT_PERCENT = 50  # card door only, first paid month, whole month

PLAN_INFO = {
    "light": {
        "name": "Books Light",
        "addon_price": "$10/mo",
        "standalone_price": "$18.99/mo",
        "tagline": "Invoicing, expenses, basic reports, receipt capture, light tax.",
    },
    "medium": {
        "name": "Books Medium",
        "addon_price": "$20/mo",
        "standalone_price": "$39.99/mo",
        "tagline": "Everything in Light, plus bank feeds, AI copilot actions, "
                   "collections automation, advanced reports, and 1099 prep.",
    },
    "heavy": {
        "name": "Books Heavy",
        "addon_price": "volume-based",
        "standalone_price": "custom",
        "tagline": "Everything in Medium, plus multi-entity, white-label, "
                   "and API access.",
    },
}

# feature -> minimum plan that unlocks it
FEATURE_PLANS = {
    # ---- Light ----
    "invoicing": "light",
    "expenses": "light",
    "basic_reports": "light",
    "receipt_capture": "light",
    "light_tax": "light",
    "time_tracking": "light",
    "client_portal": "light",
    # ---- Medium ----
    "bank_feeds": "medium",
    "copilot_actions": "medium",
    "collections_automation": "medium",
    "advanced_reports": "medium",
    "prep_1099": "medium",
    # ---- Heavy ----
    "multi_entity": "heavy",
    "white_label": "heavy",
    "api_access": "heavy",
}

# display order helpers
_FEATURE_LABELS = {
    "invoicing": "Invoicing & estimates",
    "expenses": "Expenses & bills",
    "basic_reports": "Basic reports (P&L, balance sheet, cash flow)",
    "receipt_capture": "Receipt capture",
    "light_tax": "Tax basics & quarterly check-ins",
    "time_tracking": "Time tracking",
    "client_portal": "Magic-link client portal",
    "bank_feeds": "Live bank feeds (Plaid)",
    "copilot_actions": "AI copilot actions",
    "collections_automation": "Collections automation",
    "advanced_reports": "Advanced reports (benchmarks, tax forecast, scenarios)",
    "prep_1099": "1099 prep & e-filing",
    "multi_entity": "Multi-entity & consolidated books",
    "white_label": "White-label for firms",
    "api_access": "API access",
}

_PLAN_ORDER = {p: i for i, p in enumerate(PLANS)}


def validate_plan(plan) -> str:
    """Normalize a requested plan; raises 422 on anything unrecognized."""
    p = (plan or "").strip().lower()
    if p not in PLANS:
        raise HTTPException(status_code=422,
                            detail=f"Unknown plan {plan!r}; choose one of {', '.join(PLANS)}")
    return p


def stored_plan(tenant: dict) -> str:
    """The plan on the tenant record. Tenants predating plans are grandfathered."""
    p = (tenant or {}).get("plan")
    if p in PLANS:
        return p
    return "medium"  # legacy tenant: full access


def trial_active(tenant: dict) -> bool:
    ends = (tenant or {}).get("trial_ends_at")
    if not ends:
        return False
    try:
        end = datetime.fromisoformat(ends)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < end
    except (ValueError, TypeError):
        return False


def effective_plan(tenant: dict) -> str:
    """Plan that actually governs access: Medium while the trial is live."""
    return "medium" if trial_active(tenant) else stored_plan(tenant)


def trial_days_remaining(tenant: dict) -> int:
    if not trial_active(tenant):
        return 0
    try:
        end = datetime.fromisoformat(tenant["trial_ends_at"])
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        delta = end - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() // 86400))
    except (ValueError, TypeError, KeyError):
        return 0


def plan_includes(plan: str, feature: str) -> bool:
    need = FEATURE_PLANS.get(feature)
    if need is None:
        raise KeyError(f"Unknown feature {feature!r}")
    return _PLAN_ORDER.get(plan, 0) >= _PLAN_ORDER[need]


def features_for(plan: str) -> dict:
    """Feature key -> label, for every feature the plan unlocks."""
    return {f: _FEATURE_LABELS.get(f, f) for f in FEATURE_PLANS
            if plan_includes(plan, f)}


def required_plan_for(*features: str) -> str:
    need = max((_PLAN_ORDER[FEATURE_PLANS[f]] for f in features), default=0)
    return PLANS[need]


def upgrade_detail(effective: str, feature: str) -> dict:
    need = FEATURE_PLANS[feature]
    return {
        "code": "upgrade_required",
        "feature": feature,
        "feature_label": _FEATURE_LABELS.get(feature, feature),
        "current_plan": effective,
        "required_plan": need,
        "required_plan_name": PLAN_INFO[need]["name"],
        "message": (
            f"{_FEATURE_LABELS.get(feature, feature)} needs "
            f"{PLAN_INFO[need]['name']} or higher — this workspace is on "
            f"{PLAN_INFO[effective]['name']}. Upgrade to unlock it."
        ),
    }


def require_feature(*features: str):
    """FastAPI dependency: 402 unless the tenant's effective plan unlocks
    every listed feature. Compose alongside ``require()`` — RBAC first."""
    async def dep(user: dict = Depends(get_current_user)) -> dict:
        tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
        eff = effective_plan(tenant or {})
        for f in features:
            if not plan_includes(eff, f):
                raise HTTPException(status_code=402,
                                    detail=upgrade_detail(eff, f))
        return user
    return dep


def new_trial_fields(door: str = "nocard") -> dict:
    """Fields stamped on a freshly created tenant: Light + 14-day Medium
    trial through the chosen door. The card door stamps the opt-in at
    once (packet unlock, first-month discount terms, instant referral
    activation); the no-card door records none of those — they can be
    added later by upgrading the door (routers/trial.py)."""
    if door not in TRIAL_DOORS:
        door = "nocard"
    now = datetime.now(timezone.utc)
    fields = {
        "plan": "light",
        "trial_started_at": now_iso(),
        "trial_ends_at": (now + timedelta(days=TRIAL_DAYS)).isoformat(),
        "trial_door": door,
        "card_opt_in_at": None,
        "referral_active": False,
        "referral_active_source": None,
        "first_month_discount_percent": None,
        "first_month_discount_applied": False,
        "trial_canceled_at": None,
    }
    if door == "card":
        fields["card_opt_in_at"] = now_iso()
        fields["referral_active"] = True
        fields["referral_active_source"] = "trial_card_opt_in"
        fields["first_month_discount_percent"] = FIRST_MONTH_DISCOUNT_PERCENT
    return fields
