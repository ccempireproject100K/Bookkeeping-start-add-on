"""Stripe subscription billing for the books product itself.

Until now the tenant ``plan`` was an attribute set at registration or by
an owner in Settings — "billing lives elsewhere" (lib/plans.py). This
module is the elsewhere: a tenant admin subscribes to Books Light,
Books Medium, or Books Heavy through Stripe Checkout, and the tenant's
billing state (plan, Stripe ids, status, current period end) is driven
EXCLUSIVELY by signature-verified webhook events. The checkout success
redirect never changes anything — it is a browser hint, not a payment
proof.

Books Heavy pricing (owner stamp 2026-11-19): $99/mo covering up to 3
shops — a "shop" is a business with its own books, i.e. a tenant the
login owns — plus $21/mo per additional shop. All transactions are
included; there are no volume caps. Heavy is sold as TWO subscription
items (base + per-extra-shop) so the extra-shop quantity can be
adjusted when shops are added; see heavy_line_items() and
update_subscription_shop_count().

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
# figures as display strings). All three plans are self-serve through
# Checkout. Heavy is priced per shop (owner stamp 2026-11-19): a $99/mo
# base covering up to HEAVY_INCLUDED_SHOPS shops, plus $21/mo for each
# additional shop, carried as a second subscription item whose quantity
# is the extra-shop count. A Stripe Price id from env wins when present
# (dashboard-managed prices); otherwise the session carries inline
# price_data, the portal checkout's pattern, so test mode works with
# nothing but a secret key.

HEAVY_INCLUDED_SHOPS = 3
HEAVY_BASE_CENTS = 9900
HEAVY_EXTRA_SHOP_CENTS = 2100

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
        "amount_cents": HEAVY_BASE_CENTS, "currency": "usd", "interval": "month",
        "display": "$99/mo — up to 3 shops, +$21/mo per extra shop",
        "price_env": "STRIPE_PRICE_HEAVY",
        "self_serve": True,
        "included_shops": HEAVY_INCLUDED_SHOPS,
        "extra_shop_cents": HEAVY_EXTRA_SHOP_CENTS,
        "extra_price_env": "STRIPE_PRICE_HEAVY_EXTRA_SHOP",
        "extra_name": "Books Heavy — additional shop",
    },
}

SELF_SERVE_PLANS = tuple(p for p, c in CATALOG.items() if c["self_serve"])


def catalog_public() -> list:
    """The catalog as the billing page renders it (no env internals)."""
    out = []
    for c in CATALOG.values():
        row = {
            "plan": c["plan"], "name": c["name"], "display": c["display"],
            "amount_cents": c["amount_cents"], "self_serve": c["self_serve"],
        }
        if c["plan"] == "heavy":
            row["included_shops"] = c["included_shops"]
            row["extra_shop_cents"] = c["extra_shop_cents"]
        out.append(row)
    return out


def price_id_for(plan: str) -> str:
    """The dashboard Price id configured for a plan, or "" (inline mode)."""
    env = CATALOG[plan]["price_env"]
    return (os.environ.get(env) or "").strip() if env else ""


def extra_price_id_for(plan: str = "heavy") -> str:
    """The dashboard Price id for Heavy's per-extra-shop item, or ""."""
    env = CATALOG[plan].get("extra_price_env")
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
        if extra_price_id_for(plan) == price_id:
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


# ---------------- Heavy per-shop pricing ----------------

def heavy_extra_shops(shop_count) -> int:
    """Shops billed on top of the Heavy base: every shop past the
    included three. An account always has at least the shop it is
    checking out from, so counts below 1 clamp to 1."""
    n = max(1, int(shop_count or 1))
    return max(0, n - HEAVY_INCLUDED_SHOPS)


def heavy_amount_cents(shop_count) -> int:
    """The full monthly Heavy price for a shop count: $99 covering up
    to 3 shops, + $21 per extra shop (1→9900, 3→9900, 5→14100,
    10→24600). All transactions included — no volume term exists."""
    return HEAVY_BASE_CENTS + HEAVY_EXTRA_SHOP_CENTS * heavy_extra_shops(shop_count)


def _heavy_extra_item(shop_count) -> dict:
    c = CATALOG["heavy"]
    pid = extra_price_id_for("heavy")
    if pid:
        return {"price": pid, "quantity": heavy_extra_shops(shop_count)}
    return {
        "price_data": {
            "currency": c["currency"],
            "unit_amount": c["extra_shop_cents"],
            "recurring": {"interval": c["interval"]},
            "product_data": {"name": c["extra_name"]},
        },
        "quantity": heavy_extra_shops(shop_count),
    }


