"""Stripe Connect transfers for referral payouts (rail: stripe).

Referral payouts ride the SAME connected accounts members onboard
through the invoice-payments Connect flow: the tenant doc carries
`stripe_connect = {account_id, charges_enabled, payouts_enabled,
details_submitted}` (shape defined by that change set's
`lib/stripe_connect.py` — deliberately NOT re-declared here; this
module only reads the state and moves money to it). A payout is a
platform -> connected-account Transfer; the member's own Stripe
pays out to their bank on its normal schedule.

Key-gating follows the house pattern: until STRIPE_SECRET_KEY is set,
entry points refuse honestly (503) and the SDK is imported lazily,
only after the gate. This file is named referral_payouts (not
stripe_connect) so it coexists with the sibling invoice-payments
set's lib/stripe_connect.py when the stacks are assembled.
"""
import os


class ConnectNotConfigured(Exception):
    """Raised when the platform Stripe key is absent. The router
    turns it into the honest 503."""


def platform_secret() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def configured() -> bool:
    return bool(platform_secret())


def _stripe():
    if not configured():
        raise ConnectNotConfigured(
            "Referral payouts are not configured. Set STRIPE_SECRET_KEY "
            "to enable them (see REFERRAL_HOST_BUILD.md).")
    import stripe
    stripe.api_key = platform_secret()
    return stripe


async def connect_destination(db, person: dict):
    """The person's payable Connect account, or (None, reason).

    Looks through the person's linked books tenant: the Connect state
    lives on the tenant doc (invoice-payments flow). A person with no
    books tenant, no account, or payouts not yet enabled has no
    destination — the batch skips them and the operator pays them
    through the manual or Melio paths instead.
    """
    acct = next((a for a in person.get("product_accounts", [])
                 if a.get("product") == "books"), None)
    if acct is None:
        return None, "no books account linked"
    tenant_id = acct.get("tenant_id") or acct.get("account_id")
    tenant = await db.tenants.find_one({"id": tenant_id})
    if tenant is None:
        return None, "books workspace not found"
    conn = tenant.get("stripe_connect") or {}
    if not conn.get("account_id"):
        return None, "no Stripe account connected"
    if not conn.get("payouts_enabled"):
        return None, "Stripe payouts not enabled yet"
    return conn["account_id"], None


def create_transfer(*, destination: str, amount_cents: int,
                    currency: str = "usd", idempotency_key: str,
                    description: str = "") -> dict:
    """One platform -> connected-account transfer. The Stripe
    idempotency key makes a retried call after a crash return the
    original transfer instead of paying twice."""
    stripe = _stripe()
    transfer = stripe.Transfer.create(
        amount=int(amount_cents),
        currency=currency or "usd",
        destination=destination,
        description=description or "Empire referral payout",
        metadata={"kind": "referral_payout"},
        idempotency_key=idempotency_key,
    )
    if isinstance(transfer, dict):
        return transfer
    return {"id": transfer.id, "amount": getattr(transfer, "amount", None)}
