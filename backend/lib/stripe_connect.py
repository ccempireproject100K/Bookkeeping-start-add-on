"""Stripe Connect for invoice payments — the member's own Stripe.

Design: Trade Circle docs/invoice-payments.md. An invoice in a tenant's
books gets a pay link; the customer pays on a Checkout Session created
as a DIRECT CHARGE on the tenant's connected (Express) account, so the
tenant is merchant of record, funds land in the tenant's Stripe, and
this platform never holds them. Our take is the Checkout
`application_fee_amount`, computed here from named env config.

Key-gating follows the portal flow (routers/portal.py): until
STRIPE_SECRET_KEY is set, every entry point refuses honestly (503) —
and the Stripe SDK is imported LAZILY, only after the gate, so an
unconfigured deployment never trips an import error.

Fee schedule (defaults; every figure env-overridable):
  card  — Stripe 2.9% + 30c (INVOICE_CARD_STRIPE_BPS /
          INVOICE_CARD_STRIPE_FIXED_CENTS) + our 0.2%
          (INVOICE_CARD_APP_FEE_BPS). Member's total 3.1% + 30c,
          vs Stripe Invoicing's +0.4% and PayPal's 3.49% + 49c.
  ach   — the member's ALL-IN price is 1.0% capped at $10
          (INVOICE_ACH_TOTAL_BPS / INVOICE_ACH_TOTAL_CAP_CENTS);
          Stripe's published ACH schedule is 0.8% capped at $5
          (INVOICE_ACH_STRIPE_BPS / INVOICE_ACH_STRIPE_CAP_CENTS)
          and our application fee is the spread.

Fees are borne by the member (deducted from their proceeds); the
customer always pays exactly the invoice amount.
"""
import os
from decimal import Decimal, ROUND_HALF_UP


class ConnectNotConfigured(Exception):
    """Raised by entry points when the platform Stripe key is absent.
    The router turns it into the honest 503."""


class WebhookSignatureError(Exception):
    """The webhook signature did not verify. The router turns it into
    a 400; the delivery is never processed."""


# ---------------- config (env read at call time, portal pattern) ----------------

def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def platform_secret() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def connect_webhook_secret() -> str:
    return (os.environ.get("STRIPE_CONNECT_WEBHOOK_SECRET") or "").strip()


def configured() -> bool:
    """Platform-side readiness: a Stripe secret key exists. (Per-tenant
    readiness — a connected account with charges enabled — is separate
    state on the tenant record.)"""
    return bool(platform_secret())


def fee_config() -> dict:
    return {
        "card_app_fee_bps": _env_int("INVOICE_CARD_APP_FEE_BPS", 20),
        "card_stripe_bps": _env_int("INVOICE_CARD_STRIPE_BPS", 290),
        "card_stripe_fixed_cents": _env_int("INVOICE_CARD_STRIPE_FIXED_CENTS", 30),
        "ach_total_bps": _env_int("INVOICE_ACH_TOTAL_BPS", 100),
        "ach_total_cap_cents": _env_int("INVOICE_ACH_TOTAL_CAP_CENTS", 1000),
        "ach_stripe_bps": _env_int("INVOICE_ACH_STRIPE_BPS", 80),
        "ach_stripe_cap_cents": _env_int("INVOICE_ACH_STRIPE_CAP_CENTS", 500),
    }


def _require_configured():
    if not configured():
        raise ConnectNotConfigured(
            "Invoice payments are not configured. Set STRIPE_SECRET_KEY "
            "to enable them (see STRIPE_CONNECT_SETUP.md).")


def _stripe():
    """Lazily import + configure the Stripe SDK. Call ONLY after the
    config gate has passed (portal flow's rule)."""
    _require_configured()
    import stripe
    stripe.api_key = platform_secret()
    return stripe


# ---------------- fee math (integer cents, ROUND_HALF_UP) ----------------

