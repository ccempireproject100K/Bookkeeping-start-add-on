"""DataClean external API foundation (Phase 3): keys, limits, metering.

The product shell around the framework-free engine in lib/dataclean/.
Everything here follows the house patterns set by lib/developer.py —
a long random secret shown exactly once at creation, only its SHA-256
hash stored, verification by constant-time digest compare, one generic
401 for every failure — but DataClean keys live in their OWN collection
(`dataclean_api_keys`) with their OWN prefix (`mdck_`) and scope
(`dataclean:use`). A books API key (lib/developer.py, `maek_`) cannot
call this surface and a DataClean key cannot call anything else: the
tenant ALWAYS comes from the key record, never from the request, and
every read/write a key triggers is tenant-scoped by construction.

Metering is a FOUNDATION, not billing: every authenticated external
call writes one row to `dataclean_api_usage` (tenant, key, endpoint,
rows processed, status, timestamp). This module never charges
anything or creates a Stripe object — charging lives in
lib/dataclean_billing.py (Stripe Checkout + webhook-driven
entitlement), which reads its prices from the config below. The
prices in pricing_config() are the owner's stamped ladder
(2026-11-04), not placeholders.
"""
import hashlib
import hmac
import os
import secrets as _secrets
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from db import db
from lib.base import Scoped, new_id, now_iso

# ------------------------------------------------------------------ config

KEY_PREFIX = "mdck_"
KEY_SCOPES = ["dataclean:use"]


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def rate_limit_defaults() -> dict:
    """Per-key call limits. Configurable via env; a key doc may carry
    its own overrides (set at issue time in the console)."""
    return {
        "per_minute": _env_int("DATACLEAN_API_RATE_PER_MINUTE", 60),
        "per_day": _env_int("DATACLEAN_API_RATE_PER_DAY", 5000),
        "monthly_row_quota": _env_int("DATACLEAN_API_MONTHLY_ROW_QUOTA",
                                      1000000),
    }


def playground_caps() -> dict:
    """Hard caps for the public try-a-file playground. Deliberately
    conservative: the demo exists to show the engine's work on a small
    file, not to process real imports. NOTHING from the playground is
    persisted (see routers/dataclean_api.py)."""
    return {
        "max_file_bytes": _env_int("DATACLEAN_PLAYGROUND_MAX_BYTES", 65536),
        "max_rows": _env_int("DATACLEAN_PLAYGROUND_MAX_ROWS", 200),
        "max_columns": _env_int("DATACLEAN_PLAYGROUND_MAX_COLUMNS", 24),
        "requests_per_window": _env_int("DATACLEAN_PLAYGROUND_RATE", 10),
        "window_seconds": 600,
    }


# ------------------------------------------------------- product ladder
# OWNER-STAMPED PRICES (2026-11-04, final — they supersede every
# earlier placeholder, including the 2026-10-31 bundle figure of
# $18.99, which was a placeholder the owner replaced): DataClean
# $12.99/mo standalone, Security $18.99/mo standalone, the bundle
# $27.99/mo (parts total $31.98 — the bundle saves $3.99, and the
# storefront surfaces that saving). The env overrides below are an
# ops escape hatch only; the defaults ARE the stamped ladder.

DATACLEAN_PRICE_CENTS = 1299
SECURITY_PRICE_CENTS = 1899
BUNDLE_PRICE_CENTS = 2799


def security_built() -> bool:
    """THE switch for the security product's built state.

    The security engine is built outside this repo (its own
    workstream); until that wiring ships, the product is honestly
    coming_soon and NOT purchasable — alone or inside the bundle.
    The wiring workstream flips the product live by setting
    SECURITY_PRODUCT_BUILT=1 in the environment. Every availability /
    sellable flag in this config derives from this single read, so
    pricing, the storefront and the checkout gate can never disagree
    about whether Security exists yet.
    """
    return _env_bool("SECURITY_PRODUCT_BUILT", False)


def stripe_key_mode(key: str) -> str:
    """"live" when `key` is a live Stripe secret — full (sk_live_…)
    or restricted (rk_live_…) alike — else "test" (sk_test_…,
    rk_test_…, empty, or anything unrecognized). Stripe issues both
    secret shapes; the owner's live secret is a restricted key, so
    an sk_-only check would mislabel a live deployment as test."""
    k = (key or "").strip()
    return "live" if k.startswith(("sk_live_", "rk_live_")) else "test"


