"""Service-shop packaging: plan tiers, entitlements, trial, API enforcement.

No live MongoDB needed (db handles are mocked). Run from the backend dir:
    MONGO_URL=mongodb://localhost:27017 DB_NAME=test python -m pytest tests/test_plans.py

Covers:
  - plan defaults at signup + 30-day Medium trial fields
  - effective plan (trial active -> medium; expired -> stored plan)
  - legacy tenants (no plan key) grandfathered at medium
  - per-feature gating: 402 + upgrade payload when plan is too low
  - tenant isolation of plan enforcement
  - /plans/current payload shape, /plans/change owner-only + audit
"""
import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from lib import plans
from lib.plans import (
    PLANS, PLAN_INFO, FEATURE_PLANS, TRIAL_DAYS,
    validate_plan, stored_plan, trial_active, effective_plan,
    trial_days_remaining, plan_includes, features_for, upgrade_detail,
    new_trial_fields, require_feature,
)
import routers.plans as plans_router
import routers.auth as auth_router

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _user(tenant_id="t1", role="owner", uid="u1"):
    return {"id": uid, "tenant_id": tenant_id, "role": role, "name": "Owner"}


def _tenant(plan="light", trial="active"):
    t = {"id": "t1", "name": "Shop"}
    if plan is not None:
        t["plan"] = plan
    if trial == "active":
        t["trial_ends_at"] = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    elif trial == "expired":
        t["trial_ends_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    return t


def _db_with(tenant):
    tenants = SimpleNamespace(find_one=AsyncMock(return_value=tenant),
                              update_one=AsyncMock())
    audit_log = SimpleNamespace(insert_one=AsyncMock())
    users = SimpleNamespace(find_one=AsyncMock(return_value=None),
                            insert_one=AsyncMock())
    return SimpleNamespace(tenants=tenants, audit_log=audit_log, users=users)


# ---------------- plan primitives ----------------

class TestPlanPrimitives:
    def test_validate_plan_ok(self):
        assert validate_plan("medium") == "medium"
        assert validate_plan(" Heavy ") == "heavy"

    def test_validate_plan_bad(self):
        with pytest.raises(HTTPException) as e:
            validate_plan("platinum")
        assert e.value.status_code == 422

    def test_stored_plan_defaults(self):
        assert stored_plan({"plan": "heavy"}) == "heavy"

    def test_legacy_tenant_grandfathered_medium(self):
        assert stored_plan({}) == "medium"

    def test_trial_active(self):
        assert trial_active(_tenant(trial="active")) is True
        assert trial_active(_tenant(trial="expired")) is False
        assert trial_active({}) is False

    def test_trial_fields_30_days(self):
        f = new_trial_fields()
        assert f["plan"] == "light"
        end = datetime.fromisoformat(f["trial_ends_at"])
        days = (end - datetime.now(timezone.utc)).days
        assert TRIAL_DAYS - 1 <= days <= TRIAL_DAYS

    def test_effective_plan_trial_overrides(self):
        assert effective_plan(_tenant("light", "active")) == "medium"
        assert effective_plan(_tenant("heavy", "active")) == "medium"

    def test_effective_plan_downgrades_at_expiry(self):
        assert effective_plan(_tenant("light", "expired")) == "light"
        assert effective_plan(_tenant("heavy", "expired")) == "heavy"

    def test_trial_days_remaining(self):
        assert 0 < trial_days_remaining(_tenant(trial="active")) <= TRIAL_DAYS
        assert trial_days_remaining(_tenant(trial="expired")) == 0
        assert trial_days_remaining({}) == 0


class TestEntitlementMatrix:
    def test_all_plans_known_features(self):
        for feature, need in FEATURE_PLANS.items():
            assert need in PLANS, feature

    def test_light_basics(self):
        for f in ("invoicing", "expenses", "basic_reports", "receipt_capture",
                  "light_tax", "time_tracking", "client_portal"):
            assert plan_includes("light", f) is True, f

    def test_medium_additions(self):
        for f in ("bank_feeds", "copilot_actions", "collections_automation",
                  "advanced_reports", "prep_1099"):
            assert plan_includes("medium", f) is True, f
            assert plan_includes("light", f) is False, f

    def test_heavy_additions(self):
        for f in ("multi_entity", "white_label", "api_access"):
            assert plan_includes("heavy", f) is True, f
            assert plan_includes("medium", f) is False, f

    def test_features_for_lists_labels(self):
        feats = features_for("light")
        assert "invoicing" in feats and "bank_feeds" not in feats
        assert all(feats.values())
        assert len(features_for("heavy")) == len(FEATURE_PLANS)

    def test_upgrade_detail_payload(self):
        d = upgrade_detail("light", "bank_feeds")
        assert d["code"] == "upgrade_required"
        assert d["current_plan"] == "light"
        assert d["required_plan"] == "medium"
        assert d["required_plan_name"] == PLAN_INFO["medium"]["name"]
        assert "upgrade" in d["message"].lower()


# ---------------- require_feature enforcement ----------------

class TestRequireFeature:
    def test_allows_when_included(self):
        fake = _db_with(_tenant("light", "expired"))
        with patch.object(plans, "db", fake):
            dep = require_feature("invoicing")
            out = _run(dep(_user()))
        assert out["tenant_id"] == "t1"
        fake.tenants.find_one.assert_called_once()

    def test_402_with_upgrade_payload_when_locked(self):
        fake = _db_with(_tenant("light", "expired"))
        with patch.object(plans, "db", fake):
            dep = require_feature("bank_feeds")
            with pytest.raises(HTTPException) as e:
                _run(dep(_user()))
        assert e.value.status_code == 402
        assert e.value.detail["code"] == "upgrade_required"
        assert e.value.detail["required_plan"] == "medium"

    def test_trial_unlocks_medium_features(self):
        fake = _db_with(_tenant("light", "active"))
        with patch.object(plans, "db", fake):
            dep = require_feature("copilot_actions")
            assert _run(dep(_user()))["tenant_id"] == "t1"

    def test_tenant_isolation(self):
        # Enforcement is evaluated per tenant: the tenant lookup must be scoped
        # to the caller's tenant, so one workspace's plan never leaks to another.
        med = _db_with(_tenant("medium", "expired"))
        light = _db_with(_tenant("light", "expired"))
        dep = require_feature("multi_entity")  # heavy-only: both tenants locked
        with patch.object(plans, "db", med):
            with pytest.raises(HTTPException) as e1:
                _run(dep(_user(tenant_id="t-med")))
        med.tenants.find_one.assert_called_with({"id": "t-med"}, {"_id": 0})
        with patch.object(plans, "db", light):
            with pytest.raises(HTTPException) as e2:
                _run(dep(_user(tenant_id="t-light")))
        light.tenants.find_one.assert_called_with({"id": "t-light"}, {"_id": 0})
        assert e1.value.status_code == e2.value.status_code == 402

    def test_heavy_passes_multi_entity(self):
        fake = _db_with(_tenant("heavy", "expired"))
        with patch.object(plans, "db", fake):
            dep = require_feature("multi_entity")
            assert _run(dep(_user()))["tenant_id"] == "t1"


# ---------------- /plans router ----------------

class TestPlansRouter:
    def test_current_plan_payload(self):
        fake = _db_with(_tenant("light", "active"))
        with patch.object(plans_router, "db", fake):
            out = _run(plans_router.current_plan(_user()))
        assert out["plan"] == "light"
        assert out["effective_plan"] == "medium"   # trial active
        assert out["trial_active"] is True
        assert 0 < out["trial_days_remaining"] <= TRIAL_DAYS
        assert "bank_feeds" in out["features"]     # trial unlocks medium
        assert set(out["plans"]) == {"light", "medium", "heavy"}
        assert out["features_by_plan"]["heavy"]["multi_entity"]

    def test_change_plan_owner(self):
        fake = _db_with(_tenant("light", "expired"))
        body = SimpleNamespace(plan="Medium")
        with patch.object(plans_router, "db", fake):
            out = _run(plans_router.change_plan(body, SimpleNamespace(client=None),
                                                _user(role="owner")))
        assert out["plan"] == "medium"
        fake.tenants.update_one.assert_called_once()
        assert fake.tenants.update_one.call_args[0][1] == {"$set": {"plan": "medium"}}
        fake.audit_log.insert_one.assert_called_once()
        audit = fake.audit_log.insert_one.call_args[0][0]
        assert audit["action"] == "plan.changed"
        assert audit["details"]["plan"] == "medium"

    def test_change_plan_rejects_unknown(self):
        fake = _db_with(_tenant("light", "expired"))
        body = SimpleNamespace(plan="platinum")
        with patch.object(plans_router, "db", fake):
            with pytest.raises(HTTPException) as e:
                _run(plans_router.change_plan(body, SimpleNamespace(client=None),
                                              _user(role="owner")))
        assert e.value.status_code == 422
        fake.tenants.update_one.assert_not_called()


# ---------------- signup stamps plan + trial ----------------

class TestRegisterPlan:
    def test_create_workspace_stamps_plan_and_trial(self):
        seen = {}

        async def fake_insert(doc):
            seen.update(doc)

        fake = _db_with({})
        fake.accounts = SimpleNamespace()  # Scoped(db.accounts, tenant_id)
        fake.tenants.insert_one = AsyncMock(side_effect=fake_insert)
        with patch.object(auth_router, "db", fake), \
             patch.object(auth_router, "seed_coa", AsyncMock()), \
             patch("lib.niches.get_pack", return_value={"id": "hvac"}):
            user = _run(auth_router.create_workspace(
                "new@shop.com", "StrongPass1!", "New Owner", "New Shop"))
        assert user["role"] == "owner"
        assert seen["plan"] == "light"
        assert seen["trial_ends_at"] is not None
        assert trial_active(seen) is True
        assert effective_plan(seen) == "medium"

    def test_create_workspace_explicit_plan(self):
        seen = {}

        async def fake_insert(doc):
            seen.update(doc)

        fake = _db_with({})
        fake.accounts = SimpleNamespace()  # Scoped(db.accounts, tenant_id)
        fake.tenants.insert_one = AsyncMock(side_effect=fake_insert)
        with patch.object(auth_router, "db", fake), \
             patch.object(auth_router, "seed_coa", AsyncMock()), \
             patch("lib.niches.get_pack", return_value={"id": "hvac"}):
            _run(auth_router.create_workspace(
                "heavy@shop.com", "StrongPass1!", "Heavy Owner", "Heavy Shop",
                plan="heavy"))
        assert seen["plan"] == "heavy"
        assert effective_plan(seen) == "medium"  # trial still applies first
        # but after expiry the purchased heavy plan governs
        seen["trial_ends_at"] = (datetime.now(timezone.utc)
                                 - timedelta(days=1)).isoformat()
        assert effective_plan(seen) == "heavy"

    def test_create_workspace_rejects_bad_plan(self):
        fake = _db_with({})
        with patch.object(auth_router, "db", fake), \
             patch.object(auth_router, "seed_coa", AsyncMock()), \
             patch("lib.niches.get_pack", return_value={"id": "hvac"}):
            with pytest.raises(HTTPException) as e:
                _run(auth_router.create_workspace(
                    "bad@shop.com", "StrongPass1!", "Bad", "Bad Shop", plan="platinum"))
        assert e.value.status_code == 422
