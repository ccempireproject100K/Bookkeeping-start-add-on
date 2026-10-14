"""Regression tests for the public application's demo-only role switch.

Run from the backend directory with: python -m pytest tests/test_role_switch_security.py
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response

from routers import auth


def run_switch(monkeypatch, *, flag, tenant, role="owner"):
    if flag is None:
        monkeypatch.delenv("ENABLE_DEMO_ROLE_SWITCH", raising=False)
    else:
        monkeypatch.setenv("ENABLE_DEMO_ROLE_SWITCH", flag)

    find_tenant = AsyncMock(return_value=tenant)
    find_user = AsyncMock(return_value={
        "id": "target", "tenant_id": "workspace-1",
        "email": "owner@example.test", "role": role,
    })
    monkeypatch.setattr(auth.db, "tenants", SimpleNamespace(find_one=find_tenant))
    monkeypatch.setattr(auth.db, "users", SimpleNamespace(find_one=find_user))
    issue = AsyncMock()
    monkeypatch.setattr(auth, "_issue", issue)
    user = {"id": "customer", "tenant_id": "workspace-1", "role": "staff"}
    action = auth.switch_role(auth.SwitchBody(role=role), Response(), user)
    return action, find_tenant, find_user, issue


@pytest.mark.parametrize("flag", [None, "", "false", "TRUE-ish"])
def test_role_switch_disabled_by_default(monkeypatch, flag):
    action, find_tenant, find_user, issue = run_switch(
        monkeypatch, flag=flag, tenant={"access": "demo"}
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(action)
    assert error.value.status_code == 403
    find_tenant.assert_not_awaited()
    find_user.assert_not_awaited()
    issue.assert_not_awaited()


@pytest.mark.parametrize("tenant", [None, {}, {"access": "production"}])
def test_role_switch_rejects_non_demo_workspace(monkeypatch, tenant):
    action, find_tenant, find_user, issue = run_switch(
        monkeypatch, flag="true", tenant=tenant
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(action)
    assert error.value.status_code == 403
    find_tenant.assert_awaited_once_with(
        {"id": "workspace-1"}, {"_id": 0, "access": 1}
    )
    find_user.assert_not_awaited()
    issue.assert_not_awaited()


def test_role_switch_allowed_only_in_opted_in_demo(monkeypatch):
    action, find_tenant, find_user, issue = run_switch(
        monkeypatch, flag="true", tenant={"access": "demo"}
    )
    result = asyncio.run(action)
    assert result["role"] == "owner"
    assert result["tenant_id"] == "workspace-1"
    find_user.assert_awaited_once_with(
        {"tenant_id": "workspace-1", "role": "owner"}
    )
    issue.assert_awaited_once()
