"""Stripe charging for the DataClean product family.

DataClean's prices are the owner's stamped ladder (2026-11-04:
DataClean $12.99/mo, Security $18.99/mo, bundle $27.99/mo — see
lib/dataclean_api.py's product ladder, the single source the prices
are read from). This module is the charging path the DataClean shell
never had: products/prices provisioning in Stripe, a subscription
Checkout for a workspace, and a signature-verified webhook that is
the ONLY writer of the tenant's ``dataclean_billing`` record — the
record the entitlement checks (key minting, the external API) read.
The checkout success redirect changes nothing; Stripe's servers
confirming money is the sole source of truth, exactly as in the
books billing (lib/billing.py), whose conventions this follows.

The record is deliberately separate from the books ``billing``
subdoc: a workspace's bookkeeping plan and its DataClean
subscription are different products with different lifecycles, and
neither flow may move the other's state. The Stripe ACCOUNT is
shared, so every event is claimed under its own source and only
objects stamped with this flow's metadata kind (or resolvable
through this flow's ids on the tenant) are processed.

Key-gating mirrors lib/billing.py: until STRIPE_SECRET_KEY is set,
every entry point refuses honestly (503), and the Stripe SDK is
imported LAZILY, only after the gate. Keys are read from the
environment at call time and never logged.

Environment (production):
  STRIPE_SECRET_KEY               platform secret (sk_/rk_ test… in
                                  test mode, sk_/rk_ live… live) —
                                  required
  STRIPE_DATACLEAN_WEBHOOK_SECRET signing secret for THIS flow's
                                  webhook endpoint (falls back to
                                  STRIPE_WEBHOOK_SECRET)
  STRIPE_PUBLISHABLE_KEY          publishable key, handed to the
                                  checkout page for display/config
  STRIPE_PRICE_DATACLEAN / STRIPE_PRICE_SECURITY /
  STRIPE_PRICE_BUNDLE             optional dashboard-managed Price
                                  ids; when unset, the ids recorded
                                  by ensure_products() are used, and
                                  failing that, inline price_data
  SECURITY_PRODUCT_BUILT=1        set by the security wiring
                                  workstream when Security ships;
                                  flips Security + the bundle sellable
  DATACLEAN_BILLING_ENFORCED=1    strict launch mode: a workspace
                                  with NO billing record is not
                                  entitled (default off — workspaces
                                  predate charging and are
                                  grandfathered until the owner
                                  turns charging on)
"""
import os

from db import db
from lib import dataclean_api as dca
from lib.billing import iso_from_epoch, map_subscription_status

KIND = "dataclean_subscription"
SOURCE = "dataclean_billing"

# Deterministic Stripe ids: re-running ensure_products() retrieves
# instead of duplicating, in every environment, forever.
_STRIPE_IDS = {
    "dataclean": ("mae_dataclean", "mae_dataclean_monthly"),
    "security": ("mae_security", "mae_security_monthly"),
    "bundle": ("mae_bundle", "mae_bundle_monthly"),
}
_PRICE_ENVS = {
    "dataclean": "STRIPE_PRICE_DATACLEAN",
    "security": "STRIPE_PRICE_SECURITY",
    "bundle": "STRIPE_PRICE_BUNDLE",
}

# Subscription states that keep DataClean access. past_due keeps
# access while Stripe retries the card — the same dunning grace the
# books plan gets (lib/billing.py's status map + router semantics);
# canceled and incomplete (no money landed) do not.
ENTITLED_STATUSES = ("active", "past_due")
# A bundle subscription grants DataClean too — it contains it.
GRANTING_PRODUCTS = ("dataclean", "bundle")


class BillingNotConfigured(Exception):
    """Raised by entry points when the platform Stripe key is absent.
    The router turns it into the honest 503."""


class WebhookSignatureError(Exception):
    """The webhook signature did not verify. The router turns it into
    a 400; the delivery is never processed."""


# ---------------- config (env read at call time, portal pattern) ----------------

