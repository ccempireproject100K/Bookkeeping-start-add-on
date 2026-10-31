"""Trial doors: the 14-day free trial's state machine (owner ruling
2026-10-31).

TWO DOORS at sign-up, stated plainly to the tenant:

* Door 1 — "Free trial, no card" (``trial_door == "nocard"``): 14 days
  free with the full (Medium) product, plus the DataClean welcome
  gift. No card, no auto-conversion: the trial simply ends, the
  workspace falls back to its stored plan (Light for a new signup),
  and its data is untouched.
* Door 2 — "Free trial with card" (``trial_door == "card"``): the
  same 14 days and the same gift, PLUS three extras that ride the
  card opt-in: the tax-estimate welcome gift unlocks, the first paid
  month is 50% off (the whole month, never prorated; month two bills
  at list), and referral capability activates instantly, during the
  trial. The card auto-converts at trial end.

The DataClean gift itself is UNGATED (every tenant, any door, even
before any trial choice) — only the estimate packet rides the opt-in.

Money honesty: subscriptions are created only through Stripe
(lib/billing.py) and billing state changes only in the webhook. This
module records TERMS (the 50% promise, in exact cents) and state; it
never charges anything itself. The auto-conversion sweep below fires
only when a card is actually on file and Stripe is configured, and
even then it only CREATES the subscription in Stripe — the webhook
remains the sole writer of billing state. When any piece is missing,
the sweep reports a labeled pending state instead of faking a charge.

Expiry (existing product behavior, kept): trial end falls back to the
stored plan — access narrows to that plan's features (locked routes
answer 402 with an upgrade payload), data is never deleted, and the
trial state is labeled "expired" everywhere it is shown.
"""
from datetime import datetime, timezone

from lib import billing as billing_lib
from lib import plans

DOOR_NOCARD = "nocard"
DOOR_CARD = "card"

# The one-tap cancel reasons (owner's set, verbatim). The cancel flow
# is fully optional and dark-pattern-free: cancellation completes
# immediately whether or not a reason is given.
CANCEL_REASONS = (
    "Not ready yet",
    "Too expensive",
    "Missing a feature",
    "Switching to another tool",
    "Just testing it out",
)


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def door_of(tenant: dict) -> str:
    """The tenant's trial door. Tenants created before the doors
    existed never opted into a card, so they read as the no-card door."""
    return "card" if (tenant or {}).get("trial_door") == "card" else "nocard"


def card_opted_in(tenant: dict) -> bool:
    return bool((tenant or {}).get("card_opt_in_at"))


def subscribed(tenant: dict) -> bool:
    return ((tenant or {}).get("billing") or {}).get("status") == "active"


def trial_state(tenant: dict) -> str:
    """converted | canceled | trialing | expired | none (no trial on
    record — legacy/grandfathered tenants)."""
    tenant = tenant or {}
    if subscribed(tenant):
        return "converted"
    if tenant.get("trial_canceled_at"):
        return "canceled"
    if not tenant.get("trial_ends_at"):
        return "none"
    return "trialing" if plans.trial_active(tenant) else "expired"


def gift_estimate_unlocked(tenant: dict) -> bool:
    """The tax-estimate welcome gift rides the card opt-in (owner
    ruling 2026-10-31). The DataClean gift is never gated."""
    return card_opted_in(tenant)


def referral_active(tenant: dict) -> bool:
    """Referral capability: instant at the card opt-in (door 2), or at
    conversion for everyone else (the pre-existing default). This is
    the entitlement flag only — the referral program's mechanics
    (payouts, qualification, clocks) are a separate owner-gated build."""
    tenant = tenant or {}
    if tenant.get("referral_active"):
        return True
    return subscribed(tenant)


def discount_cents(amount_cents: int, percent: int) -> int:
    """Exact-cents percentage discount, rounded half-up — the same
    rounding Stripe applies to a percent_off coupon, so the terms we
    state are the terms Stripe will compute."""
    return (int(amount_cents) * int(percent) + 50) // 100