def billing_mode() -> str:
    """"live" when the configured Stripe secret is a live key, else
    "test". Display + honesty only — lib/dataclean_billing.py does
    the actual key-gating before any Stripe call."""
    return stripe_key_mode(os.environ.get("STRIPE_SECRET_KEY") or "")


def products_config() -> list:
    """The stamped product ladder, as CONFIG values. `sellable` is
    what the checkout endpoint enforces: DataClean sells today;
    Security sells only once security_built() flips — until then it
    is flagged coming_soon and never shown as available, and no
    security feature is named anywhere in this config (the product
    is offered only as monitoring / protection / automated
    remediation when it ships — never as guaranteed removal)."""
    built = security_built()
    return [
        {"id": "dataclean", "name": "Empire Digital Data Clean",
         "monthly_price_cents": _env_int("DATACLEAN_PRICE_DATACLEAN_CENTS",
                                         DATACLEAN_PRICE_CENTS),
         "status": "live", "built": True, "available": True,
         "sellable": True, "placeholder": False},
        {"id": "security", "name": "Empire Security Suite",
         "monthly_price_cents": _env_int("DATACLEAN_PRICE_SECURITY_CENTS",
                                         SECURITY_PRICE_CENTS),
         "status": "live" if built else "coming_soon",
         "built": built, "available": built, "sellable": built,
         "placeholder": False},
    ]


def bundle_config() -> list:
    """The DataClean + Security bundle at the stamped $27.99/mo.

    Sellable ONLY when both halves are available: DataClean is live,
    but while the security half is unbuilt the bundle is honestly
    coming_soon as a whole and the checkout endpoint refuses it —
    nobody can pay for a product half of which doesn't exist yet.
    The saving against the parts total is computed, never typed in,
    so it cannot drift from the ladder.
    """
    halves = {p["id"]: p for p in products_config()}
    dc, sec = halves["dataclean"], halves["security"]
    price = _env_int("DATACLEAN_PRICE_BUNDLE_CENTS", BUNDLE_PRICE_CENTS)
    parts = dc["monthly_price_cents"] + sec["monthly_price_cents"]
    sellable = bool(dc["available"] and sec["available"])
    return [{
        "id": "dataclean-security",
        "name": "Empire Digital Data Clean + Empire Security Suite",
        "monthly_price_cents": price,
        "parts_total_cents": parts,
        "savings_cents": parts - price,
        "placeholder": False,
        "status": "live" if sellable else "coming_soon",
        "sellable": sellable,
        "components": [
            {"id": "dataclean", "name": "Empire Digital Data Clean",
             "status": dc["status"], "built": True, "available": True},
            {"id": "security", "name": "Empire Security Suite",
             "status": sec["status"], "built": sec["built"],
             "available": sec["available"]},
        ],
    }]


def pricing_config() -> dict:
    """Storefront pricing: the owner's stamped ladder (2026-11-04).

    Prices are real (`placeholder: False`) — the placeholder era
    ended when the owner stamped the ladder. What can actually be
    CHARGED is narrower than what is priced: lib/dataclean_billing.py
    enforces each product's `sellable` flag at checkout, so Security
    and the bundle stay unpurchasable until Security is built.
    """
    return {
        "currency": "USD",
        "placeholder": False,
        "billing_mode": billing_mode(),
        "products": products_config(),
        "bundles": bundle_config(),
        "rate_limits": rate_limit_defaults(),
        "playground": playground_caps(),
    }


# -------------------------------------------------------------------- keys

def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_key() -> str:
    """Full key string: ``mdck_`` + 43 urlsafe chars (32 random bytes)."""
    return KEY_PREFIX + _secrets.token_urlsafe(32)


def key_stub(token: str) -> str:
    return f"{token[:13]}...{token[-4:]}"