def secret() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def webhook_secret() -> str:
    return ((os.environ.get("STRIPE_DATACLEAN_WEBHOOK_SECRET") or "").strip()
            or (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip())


def publishable_key() -> str:
    return (os.environ.get("STRIPE_PUBLISHABLE_KEY") or "").strip()


def configured() -> bool:
    return bool(secret())


def billing_mode() -> str:
    """"live"/"test" label for the configured key — display +
    honesty only (charging is gated by configured(), never by this
    label). Delegates to lib/dataclean_api.stripe_key_mode, the
    single implementation, so a restricted live key (rk_live_…)
    reads "live" exactly like a full one (sk_live_…)."""
    return dca.billing_mode()


def enforcement_on() -> bool:
    """Strict launch mode (see the module docstring)."""
    raw = os.environ.get("DATACLEAN_BILLING_ENFORCED")
    return bool(raw and raw.strip().lower() in ("1", "true", "yes", "on"))


# ---------------- product catalog (prices read from the ladder) ----------------

def product_catalog() -> dict:
    """The purchasable catalog: the stamped ladder from
    lib/dataclean_api.py plus this flow's Stripe wiring per product.
    Keyed by product id: "dataclean" | "security" | "bundle"."""
    prods = {p["id"]: p for p in dca.products_config()}
    bundle = dca.bundle_config()[0]
    cat = {}
    for pid in ("dataclean", "security"):
        p = prods[pid]
        cat[pid] = {
            "id": pid, "name": p["name"],
            "amount_cents": p["monthly_price_cents"],
            "currency": "usd", "interval": "month",
            "sellable": p["sellable"], "status": p["status"],
            "stripe_product_id": _STRIPE_IDS[pid][0],
            "stripe_price_id": _STRIPE_IDS[pid][1],
            "price_env": _PRICE_ENVS[pid],
        }
    cat["bundle"] = {
        "id": "bundle", "name": bundle["name"],
        "amount_cents": bundle["monthly_price_cents"],
        "currency": "usd", "interval": "month",
        "sellable": bundle["sellable"], "status": bundle["status"],
        "parts_total_cents": bundle["parts_total_cents"],
        "savings_cents": bundle["savings_cents"],
        "stripe_product_id": _STRIPE_IDS["bundle"][0],
        "stripe_price_id": _STRIPE_IDS["bundle"][1],
        "price_env": _PRICE_ENVS["bundle"],
    }
    return cat


def catalog_public() -> list:
    """The catalog as the checkout page renders it (no env/Stripe
    internals beyond the deterministic ids' existence)."""
    out = []
    for c in product_catalog().values():
        row = {
            "id": c["id"], "name": c["name"],
            "amount_cents": c["amount_cents"],
            "display": f"${c['amount_cents'] / 100:,.2f}/mo",
            "sellable": c["sellable"], "status": c["status"],
        }
        if c["id"] == "bundle":
            row["parts_total_cents"] = c["parts_total_cents"]
            row["savings_cents"] = c["savings_cents"]
        out.append(row)
    return out


def sellable(product: str) -> bool:
    c = product_catalog().get(product)
    return bool(c and c["sellable"])


# ---------------- Stripe SDK (lazy — call ONLY after the gate) ----------------

def _stripe():
    import stripe
    stripe.api_key = secret()
    return stripe


def _retrieve_or_create(kind_obj, obj_id, create_kwargs):
    """Retrieve a Stripe object by its deterministic id, creating it
    with that id when absent — the ensure_first_month_coupon pattern
    from lib/billing.py, so provisioning is idempotent."""
    try:
        return kind_obj.retrieve(obj_id)
    except Exception as e:
        if type(e).__name__ != "InvalidRequestError" \
                and "No such" not in str(e):
            raise
    return kind_obj.create(id=obj_id, **create_kwargs)


async def ensure_products() -> dict:
    """Create-or-retrieve the three products + monthly prices in the
    configured Stripe account (test mode when the key is a test key)
    and record the mapping in `dataclean_stripe_products`, keyed by
    product id. Idempotent: deterministic ids + retrieve-first mean
    any number of runs converge on the same three objects. Returns
    the mapping. Raises BillingNotConfigured without a secret key."""
    if not configured():
        raise BillingNotConfigured(
            "STRIPE_SECRET_KEY is not set — cannot provision Stripe products")
    stripe = _stripe()
    out = {}
    for pid, c in product_catalog().items():
        prod = _retrieve_or_create(stripe.Product, c["stripe_product_id"], {
            "name": c["name"],
            "metadata": {"mae_product": pid, "kind": KIND},
        })
        price = _retrieve_or_create(stripe.Price, c["stripe_price_id"], {
            "product": prod.id,
            "currency": c["currency"],
            "unit_amount": c["amount_cents"],
            "recurring": {"interval": c["interval"]},
            "metadata": {"mae_product": pid, "kind": KIND},
        })
        row = {
            "product": pid,
            "stripe_product_id": prod.id,
            "stripe_price_id": price.id,
            "amount_cents": c["amount_cents"],
            "currency": c["currency"],
        }
        await db.dataclean_stripe_products.update_one(
            {"product": pid}, {"$set": row}, upsert=True)
        out[pid] = row
    return out


async def price_id_for(product: str) -> str:
    """The Stripe Price id to charge for a product: the env-configured
    dashboard id wins, then the id ensure_products() recorded, else
    "" (the session then carries inline price_data)."""
    c = product_catalog()[product]
    env = (os.environ.get(c["price_env"]) or "").strip()
    if env:
        return env
    coll = getattr(db, "dataclean_stripe_products", None)
    if coll is not None:
        row = await coll.find_one({"product": product})
        if row and row.get("stripe_price_id"):
            return row["stripe_price_id"]
    return ""


async def product_for_price_id(price_id: str) -> str:
    """Reverse-map a Stripe Price id to one of our products (env ids,
    deterministic ids, then the recorded catalog). "" when not ours."""
    if not price_id:
        return ""
    for pid, c in product_catalog().items():
        if price_id in ((os.environ.get(c["price_env"]) or "").strip(),
                        c["stripe_price_id"]):
            return pid
    coll = getattr(db, "dataclean_stripe_products", None)
    if coll is not None:
        row = await coll.find_one({"stripe_price_id": price_id})
        if row:
            return row.get("product") or ""
    return ""


async def _line_item(product: str) -> dict:
    c = product_catalog()[product]
    pid = await price_id_for(product)
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


async def create_checkout_session(*, tenant_id: str, product: str,
                                  user_email: str, customer_id: str,
                                  origin: str, ui_mode: str = "hosted"):
    """A subscription-mode Checkout Session for the workspace buying
    `product`. Metadata rides on BOTH the session and the
    subscription it creates, so every later subscription/invoice
    event can find its tenant and product. Creates the session and
    NOTHING else — the tenant's record changes only in the webhook.

    ui_mode="hosted" (default) returns Stripe's hosted page URL.
    ui_mode="elements" is the embedded checkout (the survey's ADOPT
    pick, stripe/react-stripe-js): the session carries a
    client_secret the React page renders in place, and Stripe sends
    the customer back to return_url when the flow completes."""
    stripe = _stripe()
    meta = {"kind": KIND, "tenant_id": tenant_id, "product": product}
    kwargs = {
        "mode": "subscription",
        "line_items": [await _line_item(product)],
        "metadata": meta,
        "subscription_data": {"metadata": meta},
    }
    if ui_mode == "elements":
        kwargs["ui_mode"] = "elements"
        kwargs["return_url"] = (
            f"{origin}/dataclean-checkout?checkout=success"
            "&session_id={CHECKOUT_SESSION_ID}")
    else:
        kwargs["success_url"] = (
            f"{origin}/dataclean-checkout?checkout=success"
            "&session_id={CHECKOUT_SESSION_ID}")
        kwargs["cancel_url"] = f"{origin}/dataclean-checkout?checkout=cancelled"
    if customer_id:
        kwargs["customer"] = customer_id
    elif user_email:
        kwargs["customer_email"] = user_email
    return stripe.checkout.Session.create(**kwargs)


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verify + parse a webhook delivery for THIS flow's endpoint
    (its own signing secret — see the module docstring). Raises
    ValueError on a malformed payload and WebhookSignatureError on a
    bad signature."""
    stripe = _stripe()
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret())
    except ValueError:
        raise
    except Exception as e:
        if type(e).__name__ == "SignatureVerificationError":
            raise WebhookSignatureError(str(e)) from e
        raise


# ---------------- entitlement ----------------

def record_entitles(rec) -> bool:
    """Does a tenant's dataclean_billing record grant DataClean
    access right now? Only a granting product (DataClean itself, or
    the bundle that contains it) in a live state entitles."""
    if not rec:
        return False
    return (rec.get("product") in GRANTING_PRODUCTS
            and rec.get("status") in ENTITLED_STATUSES)


async def get_billing_record(tenant_id: str):
    """The tenant's dataclean_billing record, or None. Guarded for
    db handles without a tenants collection (unit-test fakes)."""
    tenants = getattr(db, "tenants", None)
    if tenants is None:
        return None
    tenant = await tenants.find_one({"id": tenant_id})
    return (tenant or {}).get("dataclean_billing")


def entitlement_from_record(rec) -> bool:
    """The entitlement rule, pure: a workspace WITH a billing record
    is entitled exactly when the record entitles (subscription state
    is the whole truth); a workspace with NO record predates
    charging and stays entitled, unless the owner turns strict mode
    on (DATACLEAN_BILLING_ENFORCED=1), after which no record means
    no access."""
    if rec:
        return record_entitles(rec)
    return not enforcement_on()


async def entitled_with_db(db_handle, tenant_id: str) -> bool:
    """entitlement_from_record resolved through a CALLER-SUPPLIED db
    handle. The gates live in modules with their own `db` binding
    (lib/dataclean_api.py, routers/dataclean_admin.py); resolving
    through the caller's handle keeps one database view per request
    path — and keeps this check off any other module's connection.
    A handle without a tenants collection (unit-test fakes) reads as
    "no record"."""
    tenants = getattr(db_handle, "tenants", None)
    if tenants is None:
        return entitlement_from_record(None)
    tenant = await tenants.find_one({"id": tenant_id})
    return entitlement_from_record((tenant or {}).get("dataclean_billing"))


async def tenant_dataclean_entitled(tenant_id: str) -> bool:
    """The entitlement the gates enforce (key minting in
    routers/dataclean_admin.py; every external API call via
    lib/dataclean_api.py's authenticate_key) — resolved through this
    module's own db handle. The gates themselves call
    entitled_with_db with their local handle; the rule is identical."""
    return await entitled_with_db(db, tenant_id)
