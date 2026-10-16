"""Plaid webhook signature verification.

Plaid signs every webhook with a JWT in the `Plaid-Verification` header
(ES256). The JWT header's `kid` identifies the key; public keys are fetched
from the environment's `/webhook_verification_key/get` endpoint (authenticated
with the Plaid client_id/secret) and cached in memory.

Verification checks, in order:
  1. a `Plaid-Verification` header is present and parses as a JWT with
     alg == ES256 and a kid;
  2. the key for that kid is known (fetched from Plaid on a cache miss),
     and its `expired_at` is null;
  3. the JWT signature validates against that key;
  4. the `iat` claim is within the last 5 minutes (freshness / replay
     protection), allowing 60s of future skew;
  5. the `request_body_sha256` claim matches the SHA-256 of the RAW request
     body — computed before any JSON parsing, because re-serialization can
     change bytes and break the check.

Any failure returns (False, reason); the caller rejects the webhook. This
endpoint is otherwise unauthenticated (Plaid calls server-to-server), so a
failure to verify is a hard reject — Plaid redelivers webhooks, so a key-fetch
outage only delays, never loses, delivery.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Mapping, Optional

import jwt
import requests

logger = logging.getLogger(__name__)

_KEY_CACHE: dict[str, tuple[dict, float]] = {}
_KEY_CACHE_TTL = 24 * 3600
_IAT_MAX_AGE = 5 * 60
_IAT_FUTURE_SKEW = 60


def _fetch_key(kid: str, client_id: str, secret: str, base_url: str) -> Optional[dict]:
    """Fetch the verification key JWK from Plaid. Returns None on any failure."""
    try:
        r = requests.post(
            base_url + "/webhook_verification_key/get",
            json={"client_id": client_id, "secret": secret, "key_id": kid},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning("plaid webhook key fetch: HTTP %s for kid %s", r.status_code, kid)
            return None
        data = r.json()
        key = data.get("key")
        if not isinstance(key, dict):
            return None
        if key.get("expired_at") is not None:
            logger.warning("plaid webhook key %s is expired", kid)
            return None
        if key.get("alg") != "ES256" or key.get("kty") != "EC":
            logger.warning("plaid webhook key %s has unexpected alg/kty", kid)
            return None
        return key
    except Exception as e:
        logger.warning("plaid webhook key fetch failed for kid %s: %s", kid, e)
        return None


def _get_key(kid: str, client_id: str, secret: str, base_url: str) -> Optional[dict]:
    now = time.time()
    cached = _KEY_CACHE.get(kid)
    if cached and now - cached[1] < _KEY_CACHE_TTL:
        return cached[0]
    key = _fetch_key(kid, client_id, secret, base_url)
    if key is not None:
        _KEY_CACHE[kid] = (key, now)
    return key


def clear_key_cache() -> None:
    """Test hook: drop cached Plaid verification keys."""
    _KEY_CACHE.clear()


def verify_plaid_webhook(
    raw_body: bytes,
    headers: Mapping[str, str],
    client_id: str,
    secret: str,
    base_url: str,
) -> tuple[bool, str]:
    """Verify a Plaid webhook. Returns (ok, reason); reason is log-safe."""
    token = None
    for name in ("plaid-verification", "Plaid-Verification", "PLAID-VERIFICATION"):
        if name in headers:
            token = headers[name]
            break
    if not token:
        return False, "missing Plaid-Verification header"
    try:
        header = jwt.get_unverified_header(token)
    except Exception:
        return False, "unparseable verification token"
    kid = header.get("kid")
    if header.get("alg") != "ES256" or not kid:
        return False, "unsupported alg or missing kid"
    key_jwk = _get_key(kid, client_id, secret, base_url)
    if key_jwk is None:
        return False, "verification key unavailable"
    try:
        key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(key_jwk))
    except Exception:
        return False, "invalid verification key material"
    try:
        claims = jwt.decode(token, key, algorithms=["ES256"],
                            options={"require": ["iat", "request_body_sha256"]})
    except Exception:
        return False, "signature invalid"
    now = time.time()
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)) or iat < now - _IAT_MAX_AGE or iat > now + _IAT_FUTURE_SKEW:
        return False, "stale iat"
    expected = claims.get("request_body_sha256")
    actual = hashlib.sha256(raw_body).hexdigest()
    if not isinstance(expected, str) or not hmac.compare_digest(expected, actual):
        return False, "body hash mismatch"
    return True, "ok"