def new_key_doc(*, tenant_id: str, created_by: str, label: str,
                rate_limit_per_minute=None, rate_limit_per_day=None,
                monthly_row_quota=None, date_policy=None, country=None):
    """Build the stored document for a fresh DataClean key.

    Returns (doc, full_key). The full key exists only in the create
    response; the doc carries its hash + display stub, the key's limit
    overrides (None = use the configured defaults), its tenant locale
    defaults (date_policy / country — None = engine defaults), and
    denormalized usage counters maintained by record_usage.
    """
    full = mint_key()
    doc = {
        "id": new_id(),
        "tenant_id": tenant_id,
        "label": label or "DataClean API key",
        "key_hash": hash_key(full),
        "key_stub": key_stub(full),
        "scopes": list(KEY_SCOPES),
        "created_at": now_iso(),
        "created_by": created_by,
        "revoked_at": None,
        "last_used_at": None,
        "rate_limit_per_minute": rate_limit_per_minute,
        "rate_limit_per_day": rate_limit_per_day,
        "monthly_row_quota": monthly_row_quota,
        "date_policy": date_policy,
        "country": country,
        "calls_total": 0,
        "rows_total": 0,
    }
    return doc, full


# Date policies the engine understands (lib/dataclean/normalize.py).
DATE_POLICIES = ("us_month_first", "day_first", "iso_only")


def effective_defaults(actor: dict) -> dict:
    """A key's tenant locale defaults: per-request values win over the
    key's stored defaults, which win over the engine defaults. Called
    by the routers to resolve (date_policy, country) for one call."""
    doc = actor.get("_key_doc") or {}
    policy = doc.get("date_policy")
    if policy not in DATE_POLICIES:
        policy = "us_month_first"
    country = doc.get("country")
    country = str(country).strip().upper() if country else None
    return {"date_policy": policy, "country": country}


def effective_limits(doc: dict) -> dict:
    """A key's limits: its own overrides where set, else the defaults."""
    d = rate_limit_defaults()
    return {
        "per_minute": doc.get("rate_limit_per_minute") or d["per_minute"],
        "per_day": doc.get("rate_limit_per_day") or d["per_day"],
        "monthly_row_quota": doc.get("monthly_row_quota")
        or d["monthly_row_quota"],
    }


def public_key_view(doc: dict) -> dict:
    """Metadata-only view — never the key, never its hash."""
    return {
        "id": doc.get("id"),
        "label": doc.get("label"),
        "key_stub": doc.get("key_stub"),
        "scopes": doc.get("scopes", []),
        "created_at": doc.get("created_at"),
        "created_by": doc.get("created_by"),
        "revoked_at": doc.get("revoked_at"),
        "last_used_at": doc.get("last_used_at"),
        "limits": effective_limits(doc),
        "date_policy": doc.get("date_policy"),
        "country": doc.get("country"),
        "calls_total": doc.get("calls_total", 0),
        "rows_total": doc.get("rows_total", 0),
    }


async def authenticate_key(token: str) -> dict:
    """Validate a presented DataClean key; return the api actor.

    Raises HTTPException(401) with one generic message on any failure,
    so the response never reveals which part was wrong. The actor's
    tenant comes from the key record — a key can only ever act inside
    its own tenant.
    """
    if not token or not token.startswith(KEY_PREFIX):
        raise HTTPException(status_code=401, detail={
            "code": "invalid_api_key", "message": "Invalid API key"})
    digest = hash_key(token)
    doc = await db.dataclean_api_keys.find_one({"key_hash": digest})
    if not doc or not hmac.compare_digest(digest, doc.get("key_hash", "")):
        raise HTTPException(status_code=401, detail={
            "code": "invalid_api_key", "message": "Invalid API key"})
    if doc.get("revoked_at"):
        raise HTTPException(status_code=401, detail={
            "code": "invalid_api_key", "message": "Invalid API key"})
    # Entitlement: once a workspace is inside the DataClean billing
    # system (or the owner switches global enforcement on), its API
    # access follows its subscription state — a canceled workspace's
    # keys stop working here, at the one door every DataClean surface
    # (cleanup, jobs, connectors) authenticates through. The check
    # lives in lib/dataclean_billing.py and is imported lazily: that
    # module reads this one for the price ladder.
    from lib import dataclean_billing
    if not await dataclean_billing.entitled_with_db(db, doc["tenant_id"]):
        raise HTTPException(status_code=403, detail={
            "code": "subscription_inactive",
            "message": "This workspace's DataClean subscription is not active"})
    return {
        "id": f"dataclean_key:{doc['id']}",
        "tenant_id": doc["tenant_id"],
        "email": "dataclean-api@system.local",
        "name": f"DataClean API key ({doc.get('label') or 'key'})",
        "role": "dataclean_api_key",
        "auth_kind": "api_key",
        "api_key_id": doc["id"],
        "scopes": doc.get("scopes", []),
        "_key_doc": doc,
    }


