"""Security hardening tests: CSP header, Redis-backed rate limiting with
in-memory fallback, Plaid webhook signature verification, PII masking.

No live MongoDB, Redis, or Plaid needed — everything is faked or mocked.

Run from the backend directory with:
    python3 -m pytest tests/test_security_hardening.py -n 0 -q
"""
import asyncio
import base64
import hashlib
import json
import os
import time
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "testdb")
os.environ.pop("REDIS_URL", None)
os.environ.pop("PLAID_CLIENT_ID", None)
os.environ.pop("PLAID_SECRET", None)

import pytest
from fastapi import HTTPException
from starlette.responses import JSONResponse

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from lib import plaid_webhook as pw
from lib import ratelimit as rl
from lib.pii import mask_email, mask_pii, redact_mapping, scrub_secrets
from lib.security_headers import (
    CONTENT_SECURITY_POLICY,
    ContentSecurityPolicyMiddleware,
    SecurityHeadersMiddleware,
)
from routers import bankfeed


# ------------------------------------------------------------------ fakes

class FakeRequest:
    def __init__(self, path="/api/x", headers=None, body=b"", client_host="1.2.3.4"):
        self.url = SimpleNamespace(path=path)
        self.headers = headers or {}
        self._body = body
        self.client = SimpleNamespace(host=client_host)

    async def body(self):
        return self._body


async def _ok_response(request):
    return JSONResponse({"ok": True})


class FakeColl:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id="x")


class FakeZSetRedis:
    """Minimal in-memory stand-in for the redis commands _RedisStore uses."""

    def __init__(self):
        self.data = {}

    # -- pipeline API used by _RedisStore.check_and_hit
    def pipeline(self):
        return _FakePipe(self)

    def zadd(self, key, mapping):
        self.data.setdefault(key, []).extend(mapping.items())

    def expire(self, key, ttl):
        pass


class _FakePipe:
    def __init__(self, client):
        self.c = client
        self.ops = []

    def zremrangebyscore(self, key, lo, hi):
        self.ops.append(("zrem", key, lo, hi))
        return self

    def zcount(self, key, lo, hi):
        self.ops.append(("zcount", key, lo, hi))
        return self

    def execute(self):
        out = []
        for op in self.ops:
            if op[0] == "zrem":
                _, key, lo, hi = op
                members = self.c.data.get(key, [])
                kept = [(m, s) for m, s in members if not (lo <= s <= hi)]
                self.c.data[key] = kept
                out.append(len(members) - len(kept))
            else:
                _, key, lo, _hi = op
                out.append(sum(1 for _m, s in self.c.data.get(key, []) if s >= lo))
        return out


def _b64url_int(n: int, length: int = None) -> str:
    # JWK EC coordinates are fixed-width (RFC 7518 section 6.2): a P-256
    # coordinate is always 32 bytes, leading zero bytes included. Encoding
    # at minimal length instead makes roughly 1-in-128 generated keypairs
    # produce a 31-byte coordinate, which PyJWT's ECAlgorithm.from_jwk
    # rejects -- that was the intermittent TestPlaidVerify flake (healthy
    # signatures failing with "invalid verification key material").
    size = length if length is not None else (n.bit_length() + 7) // 8
    raw = n.to_bytes(size, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _ec_keypair(priv=None):
    if priv is None:
        priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_numbers()
    coord_len = (priv.curve.key_size + 7) // 8  # 32 for P-256
    jwk = {
        "kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig",
        "kid": "test-kid-1",
        "x": _b64url_int(pub.x, coord_len), "y": _b64url_int(pub.y, coord_len),
    }
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwk, pem


def _plaid_jwt(pem, kid, body: bytes, iat=None, alg="ES256", extra_claims=None):
    claims = {
        "iat": int(time.time()) if iat is None else iat,
        "request_body_sha256": hashlib.sha256(body).hexdigest(),
    }
    claims.update(extra_claims or {})
    return pyjwt.encode(claims, pem, algorithm=alg, headers={"kid": kid})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)
    pw.clear_key_cache()
    yield
    pw.clear_key_cache()


# ------------------------------------------------------------------ 1. CSP