def line_items(plan: str, shop_count=None) -> list:
    """The Checkout/subscription line items for a plan. Light and
    Medium are a single quantity-1 item; Heavy is the base item plus,
    when the account owns more than the included shops, the
    per-extra-shop item at quantity = extra shops."""
    items = [line_item(plan)]
    if plan == "heavy" and heavy_extra_shops(shop_count or 1) > 0:
        items.append(_heavy_extra_item(shop_count or 1))
    return items


def _item_price(item) -> dict:
    p = item.get("price") if isinstance(item, dict) else getattr(item, "price", None)
    if isinstance(p, dict):
        return p
    if p is None:
        return {}
    return {"id": getattr(p, "id", None),
            "unit_amount": getattr(p, "unit_amount", None)}


def heavy_shops_from_items(items) -> int:
    """Billed shop count from a Heavy subscription's items: the base
    item covers HEAVY_INCLUDED_SHOPS shops and the extra item's
    quantity is the rest. Returns None when no Heavy base item is
    present. Items are matched by env-configured Price ids when set,
    else by unit amount (9900 base / 2100 extra — amounts no other
    Books item uses)."""
    base_found = False
    extra_qty = 0
    base_pid = price_id_for("heavy")
    extra_pid = extra_price_id_for("heavy")
    for item in items or []:
        price = _item_price(item)
        qty = item.get("quantity") if isinstance(item, dict) else getattr(item, "quantity", 0)
        if (base_pid and price.get("id") == base_pid) or \
                (not base_pid and price.get("unit_amount") == HEAVY_BASE_CENTS):
            base_found = True
        elif (extra_pid and price.get("id") == extra_pid) or \
                (not extra_pid and price.get("unit_amount") == HEAVY_EXTRA_SHOP_CENTS):
            extra_qty = int(qty or 0)
    if not base_found:
        return None
    return HEAVY_INCLUDED_SHOPS + extra_qty


def update_subscription_shop_count(subscription_id: str, shop_count):
    """Adjust a live Heavy subscription to a new shop count: the base
    item stays at quantity 1; the extra-shop item is created, resized,
    or removed to match. Prorations are created (Stripe's default for
    a quantity change), so an added shop is billed from when it is
    added. Returns the modified subscription."""
    stripe = _stripe()
    sub = stripe.Subscription.retrieve(subscription_id)
    data = (sub.get("items") or {}).get("data") or []
    base_pid = price_id_for("heavy")
    extra_pid = extra_price_id_for("heavy")
    base_item = extra_item = None
    for item in data:
        price = _item_price(item)
        iid = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if (base_pid and price.get("id") == base_pid) or \
                (not base_pid and price.get("unit_amount") == HEAVY_BASE_CENTS):
            base_item = iid
        elif (extra_pid and price.get("id") == extra_pid) or \
                (not extra_pid and price.get("unit_amount") == HEAVY_EXTRA_SHOP_CENTS):
            extra_item = iid
    if not base_item:
        raise ValueError("subscription has no Books Heavy base item")
    extra = heavy_extra_shops(shop_count)
    items = [{"id": base_item, "quantity": 1}]
    if extra > 0 and extra_item:
        items.append({"id": extra_item, "quantity": extra})
    elif extra > 0:
        items.append(_heavy_extra_item(shop_count))
    elif extra_item:
        items.append({"id": extra_item, "deleted": True})
    return stripe.Subscription.modify(
        subscription_id, items=items, proration_behavior="create_prorations")


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


# ---------------- the first-month welcome coupon (card trial door) --------
# Owner ruling 2026-10-31: the card door's conversion carries 50% off
# the FIRST paid month (the whole month, never prorated; month two at
# list). Implemented with Stripe's own coupon mechanics — a percent_off
# coupon with duration "once", which Stripe applies to exactly the
# first invoice — never hand-rolled price math. The id is fixed so
# every environment converges on one coupon.

FIRST_MONTH_COUPON_ID = "books-first-month-50"
FIRST_MONTH_COUPON_PERCENT = 50