async def get_dataclean_actor(request: Request) -> dict:
    """FastAPI dependency for the /api/v1/dataclean surface.

    Accepts `X-API-Key: mdck_...` or `Authorization: Bearer mdck_...`.
    A user JWT is NOT accepted here — this surface authenticates the
    API key itself.
    """
    token = request.headers.get("X-API-Key") or ""
    if not token:
        auth = request.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):]
    return await authenticate_key(token.strip())


# ------------------------------------------------------------- rate limits

class FixedWindowLimiter:
    """In-memory fixed-window counters, one bucket per (key, window).

    Honest scope: this is a single-process limiter, like the app's
    other in-process guards. It refuses politely (429 + Retry-After)
    rather than queueing, and counts only authenticated calls — key
    auth happens first, so a bucket is always a real key (or, for the
    playground, a client IP).
    """

    def __init__(self):
        self._hits = {}

    def check(self, bucket: str, limit: int, window_seconds: int):
        """Returns (allowed, retry_after_seconds, remaining)."""
        now = time.time()
        wid = int(now // window_seconds)
        if len(self._hits) > 4096:  # opportunistic prune of dead windows
            self._hits = {k: v for k, v in self._hits.items()
                          if k[2] >= wid - 1}
        key = (bucket, window_seconds, wid)
        count = self._hits.get(key, 0)
        if count >= limit:
            retry = int((wid + 1) * window_seconds - now) + 1
            return False, retry, 0
        self._hits[key] = count + 1
        return True, 0, limit - count - 1


key_limiter = FixedWindowLimiter()
playground_limiter = FixedWindowLimiter()


# ---------------------------------------------------------------- metering

async def record_usage(actor: dict, endpoint: str, rows: int, status: str):
    """One durable usage row per external API call + counter upkeep.

    This is the billing FOUNDATION only: rows are evidence for the
    console's usage page and any future packaging decision. Nothing
    here charges. Metering must never break the call it measures, so
    failures are swallowed (the call's own result stands).
    """
    try:
        await Scoped(db.dataclean_api_usage, actor["tenant_id"]).insert_one({
            "id": new_id(),
            "key_id": actor["api_key_id"],
            "endpoint": endpoint,
            "rows_processed": int(rows or 0),
            "status": status,  # ok | error | rate_limited | quota_exceeded
            "created_at": now_iso(),
        })
        await db.dataclean_api_keys.update_one(
            {"id": actor["api_key_id"]},
            {"$inc": {"calls_total": 1, "rows_total": int(rows or 0)},
             "$set": {"last_used_at": now_iso()}})
    except Exception:
        pass


async def month_rows_for_key(actor: dict) -> int:
    """Rows processed (status ok) by this key in the current UTC month,
    counted from the durable usage rows — never from a cached counter."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    rows = await Scoped(db.dataclean_api_usage, actor["tenant_id"]).find_list(
        {"key_id": actor["api_key_id"], "status": "ok"}, limit=100000)
    return sum(r.get("rows_processed", 0) for r in rows
               if (r.get("created_at") or "")[:7] == month)


async def enforce_limits(actor: dict, endpoint: str, rows_planned: int):
    """Rate-limit + quota gate for one authenticated call.

    Raises 429 (rate_limited | quota_exceeded) after metering the
    refusal; returns None when the call may proceed.
    """
    limits = effective_limits(actor.get("_key_doc") or {})
    kid = actor["api_key_id"]
    for window, limit in ((60, limits["per_minute"]),
                          (86400, limits["per_day"])):
        allowed, retry, _remaining = key_limiter.check(
            f"{kid}:{window}", limit, window)
        if not allowed:
            await record_usage(actor, endpoint, 0, "rate_limited")
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited",
                        "message": "Rate limit exceeded for this API key",
                        "retry_after_seconds": retry,
                        "window_seconds": window},
                headers={"Retry-After": str(retry)})
    used = await month_rows_for_key(actor)
    if used + int(rows_planned or 0) > limits["monthly_row_quota"]:
        await record_usage(actor, endpoint, 0, "quota_exceeded")
        raise HTTPException(
            status_code=429,
            detail={"code": "quota_exceeded",
                    "message": "Monthly row quota exceeded for this API key",
                    "rows_used_this_month": used,
                    "monthly_row_quota": limits["monthly_row_quota"]})
