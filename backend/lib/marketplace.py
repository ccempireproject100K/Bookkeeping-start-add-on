"""App Marketplace catalog (v1).

A static catalog of the integrations the bookkeeping product knows
about, with each listing's status RESOLVED AT REQUEST TIME from the
same environment gates the underlying feature code uses — never
hardcoded, never stored. Flip a key in the server environment and the
listing flips with it, because both read the same env vars:

* Stripe card payments (routers/portal.py): STRIPE_SECRET_KEY
* Plaid bank feeds (routers/bankfeed.py): PLAID_CLIENT_ID + PLAID_SECRET
* TaxBandits 1099 (routers/tax.py): TAXBANDITS_CLIENT_ID,
  TAXBANDITS_CLIENT_SECRET, TAXBANDITS_USER_TOKEN, TAXBANDITS_BUSINESS_ID
* Receipt AI (routers/receipts.py): OPENAI_API_KEY
* Email/SMTP (lib/mailer.py): SMTP_HOST

Built-in features that genuinely work today with no key (CSV
import/export + the QuickBooks-import cleanup, and the Momo sync link)
are "live". Providers with NO integration code anywhere (Wisetack,
Shopify) are honestly "coming_soon".

Status values: "live" | "needs_key" | "coming_soon".
"""
import os

STATUS_LABELS = {
    "live": "Live",
    "needs_key": "Needs setup",
    "coming_soon": "Coming soon",
}


def _env(*names: str) -> bool:
    """True iff every named env var is set and non-empty."""
    return all((os.environ.get(n) or "").strip() for n in names)


def _stripe_status() -> str:
    return "live" if _env("STRIPE_SECRET_KEY") else "needs_key"


def _plaid_status() -> str:
    return "live" if _env("PLAID_CLIENT_ID", "PLAID_SECRET") else "needs_key"


def _taxbandits_status() -> str:
    return "live" if _env(
        "TAXBANDITS_CLIENT_ID", "TAXBANDITS_CLIENT_SECRET",
        "TAXBANDITS_USER_TOKEN", "TAXBANDITS_BUSINESS_ID") else "needs_key"


def _receipt_ai_status() -> str:
    return "live" if _env("OPENAI_API_KEY") else "needs_key"


def _email_status() -> str:
    return "live" if _env("SMTP_HOST") else "needs_key"


def _live() -> str:
    return "live"


def _coming_soon() -> str:
    return "coming_soon"


