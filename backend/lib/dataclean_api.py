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
rows processed, status, timestamp). No code in this module — or in the
routers built on it — charges anything, creates a Stripe object, or
consults BILLING_ALLOW_LIVE. Pricing/quota values are config only
(see pricing_config) and default to clearly-marked placeholders.
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


def pricing_config() -> dict:
    """Storefront pricing + plan quotas, as CONFIG values only.

    Defaults are placeholders (flagged `placeholder: true` and rendered
    as such by the storefront) — final prices are an owner decision.
    Nothing here is ever charged: there is no billing code path in the
    DataClean shell, in test mode or any other mode.
    """
    plans = []
    for pid, label, def_rows in (("starter", "Starter", 10000),
                                 ("growth", "Growth", 50000),
                                 ("scale", "Scale", 250000)):
        plans.append({
            "id": pid,
            "name": label,
            "monthly_price_cents": _env_int(
                f"DATACLEAN_PRICE_{pid.upper()}_CENTS", 0),
            "rows_included": _env_int(
                f"DATACLEAN_PLAN_{pid.upper()}_ROWS", def_rows),
            "overage_per_1000_rows_cents": _env_int(
                f"DATACLEAN_PRICE_{pid.upper()}_OVERAGE_CENTS", 0),
        })
    return {
        "currency": "USD",
        "placeholder": _env_bool("DATACLEAN_PRICING_PLACEHOLDER", True),
        "billing_mode": "test",  # display only — no charging exists
        "plans": plans,
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
                monthly_row_quota=None):
    """Build the stored document for a fresh DataClean key.

    Returns (doc, full_key). The full key exists only in the create
    response; the doc carries its hash + display stub, the key's limit
    overrides (None = use the configured defaults), and denormalized
    usage counters maintained by record_usage.
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
        "calls_total": 0,
        "rows_total": 0,
    }
    return doc, full


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
