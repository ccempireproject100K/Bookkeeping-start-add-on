"""Regression coverage for customer workspace creation during public signup."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from routers import auth


def setup_storage(monkeypatch, *, existing_user=None):
    users = SimpleNamespace(
        find_one=AsyncMock(return_value=existing_user),
        insert_one=AsyncMock(),
    )
    tenants = SimpleNamespace(insert_one=AsyncMock())
    monkeypatch.setattr(auth.db, "users", users)
    monkeypatch.setattr(auth.db, "tenants", tenants)
    monkeypatch.setattr(auth, "seed_coa", AsyncMock())
    monkeypatch.setattr(auth, "hash_password", lambda value: "hashed-for-test")
    return users, tenants


def test_separate_signups_get_separate_tenants(monkeypatch):
    users, tenants = setup_storage(monkeypatch)
    ids = iter(["tenant-one", "user-one", "tenant-two", "user-two"])
    monkeypatch.setattr(auth, "new_id", lambda: next(ids))

    first = asyncio.run(auth.create_workspace(
        "First@Example.com", "secure-password-one", "First", "First Business"
    ))
    second = asyncio.run(auth.create_workspace(
        "Second@Example.com", "secure-password-two", "Second", "Second Business"
    ))

    assert first["tenant_id"] == "tenant-one"
    assert second["tenant_id"] == "tenant-two"
    assert first["tenant_id"] != second["tenant_id"]
    assert first["email"] == "first@example.com"
    assert second["email"] == "second@example.com"
    assert tenants.insert_one.await_count == 2
    assert users.insert_one.await_count == 2
    assert first["role"] == second["role"] == "owner"


def test_duplicate_email_does_not_create_workspace(monkeypatch):
    users, tenants = setup_storage(monkeypatch, existing_user={"id": "existing"})
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.create_workspace(
            " EXISTING@EXAMPLE.COM ", "secure-password", "Someone", "Other Business"
        ))
    assert error.value.status_code == 409
    users.find_one.assert_awaited_once_with({"email": "existing@example.com"})
    tenants.insert_one.assert_not_awaited()
    users.insert_one.assert_not_awaited()
