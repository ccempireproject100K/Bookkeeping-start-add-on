"""Job budget watch — overrun alerts (owner-approved 2026-11-14, walk #9).

Direct-call tests: no mongod, no network. The alert engine and the
jobs-router settings/threshold/notification endpoints are awaited
directly with a fake user dict and an in-memory FakeDB monkeypatched
into the `db` attribute of the modules under test. (The add-cost
posting path itself is covered by the live drive in
~/workspace/buildout/job-overrun-alerts/proof/.)
"""
import asyncio
import copy
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib import jobalerts as JA
from routers import jobs as jobs_router

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}


def run(coro):
    return asyncio.run(coro)


# ============================ FakeDB ============================
_MISSING = object()


def _matches(doc, query):
    for key, cond in (query or {}).items():
        val = doc.get(key, _MISSING)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if op == "$in":
                    if val is _MISSING or val not in operand:
                        return False
                elif op == "$ne":
                    if val is not _MISSING and val == operand:
                        return False
                elif op == "$gte":
                    if val is _MISSING or not val >= operand:
                        return False
                elif op == "$lte":
                    if val is _MISSING or not val <= operand:
                        return False
                else:
                    raise NotImplementedError(f"FakeDB: operator {op}")
        else:
            if val is _MISSING or val != cond:
                return False
    return True


def _apply_update(doc, update):
    for op, fields in (update or {}).items():
        if op == "$set":
            for k, v in fields.items():
                doc[k] = copy.deepcopy(v)
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = (doc.get(k) or 0) + v
        elif op == "$push":
            for k, v in fields.items():
                doc.setdefault(k, []).append(copy.deepcopy(v))
        else:
            raise NotImplementedError(f"FakeDB: update operator {op}")