def _bps(amount_cents: int, bps: int) -> int:
    return int((Decimal(amount_cents) * Decimal(bps) / Decimal(10000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def fees_for(rail: str, amount_cents: int, cfg: dict = None) -> dict:
    """The fee split for one charge of `amount_cents` on `rail`.

    Returns the application fee (ours — exact, we set it on the
    charge), Stripe's fee as an ESTIMATE from the published schedule
    in config (on direct charges Stripe settles its fee inside the
    connected account; v1 does not read it back), and the member's
    total fee cost.
    """
    cfg = cfg or fee_config()
    amount_cents = int(amount_cents)
    if rail == "card":
        app_fee = _bps(amount_cents, cfg["card_app_fee_bps"])
        stripe_fee = _bps(amount_cents, cfg["card_stripe_bps"]) + cfg["card_stripe_fixed_cents"]
    elif rail == "ach":
        member_total = min(_bps(amount_cents, cfg["ach_total_bps"]),
                           cfg["ach_total_cap_cents"])
        stripe_fee = min(_bps(amount_cents, cfg["ach_stripe_bps"]),
                          cfg["ach_stripe_cap_cents"])
        app_fee = max(0, member_total - stripe_fee)
    else:
        raise ValueError(f"Unknown rail: {rail!r}")
    return {
        "application_fee_cents": app_fee,
        "stripe_fee_estimate_cents": stripe_fee,
        "member_total_fee_cents": app_fee + stripe_fee,
    }


# ---------------- Stripe wrappers (all assume the gate passed) ----------------

def create_account(tenant_id: str, email: str = ""):
    """Create the tenant's Express connected account."""
    stripe = _stripe()
    kwargs = {
        "type": "express",
        "country": "US",
        "capabilities": {
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
        "metadata": {"tenant_id": tenant_id, "product": "empire_books"},
    }
    if email:
        kwargs["email"] = email
    return stripe.Account.create(**kwargs)


def create_account_link(account_id: str, origin_url: str):
    stripe = _stripe()
    origin = origin_url.rstrip("/")
    return stripe.AccountLink.create(
        account=account_id,
        refresh_url=f"{origin}/settings/billing?connect=refresh",
        return_url=f"{origin}/settings/billing?connect=return",
        type="account_onboarding",
    )


def retrieve_account(account_id: str):
    stripe = _stripe()
    return stripe.Account.retrieve(account_id)


def account_state(account) -> dict:
    """The persistable Connect state from a Stripe account object."""
    return {
        "account_id": account["id"] if isinstance(account, dict) else account.id,
        "charges_enabled": bool(_get(account, "charges_enabled")),
        "payouts_enabled": bool(_get(account, "payouts_enabled")),
        "details_submitted": bool(_get(account, "details_submitted")),
    }


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def create_checkout_session(*, account_id: str, amount_cents: int, currency: str,
                            label: str, description: str, rail: str,
                            application_fee_cents: int, origin_url: str,
                            invoice_id: str, tenant_id: str):
    """A Checkout Session as a DIRECT CHARGE on the connected account:
    the tenant is merchant of record; our take rides along as the
    application fee on the payment intent."""
    stripe = _stripe()
    origin = origin_url.rstrip("/")
    payment_method_types = ["card"] if rail == "card" else ["us_bank_account"]
    return stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=payment_method_types,
        line_items=[{
            "price_data": {
                "currency": currency,
                "unit_amount": int(amount_cents),
                "product_data": {"name": label, "description": description},
            },
            "quantity": 1,
        }],
        payment_intent_data={
            "application_fee_amount": int(application_fee_cents),
            "metadata": {"kind": "connect_invoice", "tenant_id": tenant_id,
                         "invoice_id": invoice_id, "rail": rail},
        },
        metadata={"kind": "connect_invoice", "tenant_id": tenant_id,
                  "invoice_id": invoice_id, "rail": rail},
        success_url=f"{origin}/invoices/{invoice_id}?paid=1&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{origin}/invoices/{invoice_id}",
        stripe_account=account_id,
    )


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verify + parse a Connect webhook delivery. Raises
    WebhookSignatureError on a bad signature and ValueError on a bad
    payload (the portal flow's two 400 cases)."""
    secret = connect_webhook_secret()
    if not secret:
        raise ConnectNotConfigured(
            "Stripe Connect webhook is not configured. Set "
            "STRIPE_CONNECT_WEBHOOK_SECRET (see STRIPE_CONNECT_SETUP.md).")
    stripe = _stripe()
    try:
        return stripe.Webhook.construct_event(payload, sig_header, secret)
    except stripe.error.SignatureVerificationError as e:
        raise WebhookSignatureError(str(e)) from e
