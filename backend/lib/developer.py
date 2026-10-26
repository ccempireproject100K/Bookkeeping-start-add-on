"""Developer foundation: tenant API keys + signed outbound webhooks.

Two machine surfaces, both following the house patterns:

* API keys mirror lib/integration_auth.py: a long random secret shown
  exactly once at creation, only its SHA-256 hash stored, verification
  by constant-time digest compare, and one generic 401 for every
  failure. The resulting actor has role "api_key" — a role that
  appears in NO entry of lib.auth.PERMISSIONS, so an API key is
  accepted only by the endpoints that explicitly depend on
  `get_api_key_actor` (the /developers/v1 read surface in v1). It is
  not a user session and opens nothing else.

* Webhooks mirror the service-hub pattern (service-hub
  backend/lib/webhooks.py): deliveries are signed with HMAC-SHA256
  over the raw body (header value ``sha256=<hex>``), attempted
  immediately, retried with backoff of 30 * 2**attempts seconds up to
  3 attempts, and every attempt lands in the `webhook_deliveries`
  log. A subscription's signing secret is generated at creation,
  returned ONCE in the create response, stored server-side for
  signing, and never included in list/detail views.

`emit_event` NEVER raises: a broken webhook must not break invoicing.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets as _secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from fastapi import HTTPException, Request

from db import db
from lib.base import new_id, now_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- API keys

KEY_PREFIX = "maek_"
KEY_SCOPES = ["books:read"]


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_api_key() -> str:
    """Full key string: ``maek_`` + 43 urlsafe chars (32 random bytes)."""
    return KEY_PREFIX + _secrets.token_urlsafe(32)


def key_stub(token: str) -> str:
    """Non-secret display stub: first 12 chars + last 4."""
    return f"{token[:12]}...{token[-4:]}"


def is_api_key(token: str) -> bool:
    return bool(token) and token.startswith(KEY_PREFIX)


def new_api_key_doc(*, tenant_id: str, created_by: str, label: str):
    """Build the stored document for a fresh key.

    Returns (doc, full_key). The full key exists only in the create
    response; the doc carries its hash + display stub.
    """
    full = mint_api_key()
    doc = {
        "id": new_id(),
        "tenant_id": tenant_id,
        "label": label or "API key",
        "key_hash": hash_key(full),
        "key_stub": key_stub(full),
        "scopes": list(KEY_SCOPES),
        "created_at": now_iso(),
        "created_by": created_by,
        "revoked_at": None,
        "last_used_at": None,
    }
    return doc, full


def public_api_key_view(doc: dict) -> dict:
    """Metadata-only view — never the key, never its hash."""
    return {
        "id": doc.get("id"),
        "tenant_id": doc.get("tenant_id"),
        "label": doc.get("label"),
        "key_stub": doc.get("key_stub"),
        "scopes": doc.get("scopes", []),
        "created_at": doc.get("created_at"),
        "created_by": doc.get("created_by"),
        "revoked_at": doc.get("revoked_at"),
        "last_used_at": doc.get("last_used_at"),
    }


async def authenticate_api_key(token: str) -> dict:
    """Validate a presented API key; return the api_key actor.

    Raises HTTPException(401) with one generic message on any failure,
    so the response never reveals which part was wrong.
    """
    if not is_api_key(token):
        raise HTTPException(status_code=401, detail="Invalid API key")
    digest = hash_key(token)
    doc = await db.api_keys.find_one({"key_hash": digest})
    if not doc or not hmac.compare_digest(digest, doc.get("key_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid API key")
    if doc.get("revoked_at"):
        raise HTTPException(status_code=401, detail="Invalid API key")
    try:
        await db.api_keys.update_one(
            {"id": doc["id"]}, {"$set": {"last_used_at": now_iso()}})
    except Exception:
        pass  # bookkeeping about the key must not break the call
    return {
        "id": f"api_key:{doc['id']}",
        "tenant_id": doc["tenant_id"],
        "email": "api-key@system.local",
        "name": f"API key ({doc.get('label') or 'key'})",
        "role": "api_key",
        "auth_kind": "api_key",
        "api_key_id": doc["id"],
        "scopes": doc.get("scopes", []),
    }


def _presented_key(request: Request) -> str:
    headers = request.headers
    token = headers.get("X-API-Key") or headers.get("x-api-key") or ""
    if not token:
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):]
    return token.strip()


async def get_api_key_actor(request: Request) -> dict:
    """FastAPI dependency for the public v1 API surface.

    Accepts `Authorization: Bearer maek_...` or an `X-API-Key` header.
    A user JWT is NOT accepted here — this surface authenticates the
    API key itself.
    """
    token = _presented_key(request)
    if not is_api_key(token):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return await authenticate_api_key(token)


# ---------------------------------------------------------------- webhooks

EVENT_TYPES = (
    "invoice.created",
    "invoice.paid",
    "payment.received",
    "customer.created",
    "webhook.test",
)

EVENT_DESCRIPTIONS = {
    "invoice.created": "A new invoice was issued in the workspace.",
    "invoice.paid": "An invoice was paid in full.",
    "payment.received": "A payment was applied to an invoice (full or partial).",
    "customer.created": "A new customer was created in the workspace.",
    "webhook.test": "A test delivery sent from the Developers settings page.",
}

MAX_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 30

# Subscription lookups sit on the hot path of invoice/customer writes
# (via emit_event), so a dead database must not stall those writes for
# Mongo's full server-selection timeout. A failed/timed-out lookup
# marks THIS db object unavailable for a short window; later emits in
# the window skip instantly. The mark is keyed by object identity and
# holds a strong reference, so a swapped-in (test) db is never
# confused with the failed one.
_LOOKUP_TIMEOUT_SECONDS = 1.0
_UNAVAILABLE_WINDOW_SECONDS = 30.0
_unavailable_db = None
_unavailable_until = 0.0


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def mint_signing_secret() -> str:
    return _secrets.token_urlsafe(32)


def new_subscription_doc(*, tenant_id: str, created_by: str, url: str,
                         events: list):
    """Build the stored subscription document.

    Returns (doc, signing_secret). The secret is stored on the doc
    (the server needs it to sign deliveries) but is only ever
    RETURNED by the create endpoint — see public_subscription_view.
    """
    secret = mint_signing_secret()
    doc = {
        "id": new_id(),
        "tenant_id": tenant_id,
        "url": url,
        "events": list(events),
        "signing_secret": secret,
        "active": True,
        "created_at": now_iso(),
        "created_by": created_by,
    }
    return doc, secret


def public_subscription_view(doc: dict) -> dict:
    """Subscription view for list/detail — NEVER the signing secret."""
    return {
        "id": doc.get("id"),
        "tenant_id": doc.get("tenant_id"),
        "url": doc.get("url"),
        "events": doc.get("events", []),
        "active": bool(doc.get("active", True)),
        "created_at": doc.get("created_at"),
        "created_by": doc.get("created_by"),
    }


def public_delivery_view(doc: dict) -> dict:
    """Delivery-log row. The payload carries no secrets by design."""
    return {
        "id": doc.get("id"),
        "tenant_id": doc.get("tenant_id"),
        "subscription_id": doc.get("subscription_id"),
        "event": doc.get("event"),
        "payload": doc.get("payload"),
        "attempts": doc.get("attempts", 0),
        "status": doc.get("status"),
        "last_status": doc.get("last_status"),
        "last_error": doc.get("last_error"),
        "last_attempt_at": doc.get("last_attempt_at"),
        "next_retry_at": doc.get("next_retry_at"),
        "created_at": doc.get("created_at"),
    }


# ------------------------------------------------------- webhook URL safety
#
# Webhook destinations are member-supplied URLs the server POSTs to —
# a blind SSRF surface if unchecked (security audit 2026-10-26, N-2;
# owner ruling: the straight lock, no self-hosting carve-out).
# Rules, enforced at save time AND again inside every delivery hop:
#   * https only;
#   * the host must resolve ONLY to public addresses — loopback,
#     private, link-local, CGNAT, multicast, reserved and
#     documentation ranges are all refused;
#   * redirects are followed manually (max 3) and EVERY hop's target
#     is re-validated and re-resolved before it is requested, so a
#     public URL cannot bounce the delivery onto an internal address.
# Validation never trusts an earlier check: _post re-resolves at send
# time, so DNS answers that changed since save cannot smuggle a
# private target in. (Residual, stated honestly: `requests` resolves
# again when it connects, milliseconds later — the standard
# re-validation mitigation, short of full IP-pinned TLS.)

class WebhookUrlError(ValueError):
    """A webhook destination that must never be saved or sent to."""


def _resolve_ips(host: str) -> list:
    """All addresses a host resolves to ([] when unresolvable).
    Module-level so tests can pin resolution without real DNS."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return []
    out = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip not in out:
            out.append(ip)
    return out