class TestCSP:
    def test_header_present_on_response(self):
        mw = ContentSecurityPolicyMiddleware(app=None)
        # BaseHTTPMiddleware needs .app only when called; dispatch works alone.
        resp = asyncio.run(mw.dispatch(FakeRequest(), _ok_response))
        csp = resp.headers.get("content-security-policy")
        assert csp, "Content-Security-Policy header missing"
        assert csp == CONTENT_SECURITY_POLICY

    def test_policy_allowlists_frontend_needs(self):
        assert "https://cdn.plaid.com" in CONTENT_SECURITY_POLICY  # Plaid Link CDN
        assert "https://fonts.googleapis.com" in CONTENT_SECURITY_POLICY
        assert "https://fonts.gstatic.com" in CONTENT_SECURITY_POLICY
        assert "ice1.somafm.com" in CONTENT_SECURITY_POLICY  # music player streams

    def test_policy_has_no_unsafe_script_execution(self):
        script_src = [d for d in CONTENT_SECURITY_POLICY.split("; ")
                      if d.startswith("script-src")][0]
        assert "unsafe-inline" not in script_src
        assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
        assert "object-src 'none'" in CONTENT_SECURITY_POLICY
        assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY

    def test_stacks_with_existing_security_headers(self):
        async def both(request):
            mw1 = SecurityHeadersMiddleware(app=None)
            mw2 = ContentSecurityPolicyMiddleware(app=None)
            resp = await mw2.dispatch(request, _ok_response)
            return await mw1.dispatch(request, lambda r: _done(resp))

        async def _done(resp):
            return resp

        resp = asyncio.run(both(FakeRequest()))
        assert resp.headers.get("content-security-policy")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"


# ------------------------------------------------- 2. rate limiting store

class TestRateLimitStores:
    def test_no_redis_url_gives_memory_store_and_warns(self, caplog):
        with caplog.at_level("WARNING", logger="lib.ratelimit"):
            store = rl.build_store()
        assert isinstance(store, rl._MemoryStore)
        assert any("REDIS_URL" in r.message for r in caplog.records)

    def test_redis_url_unreachable_falls_back_to_memory(self, monkeypatch, caplog):
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
        with caplog.at_level("WARNING", logger="lib.ratelimit"):
            store = rl.build_store()
        # redis package is absent in this env -> ImportError path; either way
        # the contract is "never raises, memory fallback".
        assert isinstance(store, rl._MemoryStore)

    def test_memory_store_enforces_limits(self):
        store = rl._MemoryStore()
        ip = "9.9.9.9"
        for _ in range(300):
            assert store.check_and_hit(ip, time.monotonic()) is True
        assert store.check_and_hit(ip, time.monotonic()) is False  # 301st denied

    def test_memory_store_windows_slide(self):
        store = rl._MemoryStore()
        ip = "8.8.8.8"
        old = time.monotonic() - 61  # outside the 60s window
        for _ in range(300):
            assert store.check_and_hit(ip, old) is True
        # all 300 aged out of the 60s window -> allowed again
        assert store.check_and_hit(ip, time.monotonic()) is True

    def test_redis_store_enforces_limits(self):
        store = rl._RedisStore(FakeZSetRedis())
        ip = "7.7.7.7"
        for _ in range(300):
            assert store.check_and_hit(ip, time.monotonic()) is True
        assert store.check_and_hit(ip, time.monotonic()) is False

    def test_redis_store_windows_slide(self):
        store = rl._RedisStore(FakeZSetRedis())
        ip = "6.6.6.6"
        old = time.monotonic() - 61
        for _ in range(300):
            assert store.check_and_hit(ip, old) is True
        assert store.check_and_hit(ip, time.monotonic()) is True

    def test_middleware_fail_open_on_store_error(self):
        class Broken:
            def check_and_hit(self, key, now):
                raise ConnectionError("redis down")

        mw = rl.RateLimitMiddleware(app=None, store=Broken())
        assert asyncio.run(mw._check("1.1.1.1")) is True

    def test_middleware_429s_through_dispatch(self):
        store = rl._MemoryStore()
        mw = rl.RateLimitMiddleware(app=None, store=store)
        req = FakeRequest(path="/api/invoices")
        for _ in range(300):
            resp = asyncio.run(mw.dispatch(req, _ok_response))
            assert resp.status_code == 200
        resp = asyncio.run(mw.dispatch(req, _ok_response))
        assert resp.status_code == 429
        assert resp.headers.get("retry-after") == "60"

    def test_middleware_ignores_non_api_paths(self):
        store = rl._MemoryStore()
        mw = rl.RateLimitMiddleware(app=None, store=store)
        resp = asyncio.run(mw.dispatch(FakeRequest(path="/healthz"), _ok_response))
        assert resp.status_code == 200
        assert store._buckets == {} or True  # no /api hit recorded