def first_month_terms(plan: str, opted_in: bool) -> dict:
    """The first paid month's price terms for a self-serve plan, in
    exact cents. The discount exists ONLY on the card opt-in path."""
    cat = billing_lib.CATALOG[plan]
    list_cents = cat["amount_cents"]
    if opted_in:
        off = discount_cents(list_cents, plans.FIRST_MONTH_DISCOUNT_PERCENT)
        return {"plan": plan, "list_cents": list_cents,
                "discount_percent": plans.FIRST_MONTH_DISCOUNT_PERCENT,
                "discount_cents": off, "first_month_cents": list_cents - off,
                "month_two_cents": list_cents}
    return {"plan": plan, "list_cents": list_cents,
            "discount_percent": 0, "discount_cents": 0,
            "first_month_cents": list_cents, "month_two_cents": list_cents}


def public_state(tenant: dict) -> dict:
    """Everything the trial surfaces render (GET /trial, the gift
    page's gates, the settings panel)."""
    tenant = tenant or {}
    opted = card_opted_in(tenant)
    return {
        "door": door_of(tenant),
        "state": trial_state(tenant),
        "trial_active": plans.trial_active(tenant),
        "trial_started_at": tenant.get("trial_started_at"),
        "trial_ends_at": tenant.get("trial_ends_at"),
        "trial_days_remaining": plans.trial_days_remaining(tenant),
        "trial_days": plans.TRIAL_DAYS,
        "card_opt_in_at": tenant.get("card_opt_in_at"),
        "card_on_file": bool((tenant.get("billing") or {}).get("card_on_file")),
        "referral_active": referral_active(tenant),
        "referral_active_source": tenant.get("referral_active_source")
        or ("conversion" if subscribed(tenant) else None),
        "gifts": {"clean": True,  # ungated for every tenant
                  "estimate": opted},
        "first_month_discount_percent":
            plans.FIRST_MONTH_DISCOUNT_PERCENT if opted else None,
        "first_month_discount_applied":
            bool(tenant.get("first_month_discount_applied")),
        "terms": {p: first_month_terms(p, opted)
                  for p in billing_lib.SELF_SERVE_PLANS},
        "cancel_reasons": list(CANCEL_REASONS),
        "trial_canceled_at": tenant.get("trial_canceled_at"),
        "conversion": tenant.get("trial_conversion") or None,
    }


async def maybe_auto_convert(db, tenant: dict) -> str:
    """Door 2's auto-conversion at trial end, lazily evaluated (the
    codebase has no scheduler; expiry itself is computed lazily too).

    Returns the conversion status:
      "not_due"                 — not a card-door tenant past trial end
      "already"                 — subscribed or conversion initiated
      "billing_not_configured"  — Stripe keys absent; nothing charged,
                                  the 50% terms stay on record
      "awaiting_card"           — no card on file; nothing charged, the
                                  tenant converts by adding a card
      "initiated"               — the Stripe subscription was created
                                  off-session with the first-month
                                  coupon; the webhook drives state
    Never fakes a conversion: without Stripe + a stored card + a
    configured price path, it reports the labeled state and stops.
    """
    tenant = tenant or {}
    if subscribed(tenant) or (tenant.get("trial_conversion") or {}).get("status") == "initiated":
        return "already"
    if door_of(tenant) != "card" or not card_opted_in(tenant):
        return "not_due"
    if trial_state(tenant) != "expired":
        return "not_due"
    if not billing_lib.configured():
        return "billing_not_configured"
    b = tenant.get("billing") or {}
    customer_id = b.get("stripe_customer_id")
    if not customer_id or not b.get("card_on_file"):
        return "awaiting_card"
    plan = (tenant.get("trial_plan_choice")
            or (b.get("plan") if b.get("plan") in billing_lib.SELF_SERVE_PLANS else None)
            or "medium")
    coupon_id = billing_lib.ensure_first_month_coupon()
    sub = billing_lib.create_subscription_off_session(
        customer_id=customer_id, plan=plan, coupon_id=coupon_id,
        tenant_id=tenant["id"],
        payment_method_id=b.get("card_payment_method_id") or "")
    conv = {"status": "initiated", "subscription_id": getattr(sub, "id", None),
            "plan": plan, "coupon_id": coupon_id,
            "initiated_at": datetime.now(timezone.utc).isoformat()}
    await db.tenants.update_one({"id": tenant["id"]},
                                {"$set": {"trial_conversion": conv}})
    return "initiated"