def _ip_is_public(ip) -> bool:
    # is_global is False for loopback/private/link-local/CGNAT/
    # reserved/documentation ranges; multicast reports global, so it
    # is excluded explicitly.
    return ip.is_global and not ip.is_multicast


def assert_webhook_url_public(url: str) -> None:
    """Raise WebhookUrlError unless `url` is an https URL whose host
    resolves only to public addresses. When the host cannot be
    resolved at all, the check is inconclusive and passes — delivery
    re-checks with a fresh resolution and refuses then; nothing is
    ever SENT without a passing check."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        raise WebhookUrlError("Webhook url must be an https URL")
    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _ip_is_public(literal):
            raise WebhookUrlError(
                "Webhook url points at a non-public address")
        return
    if host == "localhost" or host.endswith(".localhost") \
            or host.endswith(".local"):
        raise WebhookUrlError("Webhook url host is not public")
    ips = _resolve_ips(host)
    if ips and not all(_ip_is_public(ip) for ip in ips):
        raise WebhookUrlError(
            "Webhook url resolves to a non-public address")


_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _post(url: str, body: bytes, headers: dict) -> int:
    """One synchronous HTTP POST (run via asyncio.to_thread). Returns
    the final response status code. `requests` is the house HTTP
    client — httpx is not in requirements.

    Every hop — the initial URL and each redirect target — is
    validated and re-resolved via assert_webhook_url_public BEFORE it
    is requested; a hop that fails validation raises WebhookUrlError
    and is never sent (deliver() records it as a failed attempt)."""
    current = url
    for hop in range(_MAX_REDIRECTS + 1):
        assert_webhook_url_public(current)
        resp = requests.post(current, data=body, headers=headers,
                             timeout=10, allow_redirects=False)
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("Location") or resp.headers.get(
                "location")
            if location and hop < _MAX_REDIRECTS:
                current = urljoin(current, location)
                continue
        return resp.status_code
    return resp.status_code


def _retry_at(attempts: int) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=_RETRY_BASE_SECONDS * (2 ** attempts))).isoformat()


async def deliver(delivery: dict, subscription: dict) -> dict:
    """Attempt one delivery now; update + return the delivery doc.

    Mirrors the hub: attempts increment per try, failures stay
    "pending" with a backoff `next_retry_at` until MAX_ATTEMPTS, then
    "failed". Signing is over the exact body bytes sent.
    """
    body = json.dumps(delivery["payload"]).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-MAE-Event": delivery["event"],
        "X-MAE-Delivery": delivery["id"],
    }
    secret = subscription.get("signing_secret")
    if secret:
        headers["X-MAE-Signature-256"] = sign(secret, body)
    status = None
    error = None
    try:
        status = await asyncio.to_thread(
            _post, subscription["url"], body, headers)
        ok = 200 <= status < 300
        if not ok:
            error = f"HTTP {status}"
    except Exception as exc:  # network down, bad URL — retry later
        ok, error = False, str(exc)[:200]
    attempts = delivery.get("attempts", 0) + 1
    updates = {
        "attempts": attempts,
        "last_status": status,
        "last_error": error,
        "last_attempt_at": now_iso(),
        "status": "delivered" if ok else (
            "failed" if attempts >= MAX_ATTEMPTS else "pending"),
        "next_retry_at": None if ok or attempts >= MAX_ATTEMPTS
        else _retry_at(attempts),
    }
    await db.webhook_deliveries.update_one(
        {"id": delivery["id"]}, {"$set": updates})
    delivery.update(updates)
    return delivery


async def deliver_to_subscription(subscription: dict, tenant_id: str,
                                  event: str, data: dict) -> dict:
    """Create a delivery for one subscription and attempt it now."""
    delivery_id = new_id()
    created = now_iso()
    delivery = {
        "id": delivery_id,
        "tenant_id": tenant_id,
        "subscription_id": subscription["id"],
        "event": event,
        "payload": {
            "id": delivery_id,
            "event": event,
            "tenant_id": tenant_id,
            "created_at": created,
            "data": data,
        },
        "attempts": 0,
        "status": "pending",
        "last_status": None,
        "last_error": None,
        "last_attempt_at": None,
        "next_retry_at": None,
        "created_at": created,
    }
    await db.webhook_deliveries.insert_one(delivery)
    return await deliver(delivery, subscription)


async def _subscribed(tenant_id: str, event: str) -> list:
    """Active subscriptions for this tenant + event.

    Best-effort: a lookup failure/timeout returns [] (and briefly
    marks this db unavailable) rather than stalling the caller.
    """
    global _unavailable_db, _unavailable_until
    if _unavailable_db is db and time.monotonic() < _unavailable_until:
        return []

    async def _find():
        rows = await db.webhook_subscriptions.find(
            {"tenant_id": tenant_id, "active": True},
            {"_id": 0}).sort("created_at", -1).to_list(500)
        return [r for r in rows if event in (r.get("events") or [])]

    try:
        return await asyncio.wait_for(_find(), timeout=_LOOKUP_TIMEOUT_SECONDS)
    except Exception as exc:
        _unavailable_db = db
        _unavailable_until = time.monotonic() + _UNAVAILABLE_WINDOW_SECONDS
        logger.warning("webhook subscription lookup skipped: %s", exc)
        return []


async def emit_event(tenant_id: str, event: str, data: dict) -> list:
    """Fan an event out to subscribed, active webhook subscriptions.

    Creates + attempts one delivery per subscription. NEVER raises —
    webhooks are a side channel; the business operation that emitted
    the event must succeed or fail on its own merits only.
    """
    try:
        if event not in EVENT_TYPES:
            logger.warning("emit_event: unknown event %r ignored", event)
            return []
        deliveries = []
        for sub in await _subscribed(tenant_id, event):
            try:
                deliveries.append(
                    await deliver_to_subscription(sub, tenant_id, event, data))
            except Exception as exc:
                logger.warning("webhook delivery failed sub=%s event=%s: %s",
                               sub.get("id"), event, exc)
        return deliveries
    except Exception as exc:  # last-resort guard: emit must never raise
        logger.warning("emit_event(%s) failed: %s", event, exc)
        return []


def _is_due(delivery: dict) -> bool:
    nxt = delivery.get("next_retry_at")
    if not nxt:
        return True
    try:
        ts = datetime.fromisoformat(nxt)
    except (TypeError, ValueError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts <= datetime.now(timezone.utc)


async def retry_due_deliveries() -> int:
    """Re-attempt pending deliveries whose backoff has elapsed.

    Returns how many deliveries were re-attempted. Lib function only —
    scheduler wiring is a follow-up, deliberately not done here.
    """
    rows = await db.webhook_deliveries.find(
        {"status": "pending"}, {"_id": 0}).to_list(500)
    retried = 0
    for delivery in rows:
        if not _is_due(delivery):
            continue
        sub = await db.webhook_subscriptions.find_one(
            {"id": delivery.get("subscription_id")})
        if not sub:
            continue
        try:
            await deliver(delivery, sub)
            retried += 1
        except Exception as exc:
            logger.warning("webhook retry failed delivery=%s: %s",
                           delivery.get("id"), exc)
    return retried