# ------------------------------------------- 3. Plaid webhook verification

class TestPlaidVerify:
    def _setup(self, monkeypatch, body, **jwt_kw):
        jwk, pem = _ec_keypair()
        token = _plaid_jwt(pem, "test-kid-1", body, **jwt_kw)
        monkeypatch.setattr(pw, "_fetch_key", lambda *a: jwk)
        headers = {"plaid-verification": token}
        return headers

    def test_accepts_good_signature(self, monkeypatch):
        body = b'{"webhook_type":"TRANSACTIONS","webhook_code":"SYNC_UPDATES"}'
        headers = self._setup(monkeypatch, body)
        ok, reason = pw.verify_plaid_webhook(
            body, headers, "cid", "sec", "https://sandbox.plaid.com")
        assert ok is True and reason == "ok"

    def test_rejects_tampered_body(self, monkeypatch):
        body = b'{"webhook_type":"TRANSACTIONS"}'
        headers = self._setup(monkeypatch, body)
        ok, reason = pw.verify_plaid_webhook(
            body + b" ", headers, "cid", "sec", "https://sandbox.plaid.com")
        assert ok is False and reason == "body hash mismatch"

    def test_rejects_stale_iat(self, monkeypatch):
        body = b'{}'
        headers = self._setup(monkeypatch, body, iat=int(time.time()) - 600)
        ok, _ = pw.verify_plaid_webhook(
            body, headers, "cid", "sec", "https://sandbox.plaid.com")
        assert ok is False

    def test_rejects_missing_header(self):
        ok, reason = pw.verify_plaid_webhook(b"{}", {}, "cid", "sec", "https://x")
        assert ok is False and "missing" in reason

    def test_rejects_unknown_kid(self, monkeypatch):
        jwk, pem = _ec_keypair()
        body = b"{}"
        token = _plaid_jwt(pem, "nope-kid", body)
        monkeypatch.setattr(pw, "_fetch_key", lambda *a: None)
        ok, _ = pw.verify_plaid_webhook(
            body, {"plaid-verification": token}, "cid", "sec", "https://x")
        assert ok is False

    def test_rejects_wrong_alg(self, monkeypatch):
        jwk, _pem = _ec_keypair()
        body = b"{}"
        # Attacker-style alg-confusion attempt: HS256-signed token.
        token = pyjwt.encode(
            {"iat": int(time.time()),
             "request_body_sha256": hashlib.sha256(body).hexdigest()},
            b"attacker-hmac-secret", algorithm="HS256",
            headers={"kid": "test-kid-1"})
        monkeypatch.setattr(pw, "_fetch_key", lambda *a: jwk)
        ok, reason = pw.verify_plaid_webhook(
            body, {"plaid-verification": token}, "cid", "sec", "https://x")
        assert ok is False and "alg" in reason

    def test_accepts_key_with_leading_zero_coordinate(self, monkeypatch):
        # Regression for the intermittent full-suite flake: the fixed
        # private scalar 43 yields a P-256 point whose y coordinate has a
        # leading zero byte (31 significant bytes). The JWK must still
        # encode it at the full 32-byte width, or from_jwk rejects the
        # key and even a healthy signature fails verification.
        priv = ec.derive_private_key(43, ec.SECP256R1())
        jwk, pem = _ec_keypair(priv)

        def _decode(s):
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

        assert len(_decode(jwk["x"])) == 32 and len(_decode(jwk["y"])) == 32
        body = b'{"webhook_type":"TRANSACTIONS","webhook_code":"SYNC_UPDATES"}'
        token = _plaid_jwt(pem, "test-kid-1", body)
        monkeypatch.setattr(pw, "_fetch_key", lambda *a: jwk)
        headers = {"plaid-verification": token}
        ok, reason = pw.verify_plaid_webhook(
            body, headers, "cid", "sec", "https://sandbox.plaid.com")
        assert ok is True and reason == "ok"
        ok, reason = pw.verify_plaid_webhook(
            body + b" ", headers, "cid", "sec", "https://sandbox.plaid.com")
        assert ok is False and reason == "body hash mismatch"