def ensure_first_month_coupon() -> str:
    """Create-or-retrieve the first-month coupon; returns its id."""
    stripe = _stripe()
    try:
        coupon = stripe.Coupon.retrieve(FIRST_MONTH_COUPON_ID)
        return coupon.id
    except Exception as e:
        if type(e).__name__ != "InvalidRequestError" and "No such coupon" not in str(e):
            raise
    coupon = stripe.Coupon.create(
        id=FIRST_MONTH_COUPON_ID,
        percent_off=FIRST_MONTH_COUPON_PERCENT,
        duration="once",
        name="Welcome offer — 50% off the first month",
    )
    return coupon.id


def create_checkout_session(*, tenant_id: str, plan: str, user_email: str,
                            customer_id: str, origin: str, coupon_id: str = "",
                            shop_count=None):
    """A subscription-mode Checkout Session for the tenant. Metadata
    rides on BOTH the session and the subscription it creates, so every
    later subscription/invoice event can find its tenant. A coupon_id
    (the first-month welcome coupon, card-door conversions only) rides
    as a session discount — Stripe applies it to the first invoice.
    For Heavy, ``shop_count`` is the account's owned-shop count at
    checkout: it sizes the extra-shop line item and is stamped into
    the metadata as ``shops``."""
    stripe = _stripe()
    meta = {"kind": "books_subscription", "tenant_id": tenant_id, "plan": plan}
    if plan == "heavy":
        meta["shops"] = str(max(1, int(shop_count or 1)))
    kwargs = {
        "mode": "subscription",
        "line_items": line_items(plan, shop_count),
        "metadata": meta,
        "subscription_data": {"metadata": meta},
        "success_url": f"{origin}/settings?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{origin}/settings?billing=cancelled",
    }
    if coupon_id:
        kwargs["discounts"] = [{"coupon": coupon_id}]
    if customer_id:
        kwargs["customer"] = customer_id
    elif user_email:
        kwargs["customer_email"] = user_email
    return stripe.checkout.Session.create(**kwargs)


def create_card_setup_session(*, tenant_id: str, user_email: str,
                              customer_id: str, origin: str):
    """A setup-mode Checkout Session that puts a card on file for the
    card trial door (no charge). The webhook records the card's
    presence on the tenant; the auto-conversion sweep charges it
    off-session at trial end."""
    stripe = _stripe()
    meta = {"kind": "books_card_setup", "tenant_id": tenant_id}
    kwargs = {
        "mode": "setup",
        "payment_method_types": ["card"],
        "metadata": meta,
        "setup_intent_data": {"metadata": meta},
        "success_url": f"{origin}/settings?billing=card_saved",
        "cancel_url": f"{origin}/settings?billing=cancelled",
    }
    if customer_id:
        kwargs["customer"] = customer_id
    elif user_email:
        kwargs["customer_email"] = user_email
    return stripe.checkout.Session.create(**kwargs)


def setup_intent_payment_method(setup_intent_id: str) -> str:
    """The payment-method id a completed SetupIntent captured ("" when
    unavailable — callers treat absence as 'no card on file')."""
    if not setup_intent_id:
        return ""
    stripe = _stripe()
    si = stripe.SetupIntent.retrieve(setup_intent_id)
    return getattr(si, "payment_method", None) or (si.get("payment_method") if isinstance(si, dict) else "") or ""


def create_subscription_off_session(*, customer_id: str, plan: str,
                                    coupon_id: str, tenant_id: str,
                                    payment_method_id: str = "",
                                    shop_count=None):
    """Door 2's auto-conversion: create the subscription against the
    card on file, first month discounted by the welcome coupon.
    error_if_incomplete means a card that cannot be charged fails
    loudly in Stripe instead of silently creating a free ride; the
    webhook still drives all tenant billing state from the events
    this produces."""
    stripe = _stripe()
    meta = {"kind": "books_subscription", "tenant_id": tenant_id, "plan": plan,
            "source": "trial_auto_convert"}
    if plan == "heavy":
        meta["shops"] = str(max(1, int(shop_count or 1)))
    kwargs = {
        "customer": customer_id,
        "items": line_items(plan, shop_count),
        "metadata": meta,
        "off_session": True,
        "payment_behavior": "error_if_incomplete",
    }
    if coupon_id:
        kwargs["discounts"] = [{"coupon": coupon_id}]
    if payment_method_id:
        kwargs["default_payment_method"] = payment_method_id
    return stripe.Subscription.create(**kwargs)


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
