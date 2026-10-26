"""Password reset flow: token lifecycle, uniform responses, session
revocation, and the password actually changing.

No live MongoDB needed — the db collections the flow touches are faked
in memory, following the monkeypatch style of the other router tests.

Run from the backend directory with:
    python3 -m pytest tests/test_password_reset.py -n 0 -q
"""
import asyncio
import hashlib
import os
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "testdb")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest
from fastapi import HTTPException

from db import db
from lib.auth import create_access_token, get_current_user, verify_password
from routers import password_reset as pr


# ------------------------------------------------------------------ fakes

def _matches(doc, query):
    for key, cond in query.items():
        val = doc.get(key)
        if isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$gte" and not (val is not None and val >= operand):
                    return False
                if op == "$lt" and not (val is not None and val < operand):
                    return False
                if op == "$ne" and not (val != operand):
                    return False
        elif val != cond:
            return False
    return True


class FakeColl:
    def __init__(self):
        self.docs = []

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return d
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, query, update):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, query, update):
        n = 0
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                n += 1
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _matches(d, query))


USER = {
    "id": "user-1", "tenant_id": "tenant-1", "email": "owner@shop.example.com",
    "name": "Owner", "role": "owner", "token_version": 3,
}


@pytest.fixture()
def env(monkeypatch):
    users = FakeColl()
    user = dict(USER)
    from lib.auth import hash_password
    user["password_hash"] = hash_password("old-password-123")
    users.docs.append(user)
    resets = FakeColl()
    requests = FakeColl()
    attempts = FakeColl()
    attempts.docs.append({"id": "a1", "email": USER["email"],
                          "at": datetime.now(timezone.utc)})
    audit = FakeColl()
    monkeypatch.setattr(db, "users", users)
    monkeypatch.setattr(db, "password_resets", resets)
    monkeypatch.setattr(db, "password_reset_requests", requests)
    monkeypatch.setattr(db, "login_attempts", attempts)
    monkeypatch.setattr(db, "audit_log", audit)
    sent = AsyncMock(return_value=("sent", None))
    monkeypatch.setattr(pr, "send_email", sent)
    monkeypatch.setattr(pr, "is_pwned", lambda pw: False)
    monkeypatch.setenv("FRONTEND_URL", "https://books.example.com")
    return SimpleNamespace(users=users, user=user, resets=resets,
                           requests=requests, attempts=attempts,
                           audit=audit, sent=sent)


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"))


def _ask(email=USER["email"]):
    return asyncio.run(pr.request_reset(pr.RequestBody(email=email), _request()))


def _raw_token_from_email(env):
    """The raw token as the user receives it — parsed out of the email."""
    body = env.sent.call_args[0][2]
    for line in body.splitlines():
        if "token=" in line:
            return line.split("token=", 1)[1].strip()
    raise AssertionError("no reset link in email body")


# ------------------------------------------------------------------ tests

def test_request_returns_uniform_message_and_emails_link(env):
    out = _ask()
    assert out == {"ok": True, "message": pr.UNIFORM_MESSAGE}
    env.sent.assert_awaited_once()
    to, subject, _body = env.sent.call_args[0]
    assert to == USER["email"]
    raw = _raw_token_from_email(env)
    assert f"https://books.example.com/reset-password?token={raw}" in _body


