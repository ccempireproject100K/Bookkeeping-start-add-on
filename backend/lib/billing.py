"""Stripe subscription billing for the books product itself.

Until now the tenant ``plan`` was an attribute set at registration or by
an owner in Settings — "billing lives elsewhere" (lib/plans.py). This
module is the elsewhere: a tenant admin subscribes to Books Light or
Books Medium through Stripe Checkout, and the tenant's billing state
(plan, Stripe ids, status, current period end) is driven EXCLUSIVELY by
signature-verified webhook events. The checkout success redirect never
changes anything — it is a browser hint, not a payment proof.

Key-gating follows the portal flow (routers/portal.py) and the Connect
lib: until STRIPE_SECRET_KEY is set, every entry point refuses honestly
(503), and the Stripe SDK is imported LAZILY, only after the gate, so an
unconfigured deployment never trips an import error. Keys are read from
the environment at call time and never logged.

The subscription webhook is its own Stripe endpoint with its own
signing secret — STRIPE_BILLING_WEBHOOK_SECRET (falling back to
STRIPE_WEBHOOK_SECRET for single-endpoint setups) — mirroring how the
Connect flow keeps STRIPE_CONNECT_WEBHOOK_SECRET separate from the
portal's secret: Stripe issues one secret per endpoint, and endpoints
must not share.
"""
import os
from datetime import datetime, timezone


class BillingNotConfigured(Exception):
    """Raised by entry points when the platform Stripe key is absent.
    The router turns it into the honest 503."""


class WebhookSignatureError(Exception):
    """The webhook signature did not verify. The router turns it into a
    400; the delivery is never processed."""


# ---------------- config (env read at call time, portal pattern) ----------------

def secret() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def webhook_secret() -> str:
    return ((os.environ.get("STRIPE_BILLING_WEBHOOK_SECRET") or "").strip()
            or (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip())


def configured() -> bool:
    return bool(secret())


# ---------------- plan catalog ----------------
# Standalone subscription prices (lib/plans.py PLAN_INFO carries the same
# figures as display strings). Light and Medium are self-serve; Heavy is
# volume/custom and is NEVER sold through Checkout — the UI says so and
# points at a human. A Stripe Price id from env wins when present
# (dashboard-managed prices); otherwise the session carries inline
# price_data, the portal checkout's pattern, so test mode works with
# nothing but a secret key.

CATALOG = {
    "light": {
        "plan": "light", "name": "Books Light",
        "amount_cents": 1899, "currency": "usd", "interval": "month",
        "display": "$18.99/mo", "price_env": "STRIPE_PRICE_LIGHT",
        "self_serve": True,
    },
    "medium": {
        "plan": "medium", "name": "Books Medium",
        "amount_cents": 3999, "currency": "usd", "interval": "month",
        "display": "$39.99/mo", "price_env": "STRIPE_PRICE_MEDIUM",
        "self_serve": True,
    },
    "heavy": {
        "plan": "heavy", "name": "Books Heavy",
        "amount_cents": None, "currency": "usd", "interval": "month",
        "display": "volume-based", "price_env": None,
        "self_serve": False,
    },
}

SELF_SERVE_PLANS = tuple(p for p, c in CATALOG.items() if c["self_serve"])


def catalog_public() -> list:
    """The catalog as the billing page renders it (no env internals)."""
    return [{
        "plan": c["plan"], "name": c["name"], "display": c["display"],
        "amount_cents": c["amount_cents"], "self_serve": c["self_serve"],
    } for c in CATALOG.values()]


def price_id_for(plan: str) -> str:
    """The dashboard Price id configured for a plan, or "" (inline mode)."""
    env = CATALOG[plan]["price_env"]
    return (os.environ.get(env) or "").strip() if env else ""


def plan_for_price_id(price_id: str) -> str:
    """Reverse-map a Stripe Price id to a plan via the env-configured
    ids (used when a subscription event carries no plan metadata).
    Returns "" when the price is not one of ours."""
    if not price_id:
        return ""
    for plan in SELF_SERVE_PLANS:
        if price_id_for(plan) == price_id:
            return plan
    return ""


def line_item(plan: str) -> dict:
    c = CATALOG[plan]
    pid = price_id_for(plan)
    if pid:
        return {"price": pid, "quantity": 1}
    return {
        "price_data": {
            "currency": c["currency"],
            "unit_amount": c["amount_cents"],
            "recurring": {"interval": c["interval"]},
            "product_data": {"name": c["name"]},
        },
        "quantity": 1,
    }


# ---------------- subscription status mapping ----------------
# Stripe's vocabulary -> the tenant billing vocabulary (lib/plans and
# the billing page speak active / past_due / canceled; "incomplete" is
# kept distinct because no money has landed yet).

_STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "paused": "past_due",
    "canceled": "canceled",
    "incomplete_expired": "canceled",
    "incomplete": "incomplete",
}


def map_subscription_status(raw: str) -> str:
    return _STATUS_MAP.get((raw or "").strip().lower(), "incomplete")


def iso_from_epoch(ts) -> str:
    """Stripe epoch seconds -> ISO string ("" when absent)."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


# ---------------- Stripe SDK (lazy — call ONLY after the gate) ----------------

def _stripe():
    import stripe
    stripe.api_key = secret()
    return stripe


def create_checkout_session(*, tenant_id: str, plan: str, user_email: str,
                            customer_id: str, origin: str):
    """A subscription-mode Checkout Session for the tenant. Metadata
    rides on BOTH the session and the subscription it creates, so every
    later subscription/invoice event can find its tenant."""
    stripe = _stripe()
    meta = {"kind": "books_subscription", "tenant_id": tenant_id, "plan": plan}
    kwargs = {
        "mode": "subscription",
        "line_items": [line_item(plan)],
        "metadata": meta,
        "subscription_data": {"metadata": meta},
        "success_url": f"{origin}/settings?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{origin}/settings?billing=cancelled",
    }
    if customer_id:
        kwargs["customer"] = customer_id
    elif user_email:
        kwargs["customer_email"] = user_email
    return stripe.checkout.Session.create(**kwargs)


def create_portal_session(*, customer_id: str, return_url: str):
    """A Stripe Customer Portal session — cancel / switch plan / update
    card all happen there, never hand-rolled here."""
    stripe = _stripe()
    return stripe.billing_portal.Session.create(
        customer=customer_id, return_url=return_url)


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verify + parse a webhook delivery. Raises ValueError on a
    malformed payload and WebhookSignatureError on a bad signature,
    mirroring the portal webhook's two 400 paths."""
    stripe = _stripe()
    try:
        return stripe.Webhook.construct_event(payload, sig_header, webhook_secret())
    except ValueError:
        raise
    except Exception as e:
        if type(e).__name__ == "SignatureVerificationError":
            raise WebhookSignatureError(str(e)) from e
        raise