# The catalog. `_resolve` is an internal callable, stripped from the
# public view; everything else is display/connection metadata.
_LISTINGS = [
    {
        "slug": "stripe",
        "name": "Stripe",
        "provider": "Stripe",
        "category": "Payments",
        "tagline": "Card payments for invoices and the client portal",
        "description": (
            "Customers pay invoices by card through Stripe Checkout from "
            "the client portal. Payments are fulfilled by a "
            "signature-verified webhook and posted to the ledger exactly "
            "once. Until STRIPE_SECRET_KEY is set, checkout returns a "
            "graceful 'not configured' response instead of a dead button."),
        "cost_text": "Stripe's standard transaction fees apply, paid to Stripe.",
        "connect_needs": ["STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY",
                          "STRIPE_WEBHOOK_SECRET"],
        "setup_doc": "STRIPE_SETUP.md",
        "_resolve": _stripe_status,
    },
    {
        "slug": "plaid",
        "name": "Plaid Bank Feeds",
        "provider": "Plaid",
        "category": "Banking",
        "tagline": "Connect a bank once — transactions flow in automatically",
        "description": (
            "Live bank feeds via Plaid. Synced transactions land in the "
            "same staging area as CSV imports, so categorising, matching "
            "and reconciling work exactly as before. Access tokens are "
            "encrypted at rest and never returned by the API."),
        "cost_text": "Plaid pricing applies, paid to Plaid.",
        "connect_needs": ["PLAID_CLIENT_ID", "PLAID_SECRET",
                          "TOKEN_ENCRYPTION_KEY"],
        "setup_doc": "PLAID_SETUP.md",
        "_resolve": _plaid_status,
    },
    {
        "slug": "taxbandits",
        "name": "TaxBandits 1099",
        "provider": "TaxBandits",
        "category": "Tax",
        "tagline": "1099-NEC drafts and preparation for contractors",
        "description": (
            "Prepare 1099-NEC drafts in TaxBandits from your contractor "
            "payment data. Year summaries, W-9 tracking and the 1099 "
            "preview work without any keys; creating a draft in "
            "TaxBandits requires the TAXBANDITS_* keys. Drafts are "
            "reviewed — and transmitted — inside TaxBandits; this "
            "integration prepares drafts only and does not file with "
            "the IRS itself."),
        "cost_text": "TaxBandits per-form pricing applies, paid to TaxBandits.",
        "connect_needs": ["TAXBANDITS_CLIENT_ID", "TAXBANDITS_CLIENT_SECRET",
                          "TAXBANDITS_USER_TOKEN", "TAXBANDITS_BUSINESS_ID"],
        "setup_doc": "TAXBANDITS_SETUP.md",
        "_resolve": _taxbandits_status,
    },
    {
        "slug": "wisetack",
        "name": "Wisetack Financing",
        "provider": "Wisetack",
        "category": "Financing",
        "tagline": "Offer customers financing at checkout",
        "description": (
            "Customer financing at checkout for larger jobs, "
            "provider-abstracted like the rest of the platform: when "
            "this lands, each shop connects its own Wisetack merchant "
            "account. No integration code exists yet — this listing is "
            "a placeholder on the roadmap, not a working connection."),
        "cost_text": "To be announced when the integration ships.",
        "connect_needs": ["Your own Wisetack merchant account (when available)"],
        "setup_doc": None,
        "_resolve": _coming_soon,
    },
    {
        "slug": "receipt_ai",
        "name": "Receipt AI Scanning",
        "provider": "OpenAI",
        "category": "Receipts & Documents",
        "tagline": "Snap a receipt — AI extracts vendor, date and total",
        "description": (
            "Receipt photos are read by a vision model that extracts "
            "the vendor, date, total and a suggested expense category in "
            "your trade's language, ready to review and post. Uploads "
            "and manual entry work without a key; the AI scan step "
            "needs OPENAI_API_KEY."),
        "cost_text": "OpenAI API usage pricing applies, paid to OpenAI.",
        "connect_needs": ["OPENAI_API_KEY"],
        "setup_doc": None,
        "_resolve": _receipt_ai_status,
    },
    {
        "slug": "email_delivery",
        "name": "Email Delivery (SMTP)",
        "provider": "SMTP",
        "category": "Receipts & Documents",
        "tagline": "Deliver invoices, portal links and receipts by email",
        "description": (
            "Outbound email over your own SMTP server: client-portal "
            "invoice links, invoice and receipt delivery, reminders and "
            "security alerts. Without SMTP_HOST configured, sends are a "
            "graceful no-op and links stay queued for a later retry "
            "instead of failing."),
        "cost_text": "Your SMTP provider's pricing applies, if any.",
        "connect_needs": ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"],
        "setup_doc": None,
        "_resolve": _email_status,
    },
    {
        "slug": "quickbooks_csv_bridge",
        "name": "QuickBooks CSV Bridge",
        "provider": "Built-in",
        "category": "Data & Migration",
        "tagline": "CSV import/export and QuickBooks-import cleanup",
        "description": (
            "Built in, no key needed: import bank transactions from "
            "CSV, export a CPA package (CSV ZIP), and run the cleanup "
            "tools that fix up data imported from QuickBooks. This is "
            "the migration bridge between QuickBooks and your books "
            "here — file-based by design, no live sync."),
        "cost_text": "Included — no extra cost.",
        "connect_needs": [],
        "setup_doc": None,
        "_resolve": _live,
    },
    {
        "slug": "momo_sync",
        "name": "Momo Sync",
        "provider": "Momo",
        "category": "Scheduling",
        "tagline": "Momo pushes customers, invoices and payments into your books",
        "description": (
            "The live sync link from Momo — the office and scheduling "
            "platform for your shop — into these books. Momo pushes "
            "customers, invoices and payments with idempotency keys so "
            "retries never double-post. Connect it with a dedicated, "
            "revocable integration credential (scoped to this workspace "
            "only) instead of a pasted user login."),
        "cost_text": "Included with Momo.",
        "connect_needs": [
            "An integration credential issued in Integrations settings"],
        "setup_doc": None,
        "_resolve": _live,
    },
    {
        "slug": "shopify",
        "name": "Shopify",
        "provider": "Shopify",
        "category": "E-commerce",
        "tagline": "Sync Shopify sales into your books",
        "description": (
            "Planned: pull Shopify orders and payouts into the ledger "
            "automatically. No integration code exists yet — this "
            "listing is a placeholder on the roadmap, not a working "
            "connection."),
        "cost_text": "To be announced when the integration ships.",
        "connect_needs": ["A Shopify store (when available)"],
        "setup_doc": None,
        "_resolve": _coming_soon,
    },
]

_BY_SLUG = {item["slug"]: item for item in _LISTINGS}


def _public(item: dict) -> dict:
    status = item["_resolve"]()
    out = {k: v for k, v in item.items() if not k.startswith("_")}
    out["connect_needs"] = list(item.get("connect_needs") or [])
    out["status"] = status
    out["status_label"] = STATUS_LABELS[status]
    return out


def list_listings() -> list:
    """All listings with status resolved from the CURRENT environment."""
    return [_public(item) for item in _LISTINGS]


def get_listing(slug: str):
    """One listing with resolved status, or None for an unknown slug."""
    item = _BY_SLUG.get(slug or "")
    return _public(item) if item else None


def list_categories(listings=None) -> list:
    """[{name, count}] in catalog order for the browse UI."""
    listings = list_listings() if listings is None else listings
    counts: dict = {}
    order: list = []
    for item in listings:
        cat = item["category"]
        if cat not in counts:
            counts[cat] = 0
            order.append(cat)
        counts[cat] += 1
    return [{"name": name, "count": counts[name]} for name in order]