def test_token_stored_hashed_with_one_hour_expiry(env):
    _ask()
    raw = _raw_token_from_email(env)
    assert len(env.resets.docs) == 1
    doc = env.resets.docs[0]
    assert doc["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in str(doc)  # the raw secret is nowhere in storage
    assert doc["expires_at"] - doc["created_at"] == timedelta(hours=1)
    assert doc["used_at"] is None and doc["invalidated"] is False


def test_unknown_email_gets_identical_response_and_no_token(env):
    out = _ask("ghost@nowhere.example.com")
    assert out == {"ok": True, "message": pr.UNIFORM_MESSAGE}
    assert env.resets.docs == []
    env.sent.assert_not_awaited()


def test_new_request_invalidates_previous_token(env):
    _ask()
    raw_first = _raw_token_from_email(env)
    _ask()
    raw_second = _raw_token_from_email(env)
    assert raw_first != raw_second
    with pytest.raises(HTTPException) as e:
        asyncio.run(pr.peek_reset(raw_first))
    assert e.value.status_code == 400
    assert asyncio.run(pr.peek_reset(raw_second)) == {"valid": True}


def test_expired_token_rejected(env):
    _ask()
    raw = _raw_token_from_email(env)
    env.resets.docs[0]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1))
    with pytest.raises(HTTPException) as e:
        asyncio.run(pr.confirm_reset(
            pr.ConfirmBody(token=raw, password="new-password-456")))
    assert e.value.status_code == 400
    assert e.value.detail == pr.INVALID_LINK


def test_unknown_token_rejected_with_generic_message(env):
    with pytest.raises(HTTPException) as e:
        asyncio.run(pr.confirm_reset(
            pr.ConfirmBody(token="not-a-real-token",
                           password="new-password-456")))
    assert e.value.status_code == 400
    assert e.value.detail == pr.INVALID_LINK


def test_confirm_changes_password_and_kills_token_and_sessions(env):
    old_hash = env.user["password_hash"]
    _ask()
    raw = _raw_token_from_email(env)
    out = asyncio.run(pr.confirm_reset(
        pr.ConfirmBody(token=raw, password="new-password-456")))
    assert out == {"ok": True}
    # The password really changed.
    assert env.user["password_hash"] != old_hash
    assert verify_password("new-password-456", env.user["password_hash"])
    assert not verify_password("old-password-123", env.user["password_hash"])
    # Sessions: token_version bumped, so JWTs issued before the reset die
    # in get_current_user ("Session revoked") and fresh ones validate.
    assert env.user["token_version"] == USER["token_version"] + 1
    stale = create_access_token(USER["id"], USER["email"], USER["token_version"])
    fresh = create_access_token(USER["id"], USER["email"],
                                USER["token_version"] + 1)

    async def _as(token):
        req = SimpleNamespace(cookies={"access_token": token}, headers={})
        return await get_current_user(req)

    with pytest.raises(HTTPException) as e:
        asyncio.run(_as(stale))
    assert e.value.status_code == 401
    assert asyncio.run(_as(fresh))["id"] == USER["id"]
    # Failed-login history cleared; token is single-use now.
    assert env.attempts.docs == []
    with pytest.raises(HTTPException) as e2:
        asyncio.run(pr.confirm_reset(
            pr.ConfirmBody(token=raw, password="another-password-789")))
    assert e2.value.status_code == 400
    # And the reset was audited.
    assert any(d["action"] == "auth.password_reset" for d in env.audit.docs)


def test_confirm_rejects_breached_password_without_burning_token(env, monkeypatch):
    _ask()
    raw = _raw_token_from_email(env)
    monkeypatch.setattr(pr, "is_pwned", lambda pw: True)
    with pytest.raises(HTTPException) as e:
        asyncio.run(pr.confirm_reset(
            pr.ConfirmBody(token=raw, password="breached-password-1")))
    assert e.value.status_code == 400
    assert "data breach" in e.value.detail
    assert env.resets.docs[0]["used_at"] is None  # link still usable


def test_confirm_reuses_registration_password_rules(env):
    with pytest.raises(Exception):
        pr.ConfirmBody(token="x", password="short")
    with pytest.raises(Exception):
        pr.ConfirmBody(token="x", password="é" * 37)  # 74 UTF-8 bytes > 72
    ok = pr.ConfirmBody(token="x", password="a" * 12)
    assert ok.password == "a" * 12


def test_request_rate_limited_per_email(env):
    for _ in range(pr.MAX_PER_EMAIL):
        _ask()
    with pytest.raises(HTTPException) as e:
        _ask()
    assert e.value.status_code == 429