def _project(doc, projection):
    if not projection:
        return copy.deepcopy(doc)
    exc = [k for k, v in projection.items() if not v]
    out = copy.deepcopy(doc)
    for k in exc:
        out.pop(k, None)
    return out


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        items = [spec] if isinstance(spec, tuple) else list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: (d.get(field) is None, d.get(field)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return _project(d, projection)
        return None

    def find(self, query=None, projection=None):
        return FakeCursor([_project(d, projection) for d in self.docs
                           if _matches(d, query)])

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", uuid.uuid4().hex)
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))

    async def find_one_and_update(self, query, update, upsert=False,
                                  return_document=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return copy.deepcopy(d)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return copy.deepcopy(new)
        return None


class FakeDB:
    def __init__(self):
        object.__setattr__(self, "_colls", {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        colls = object.__getattribute__(self, "_colls")
        if name not in colls:
            colls[name] = FakeCollection(name)
        return colls[name]


# ============================ fixtures ============================
@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(JA, "db", fake)
    monkeypatch.setattr(jobs_router, "db", fake)
    return fake


def mk_job(status="active", est_cost_cents=100_000, **kw):
    job = {"id": kw.pop("id", "job-1"), "job_number": "JOB-0001",
           "customer_name": "Acme", "status": status,
           "estimated_revenue_cents": 200_000,
           "estimated_costs_cents": {"material": est_cost_cents},
           "change_orders": []}
    job.update(kw)
    return job


def profit(actual_cents, est_cents=100_000):
    return {"actual_cost_cents": actual_cents,
            "estimated_cost_cents": est_cents}


def notifs(env):
    return [d for d in env.app_notifications.docs
            if d.get("kind") == "job_budget_alert"]


def evaluate(env, job, prof, user=USER):
    return run(JA.evaluate_alerts(user["tenant_id"], job, prof,
                                  actor_id=user["id"]))


# ============================ crossing + dedupe ============================
def test_crossing_90_fires_once_then_dedupes(env):
    job = mk_job()
    res = evaluate(env, job, profit(95_000))
    assert [f["level"] for f in res["fired"]] == ["warning"]
    assert res["budget"]["pct_used"] == 95.0
    assert res["budget"]["level"] == "warning"
    assert len(notifs(env)) == 1
    n = notifs(env)[0]
    assert n["job_number"] == "JOB-0001" and n["threshold_pct"] == 90
    assert n["read"] is False and n["link"] == "/jobs"
    # More costs land, still under 100: no second warning (no nagging).
    res = evaluate(env, job, profit(97_000))
    assert res["fired"] == []
    assert len(notifs(env)) == 1


def test_crossing_100_fires_overrun_once(env):
    job = mk_job()
    evaluate(env, job, profit(95_000))
    res = evaluate(env, job, profit(101_000))
    assert [f["level"] for f in res["fired"]] == ["overrun"]
    assert res["budget"]["level"] == "overrun"
    assert len(notifs(env)) == 2
    res = evaluate(env, job, profit(120_000))
    assert res["fired"] == []
    assert len(notifs(env)) == 2


def test_jump_straight_over_fires_both_once(env):
    job = mk_job()
    res = evaluate(env, job, profit(150_000))
    assert [f["level"] for f in res["fired"]] == ["warning", "overrun"]
    assert len(notifs(env)) == 2


def test_below_threshold_is_silent(env):
    res = evaluate(env, mk_job(), profit(50_000))
    assert res["fired"] == [] and res["budget"]["level"] is None
    assert notifs(env) == []


# ============================ correction + re-cross ============================
def test_correction_clears_and_recross_fires_again(env):
    job = mk_job()
    evaluate(env, job, profit(95_000))
    assert len(notifs(env)) == 1
    # Costs corrected back under the threshold (e.g. a change order
    # raised the estimate): the fired mark clears, silently.
    res = evaluate(env, job, profit(95_000, est_cents=200_000))
    assert res["fired"] == [] and len(notifs(env)) == 1
    # Crossing again is a NEW crossing: it may fire again.
    res = evaluate(env, job, profit(190_000, est_cents=200_000))
    assert [f["level"] for f in res["fired"]] == ["warning"]
    assert len(notifs(env)) == 2


# ============================ honest no-estimate state ============================
def test_no_estimate_no_machinery(env):
    job = mk_job(est_cost_cents=0)
    res = evaluate(env, job, profit(50_000, est_cents=0))
    assert res["budget"]["has_estimate"] is False
    assert res["budget"]["pct_used"] is None
    assert res["budget"]["level"] is None
    assert res["fired"] == [] and notifs(env) == []
    assert env.job_alert_state.docs == [] or \
        not env.job_alert_state.docs[0].get("fired")


# ============================ status rules ============================
def test_warning_needs_open_job_but_overrun_fires_when_completed(env):
    job = mk_job(status="completed")
    res = evaluate(env, job, profit(95_000))
    assert res["fired"] == []           # warning is for open jobs only
    res = evaluate(env, job, profit(105_000))
    assert [f["level"] for f in res["fired"]] == ["overrun"]


def test_closed_job_is_silent(env):
    job = mk_job(status="closed")
    res = evaluate(env, job, profit(150_000))
    assert res["fired"] == [] and notifs(env) == []


# ============================ tenant config ============================
def test_tenant_threshold_config_applies(env):
    run(JA.save_config(TID, 50, 75, True))
    job = mk_job()
    res = evaluate(env, job, profit(60_000))
    assert [f["level"] for f in res["fired"]] == ["warning"]
    assert res["budget"]["warning_pct"] == 50
    res = evaluate(env, job, profit(80_000))
    assert [f["level"] for f in res["fired"]] == ["overrun"]


def test_tenant_config_is_isolated(env):
    run(JA.save_config(TID, 50, 75, True))
    job = mk_job()
    res = evaluate(env, job, profit(60_000), user=USER2)
    assert res["fired"] == []  # other tenant still on 90/100 defaults
    assert res["budget"]["warning_pct"] == 90


def test_disabled_lane_is_silent_then_fires_when_enabled(env):
    run(JA.save_config(TID, 90, 100, False))
    job = mk_job()
    res = evaluate(env, job, profit(95_000))
    assert res["fired"] == [] and notifs(env) == []
    # Budget state still computed for the UI flag while the lane is off.
    assert res["budget"]["level"] == "warning"
    run(JA.save_config(TID, 90, 100, True))
    res = evaluate(env, job, profit(96_000))
    assert [f["level"] for f in res["fired"]] == ["warning"]


def test_settings_endpoint_roundtrip_and_validation(env):
    cfg = run(jobs_router.get_alert_settings(USER))
    assert cfg["warning_pct"] == 90 and cfg["overrun_pct"] == 100
    assert cfg["enabled"] is True
    out = run(jobs_router.update_alert_settings(
        jobs_router.AlertSettingsBody(warning_pct=80, overrun_pct=110,
                                      enabled=True), USER))
    assert out["warning_pct"] == 80 and out["overrun_pct"] == 110
    with pytest.raises(HTTPException) as ei:
        run(jobs_router.update_alert_settings(
            jobs_router.AlertSettingsBody(warning_pct=110, overrun_pct=100),
            USER))
    assert ei.value.status_code == 422


# ============================ per-job override ============================
def test_per_job_override_beats_tenant(env):
    run(JA.save_config(TID, 90, 100, True))
    job = mk_job(alert_warning_pct=40, alert_overrun_pct=60)
    res = evaluate(env, job, profit(50_000))
    assert [f["level"] for f in res["fired"]] == ["warning"]
    assert res["budget"]["warning_pct"] == 40


def test_job_thresholds_endpoint_sets_override(env):
    job = mk_job()
    env.jobs.docs.append({**job, "tenant_id": TID})
    out = run(jobs_router.update_job_thresholds(
        "job-1", jobs_router.JobThresholdsBody(alert_warning_pct=95,
                                               alert_overrun_pct=120),
        USER))
    assert out["alert_warning_pct"] == 95 and out["alert_overrun_pct"] == 120
    stored = env.jobs.docs[0]
    assert stored["alert_warning_pct"] == 95


# ============================ notifications surface ============================
def test_notifications_list_and_read(env):
    job = mk_job()
    evaluate(env, job, profit(95_000))
    out = run(jobs_router.list_job_notifications(USER))
    assert out["unread"] == 1 and len(out["notifications"]) == 1
    nid = out["notifications"][0]["id"]
    run(jobs_router.mark_job_notification_read(nid, USER))
    out = run(jobs_router.list_job_notifications(USER))
    assert out["unread"] == 0
    # Tenant isolation: the other tenant sees nothing.
    out2 = run(jobs_router.list_job_notifications(USER2))
    assert out2["notifications"] == [] and out2["unread"] == 0


# ============================ router integration via _profit ============================
def test_evaluate_through_jobs_profit(env):
    job = mk_job()
    env.jobs.docs.append({**job, "tenant_id": TID})
    env.job_costs.docs.append(
        {"id": "c1", "tenant_id": TID, "job_id": "job-1",
         "category": "material", "amount_cents": 92_000})
    stored = run(jobs_router.jobs(USER).find_one({"id": "job-1"}))
    profit_res, res = run(jobs_router._evaluate(USER, stored))
    assert profit_res["actual_cost_cents"] == 92_000
    assert [f["level"] for f in res["fired"]] == ["warning"]
    assert len(notifs(env)) == 1
