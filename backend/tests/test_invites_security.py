"""Invite-token storage: hash at rest, raw token only in the link.

Security audit 2026-10-26: portal links and password-reset tokens in
this codebase store only a SHA-256 of the secret ("a database read
never yields a usable link"), but invitations stored the raw token —
anyone with a database read held live workspace invites. These tests
pin the fixed behavior, including that invites issued before the fix
(plaintext rows) keep working until accepted or revoked.

Run from the backend directory with:
    python3 -m pytest tests/test_invites_security.py -n 0 -q
"""
import asyncio
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "testdb")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest
from fastapi import HTTPException, Response

from db import db
from routers import invites as INV


# ------------------------------------------------------------------ fakes

def _matches(doc, query):
    for key, cond in (query or {}).items():
        val = doc.get(key)
        if isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$ne" and not (val != operand):
                    return False
                if op == "$in" and val not in operand:
                    return False
        elif val != cond:
            return False
    return True


def _project(doc, projection):
    out = dict(doc)
    if projection:
        for k, v in projection.items():
            if v == 0:
                out.pop(k, None)
    return out


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=-1):
        self._docs = sorted(self._docs,
                            key=lambda d: d.get(key) or "",
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return self._docs


class FakeColl:
    def __init__(self):
        self.docs = []

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return _project(d, projection)
        return None

    def find(self, query=None, projection=None):
        return _Cursor([_project(d, projection) for d in self.docs
                        if _matches(d, query)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, query, update):
        n = 0
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                n += 1
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _matches(d, query))


OWNER = {"id": "owner-1", "tenant_id": "tenant-1",
         "email": "owner@shop.example.com", "name": "Owner",
         "role": "owner"}


@pytest.fixture()
def env(monkeypatch):
    invitations = FakeColl()
    users = FakeColl()
    tenants = FakeColl()
    tenants.docs.append({"id": "tenant-1", "name": "Acme Plumbing"})
    audit = FakeColl()
    monkeypatch.setattr(db, "invitations", invitations)
    monkeypatch.setattr(db, "users", users)
    monkeypatch.setattr(db, "tenants", tenants)
    monkeypatch.setattr(db, "audit_log", audit)
    sent = AsyncMock(return_value=("sent", None))
    monkeypatch.setattr(INV, "send_email", sent)
    monkeypatch.setattr(INV, "is_pwned", lambda pw: False)
    monkeypatch.setenv("FRONTEND_URL", "https://books.example.com")
    return SimpleNamespace(invitations=invitations, users=users,
                           tenants=tenants, sent=sent)


def _create(email="pro@tax.example.com", role="accountant"):
    return asyncio.run(INV.create_invite(
        INV.InviteIn(email=email, role=role), dict(OWNER)))


def _raw_from(out):
    return out["accept_url"].split("token=", 1)[1]


# ------------------------------------------------------------------ tests

def test_invite_token_stored_hashed_only(env):
    out = _create()
    raw = _raw_from(out)
    assert len(env.invitations.docs) == 1
    doc = env.invitations.docs[0]
    assert doc["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert "token" not in doc  # no raw-token field at all
    assert raw not in str(doc)  # and the secret appears nowhere in storage
    assert "token_hash" not in out  # the hash is not API surface either


def test_peek_resolves_raw_token(env):
    raw = _raw_from(_create())
    out = asyncio.run(INV.peek_invite(raw))
    assert out == {"email": "pro@tax.example.com", "role": "accountant",
                   "tenant_name": "Acme Plumbing"}
    with pytest.raises(HTTPException) as e:
        asyncio.run(INV.peek_invite("not-a-real-token"))
    assert e.value.status_code == 404


def test_accept_with_raw_token_creates_account_once(env):
    raw = _raw_from(_create())
    out = asyncio.run(INV.accept_invite(
        INV.AcceptIn(token=raw, name="Pat Pro",
                     password="invite-password-123"), Response()))
    assert out["email"] == "pro@tax.example.com"
    assert out["role"] == "accountant"
    assert "password_hash" not in out
    user = env.users.docs[0]
    assert user["memberships"] == [
        {"tenant_id": "tenant-1", "role": "accountant"}]
    assert env.invitations.docs[0]["status"] == "accepted"
    # Single-use: the same link cannot be accepted twice.
    with pytest.raises(HTTPException) as e:
        asyncio.run(INV.accept_invite(
            INV.AcceptIn(token=raw, name="Pat Pro",
                         password="invite-password-123"), Response()))
    assert e.value.status_code == 400
    assert len(env.users.docs) == 1


def test_accept_existing_user_gains_membership(env):
    env.users.docs.append({
        "id": "u-9", "tenant_id": "tenant-other",
        "email": "pro@tax.example.com", "name": "Pat", "role": "owner",
        "password_hash": "x", "token_version": 0,
        "memberships": [{"tenant_id": "tenant-other", "role": "owner"}]})
    raw = _raw_from(_create(role="bookkeeper"))
    asyncio.run(INV.accept_invite(
        INV.AcceptIn(token=raw, name="Pat", password=""), Response()))
    user = env.users.docs[0]
    assert {"tenant_id": "tenant-1", "role": "bookkeeper"} \
        in user["memberships"]
    assert len(env.users.docs) == 1  # no duplicate account created


def test_legacy_plaintext_invite_still_works(env):
    """Invites issued before hash-at-rest carry a raw `token` field;
    they must keep working until accepted or revoked."""
    env.invitations.docs.append({
        "id": "inv-legacy", "token": "legacy-raw-token",
        "tenant_id": "tenant-1", "email": "old@tax.example.com",
        "role": "accountant", "invited_by": "owner-1",
        "status": "pending", "created_at": "2026-10-20T00:00:00",
        "accepted_at": None})
    out = asyncio.run(INV.peek_invite("legacy-raw-token"))
    assert out["email"] == "old@tax.example.com"
    asyncio.run(INV.accept_invite(
        INV.AcceptIn(token="legacy-raw-token", name="Olga",
                     password="legacy-password-123"), Response()))
    assert env.invitations.docs[0]["status"] == "accepted"
    assert env.users.docs[0]["email"] == "old@tax.example.com"


def test_list_invites_exposes_no_token_material(env):
    _create()
    rows = asyncio.run(INV.list_invites(dict(OWNER)))
    assert len(rows) == 1
    assert "token" not in rows[0]
    assert "token_hash" not in rows[0]