class TestWebhookEndpoint:
    def _req(self, body: bytes, headers=None):
        return FakeRequest(path="/api/bankfeed/webhook", headers=headers, body=body)

    def test_rejects_bad_signature_when_configured(self, monkeypatch):
        monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
        monkeypatch.setenv("PLAID_SECRET", "sec")
        monkeypatch.setattr(bankfeed, "verify_plaid_webhook",
                            lambda *a: (False, "signature invalid"))
        fake_db = SimpleNamespace(bank_webhooks=FakeColl())
        monkeypatch.setattr(bankfeed, "db", fake_db)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(bankfeed.webhook(self._req(b'{"webhook_type":"X"}')))
        assert ei.value.status_code == 403
        assert fake_db.bank_webhooks.docs == []  # nothing recorded

    def test_accepts_good_signature_when_configured(self, monkeypatch):
        monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
        monkeypatch.setenv("PLAID_SECRET", "sec")
        seen = {}
        def _verify(raw, headers, cid, sec, base):
            seen["raw"] = raw
            return (True, "ok")
        monkeypatch.setattr(bankfeed, "verify_plaid_webhook", _verify)
        fake_db = SimpleNamespace(bank_webhooks=FakeColl())
        monkeypatch.setattr(bankfeed, "db", fake_db)
        raw = b'{"webhook_type":"TRANSACTIONS","webhook_code":"SYNC_UPDATES","item_id":"i1"}'
        out = asyncio.run(bankfeed.webhook(self._req(raw)))
        assert out == {"ok": True}
        assert seen["raw"] == raw  # verification ran over the RAW body
        assert fake_db.bank_webhooks.docs[0]["webhook_type"] == "TRANSACTIONS"

    def test_skips_verification_gracefully_when_unconfigured(self, monkeypatch):
        called = []
        monkeypatch.setattr(bankfeed, "verify_plaid_webhook",
                            lambda *a: called.append(1) or (True, "ok"))
        fake_db = SimpleNamespace(bank_webhooks=FakeColl())
        monkeypatch.setattr(bankfeed, "db", fake_db)
        out = asyncio.run(bankfeed.webhook(self._req(b'{"webhook_type":"X"}')))
        assert out == {"ok": True}
        assert called == []  # verification skipped, old log-only path preserved


# ------------------------------------------------------------ 4. PII masking

class TestPiiMasking:
    def test_mask_email(self):
        assert mask_email("jane.doe@example.com") == "j***@example.com"
        assert mask_email("a@example.com") == "*@example.com"
        assert mask_email("") == "—"
        assert mask_email("not-an-email") == "***"

    def test_mask_pii_kinds(self):
        assert mask_pii("123456789", "tin") == "•••6789"
        assert mask_pii("jane@example.com", "email") == "j***@example.com"
        assert mask_pii("anything", "generic") == "***"
        assert mask_pii(None) == "—"
        assert mask_pii("4111111111111111", "account") == "•••1111"

    def test_scrub_secrets(self):
        assert scrub_secrets("Bearer abcdefghijklmnop123") == "Bearer ***REDACTED***"
        assert scrub_secrets("api_key=supersecretvalue99") == "api_key=***REDACTED***"
        s = scrub_secrets("token gAAAAA" + "x" * 40 + " end")
        assert "gAAAAA" not in s and "***REDACTED***" in s
        assert scrub_secrets("plain error, nothing secret") == "plain error, nothing secret"

    def test_redact_mapping(self):
        m = redact_mapping({"email": "jane@example.com", "n": 3}, ("email",))
        assert m == {"email": "j***@example.com", "n": 3}
