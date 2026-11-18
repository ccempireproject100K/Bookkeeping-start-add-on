"""Job-costing #10 — estimate -> job budget + cost-to-complete forecast
(owner-approved 2026-11-14, job-costing requests report item #10).

Direct-call tests: no mongod, no network. Endpoints and lib functions
are awaited directly with a fake user dict and an in-memory FakeDB
monkeypatched into the `db` attribute of every module under test
(house pattern, same as test_job_costing.py). The ledger posting seam
inside jobs.add_cost is stubbed; the double-entry engine has its own
coverage.
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

from lib import jobforecast as JF
from routers import estimates as est_router
from routers import jobs as jobs_router
from routers import job_forecast as fc_router

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

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

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
    # Merge-train (2026-11-14): the merged record_job_cost also consults
    # the job-costing family libs through their own db handles.
    from lib import jobalerts as JA, jobcosting as JC, jobphases as JP
    from lib import retainage as RT
    for mod in (JF, est_router, jobs_router, fc_router, JA, JC, JP, RT):
        monkeypatch.setattr(mod, "db", fake)
    return fake


@pytest.fixture()
def stub_ledger(monkeypatch):
    async def fake_post(db_, tenant_id, user, draft, status="posted", **kw):
        return {"number": "JE-TEST-1"}
    monkeypatch.setattr(jobs_router.L, "post_journal_entry", fake_post)
    return fake_post


def make_estimate(env, user=USER, lines=None, estimated_costs=None):
    body = est_router.EstimateBody(
        client_name="Acme HVAC",
        lines=lines or [est_router.EstLine(name="Install unit", amount=5000.0)],
        estimated_costs=estimated_costs or {})
    return run(est_router.create_estimate(body, user))


def seed_completed_job(env, tenant=TID, jid="hist-1", est=None, actual=None,
                       status="completed"):
    est = est or {}
    doc = {"id": jid, "tenant_id": tenant, "job_number": f"JOB-{jid}",
           "customer_name": "Hist", "status": status,
           "estimated_revenue_cents": 0,
           "estimated_costs_cents": {c: est.get(c, 0) for c in JF.CATEGORIES},
           "change_orders": [], "created_at": "2026-10-01T00:00:00"}
    env.jobs.docs.append(doc)
    for cat, amt in (actual or {}).items():
        env.job_costs.docs.append(
            {"id": uuid.uuid4().hex, "tenant_id": tenant, "job_id": jid,
             "category": cat, "amount_cents": amt,
             "created_at": "2026-10-02T00:00:00"})
    return doc


# ==================== conversion (core 1) ====================
def test_conversion_carries_figures_exactly(env):
    est = make_estimate(env, lines=[
        est_router.EstLine(name="Unit", amount=5000.0, category="material",
                           cost_amount=2100.55, phase="Rough-in"),
        est_router.EstLine(name="Crew", amount=3000.0, category="labor",
                           cost_amount=1450.25, phase="Rough-in"),
        est_router.EstLine(name="Sub", amount=1000.0, category="subcontractor",
                           cost_amount=800.0),
    ])
    job = run(est_router.convert_to_job(
        est["id"], est_router.ConvertJobBody(), USER))
    # exact cent carry — no re-key drift
    assert job["estimated_costs_cents"]["material"] == 210055
    assert job["estimated_costs_cents"]["labor"] == 145025
    assert job["estimated_costs_cents"]["subcontractor"] == 80000
    assert job["estimated_revenue_cents"] == 900000  # estimate subtotal
    # baseline snapshot == the estimate's own figures
    assert job["baseline_costs_cents"] == job["estimated_costs_cents"]
    assert job["baseline_estimate_id"] == est["id"]
    assert job["source_estimate_id"] == est["id"]
    assert job["status"] == "active"
    # phases grouped from lines, budget summed per category
    assert len(job["phases"]) == 1
    ph = job["phases"][0]
    assert ph["name"] == "Rough-in"
    assert ph["budget_cents"]["material"] == 210055
    assert ph["budget_cents"]["labor"] == 145025
    assert job["baseline_phases"][0]["budget_cents"] == ph["budget_cents"]
    # estimate now points at the job; second conversion refused
    stored = [e for e in env.estimates.docs if e["id"] == est["id"]][0]
    assert stored["job_id"] == job["id"]
    with pytest.raises(HTTPException) as ei:
        run(est_router.convert_to_job(
            est["id"], est_router.ConvertJobBody(), USER))
    assert ei.value.status_code == 409


def test_conversion_category_map_fallback(env):
    est = make_estimate(env, estimated_costs={"labor": 999.99, "other": 1.01})
    job = run(est_router.convert_to_job(
        est["id"], est_router.ConvertJobBody(), USER))
    assert job["estimated_costs_cents"]["labor"] == 99999
    assert job["estimated_costs_cents"]["other"] == 101
    assert job["phases"] == []


def test_revenue_only_estimate_converts_with_zero_budget(env):
    est = make_estimate(env)
    job = run(est_router.convert_to_job(
        est["id"], est_router.ConvertJobBody(), USER))
    assert all(v == 0 for v in job["estimated_costs_cents"].values())
    assert job["estimated_revenue_cents"] == 500000


# ==================== baseline immutability (A) ====================
def test_baseline_survives_budget_changes(env):
    est = make_estimate(env, estimated_costs={"labor": 1000.0})
    job = run(est_router.convert_to_job(
        est["id"], est_router.ConvertJobBody(), USER))
    # working budget moves (change order + manual edit); baseline must not
    stored = [j for j in env.jobs.docs if j["id"] == job["id"]][0]
    stored["estimated_costs_cents"]["labor"] = 555555
    stored["change_orders"] = [{"description": "x", "cost_delta_cents": 7000,
                                "approved": True}]
    fc = run(fc_router.get_forecast(job["id"], USER))
    assert fc["baseline"]["costs_cents"]["labor"] == 100000
    assert fc["baseline_total_cents"] == 100000
    row = [r for r in fc["rows"] if r["category"] == "labor"][0]
    assert row["budget_cents"] == 555555
    assert fc["change_order_delta_cents"] == 7000


# ==================== forecast math (C) ====================
def _seed_active_job(env, tenant=TID, jid="job-f", est=None, costs=None):
    doc = {"id": jid, "tenant_id": tenant, "job_number": "JOB-0100",
           "customer_name": "Fc", "status": "active",
           "estimated_revenue_cents": 0,
           "estimated_costs_cents": {c: (est or {}).get(c, 0)
                                     for c in JF.CATEGORIES},
           "change_orders": [], "created_at": "2026-11-01T00:00:00"}
    env.jobs.docs.append(doc)
    for cat, amt in (costs or {}).items():
        env.job_costs.docs.append(
            {"id": uuid.uuid4().hex, "tenant_id": tenant, "job_id": jid,
             "category": cat, "amount_cents": amt,
             "created_at": "2026-11-02T00:00:00"})
    return doc


def test_forecast_math_no_committed(env):
    job = _seed_active_job(env, est={"labor": 100000, "material": 50000},
                           costs={"labor": 40000})
    fc = run(fc_router.get_forecast(job["id"], USER))
    labor = [r for r in fc["rows"] if r["category"] == "labor"][0]
    assert labor["actual_cents"] == 40000
    assert labor["remaining_cents"] == 60000
    assert labor["forecast_cents"] == 100000
    assert fc["totals"]["forecast_cents"] == 150000
    assert fc["committed_enabled"] is False
    assert fc["totals"]["committed_cents"] == 0


def test_forecast_remaining_floors_at_zero_on_overrun(env):
    job = _seed_active_job(env, est={"labor": 100000}, costs={"labor": 130000})
    fc = run(fc_router.get_forecast(job["id"], USER))
    labor = [r for r in fc["rows"] if r["category"] == "labor"][0]
    assert labor["remaining_cents"] == 0
    assert labor["forecast_cents"] == 130000
    assert labor["drift_cents"] == 30000
    assert fc["drift_drivers"][0]["category"] == "labor"


def test_forecast_committed_seam_when_lane_enabled(env):
    job = _seed_active_job(env, est={"material": 100000, "subcontractor": 80000},
                           costs={"material": 10000})
    env.job_costing_config.docs.append(
        {"tenant_id": TID, "key": "job_costing", "committed_costs_enabled": True})
    env.purchase_orders.docs.append(
        {"id": "po-1", "tenant_id": TID, "job_id": job["id"],
         "status": "approved",
         "lines": [{"quantity": 2, "unit_cost_cents": 15000}]})
    env.job_commitments.docs.append(
        {"id": "jc-1", "tenant_id": TID, "job_id": job["id"],
         "status": "approved", "amount_cents": 50000, "fulfilled_cents": 5000})
    fc = run(fc_router.get_forecast(job["id"], USER))
    assert fc["committed_enabled"] is True
    mat = [r for r in fc["rows"] if r["category"] == "material"][0]
    sub = [r for r in fc["rows"] if r["category"] == "subcontractor"][0]
    assert mat["committed_cents"] == 30000
    assert mat["remaining_cents"] == 60000
    assert mat["forecast_cents"] == 100000
    assert sub["committed_cents"] == 45000
    assert sub["forecast_cents"] == 80000
    assert fc["totals"]["committed_cents"] == 75000


def test_forecast_phase_rows(env):
    job = _seed_active_job(env, est={"labor": 100000})
    stored = env.jobs.docs[0]
    stored["phases"] = [{"id": "ph-1", "name": "Rough-in",
                         "budget_cents": {"labor": 60000}}]
    env.job_costs.docs.append(
        {"id": "c-ph", "tenant_id": TID, "job_id": job["id"],
         "category": "labor", "amount_cents": 25000, "phase_id": "ph-1",
         "created_at": "2026-11-03T00:00:00"})
    fc = run(fc_router.get_forecast(job["id"], USER))
    assert len(fc["phases"]) == 1
    ph = fc["phases"][0]
    assert ph["budget_cents"] == 60000
    assert ph["actual_cents"] == 25000
    assert ph["forecast_cents"] == 60000


def test_totals_equal_sum_of_rows_when_category_overruns(env):
    # Regression (live drive, 2026-11-14): labor overruns its budget
    # while material still has remaining — the total forecast must be
    # the sum of the category forecasts, not a total-level recompute.
    job = _seed_active_job(env, est={"material": 200000, "labor": 112000,
                                     "subcontractor": 80000},
                           costs={"labor": 130000, "material": 50000})
    fc = run(fc_router.get_forecast(job["id"], USER))
    assert fc["totals"]["forecast_cents"] == \
        sum(r["forecast_cents"] for r in fc["rows"]) == 410000
    # baseline here = the seeded estimate (392000) -> drift 18000
    assert fc["totals"]["drift_cents"] == 18000


# ==================== suggestions (B) ====================
def test_suggestion_silent_under_three_jobs(env):
    for i in range(2):
        seed_completed_job(env, jid=f"h-{i}", est={"labor": 100000},
                           actual={"labor": 112000})
    est = make_estimate(env, estimated_costs={"labor": 1000.0})
    sugg = run(est_router.budget_suggestion(est["id"], USER))
    assert sugg["available"] is False
    assert sugg["per_category"] == {}


def test_suggestion_averages_and_evidence(env):
    # labor lands +10%, +12%, +14% over -> average +12%
    for i, act in enumerate((110000, 112000, 114000)):
        seed_completed_job(env, jid=f"h-{i}", est={"labor": 100000},
                           actual={"labor": act})
    est = make_estimate(env, estimated_costs={"labor": 1000.0, "material": 500.0})
    sugg = run(est_router.budget_suggestion(est["id"], USER))
    assert sugg["available"] is True
    lab = sugg["per_category"]["labor"]
    assert lab["jobs"] == 3
    assert lab["avg_variance_pct"] == 12.0
    assert lab["suggested_cents"] == 112000
    # material has no history -> silent, never fabricated
    assert "material" not in sugg["per_category"]


def test_suggestion_acceptance_recorded_baseline_untouched(env):
    for i, act in enumerate((110000, 112000, 114000)):
        seed_completed_job(env, jid=f"h-{i}", est={"labor": 100000},
                           actual={"labor": act})
    est = make_estimate(env, estimated_costs={"labor": 1000.0})
    job = run(est_router.convert_to_job(
        est["id"], est_router.ConvertJobBody(apply_suggestion=True), USER))
    assert job["estimated_costs_cents"]["labor"] == 112000   # working budget
    assert job["baseline_costs_cents"]["labor"] == 100000     # baseline intact
    adj = job["budget_adjustment"]
    assert adj["accepted"] is True
    assert adj["evidence"]["labor"]["jobs"] == 3
    assert adj["evidence"]["labor"]["avg_variance_pct"] == 12.0


def test_suggestion_not_applied_by_default(env):
    for i, act in enumerate((110000, 112000, 114000)):
        seed_completed_job(env, jid=f"h-{i}", est={"labor": 100000},
                           actual={"labor": act})
    est = make_estimate(env, estimated_costs={"labor": 1000.0})
    job = run(est_router.convert_to_job(
        est["id"], est_router.ConvertJobBody(), USER))
    assert job["estimated_costs_cents"]["labor"] == 100000
    assert job["budget_adjustment"] is None


# ==================== what-if (E) ====================
def test_what_if_does_not_persist(env):
    job = _seed_active_job(env, est={"labor": 100000}, costs={"labor": 40000})
    before = copy.deepcopy(env.jobs.docs[0])
    n_snaps = len(env.job_forecast_snapshots.docs)
    out = run(fc_router.what_if(job["id"],
                                fc_router.WhatIfBody(remaining={"labor": 5000.0}),
                                USER))
    labor = [r for r in out["rows"] if r["category"] == "labor"][0]
    assert labor["remaining_cents"] == 500000
    assert labor["forecast_cents"] == 540000
    assert out["what_if"] is True
    assert env.jobs.docs[0] == before
    assert len(env.job_forecast_snapshots.docs) == n_snaps


# ==================== history (F) ====================
def test_history_trail_created_material_final(env, stub_ledger):
    est = make_estimate(env, estimated_costs={"labor": 1000.0})
    job = run(est_router.convert_to_job(
        est["id"], est_router.ConvertJobBody(), USER))
    hist = run(fc_router.get_history(job["id"], USER))
    assert [s["reason"] for s in hist["snapshots"]] == ["created"]
    assert hist["baseline_total_cents"] == 100000
    assert hist["final_actual_cents"] is None

    # a small cost (< $50 and < 2%) does not snapshot
    run(jobs_router.add_cost(job["id"], jobs_router.CostBody(
        category="labor", amount=10.0), USER))
    assert len(env.job_forecast_snapshots.docs) == 1
    # a material cost does
    run(jobs_router.add_cost(job["id"], jobs_router.CostBody(
        category="labor", amount=200.0), USER))
    assert len(env.job_forecast_snapshots.docs) == 2
    assert env.job_forecast_snapshots.docs[-1]["reason"] == "material"

    # manual snapshot on demand
    run(fc_router.force_snapshot(job["id"], fc_router.SnapshotBody(), USER))
    assert env.job_forecast_snapshots.docs[-1]["reason"] == "manual"

    # closing freezes the final snapshot; history reports final actuals
    run(jobs_router.update_status(job["id"],
                                  jobs_router.StatusBody(status="completed"),
                                  USER))
    assert env.job_forecast_snapshots.docs[-1]["reason"] == "final"
    hist = run(fc_router.get_history(job["id"], USER))
    assert hist["final_actual_cents"] == 21000


# ==================== isolation + plain jobs ====================
def test_tenant_isolation(env):
    job = _seed_active_job(env)
    with pytest.raises(HTTPException) as ei:
        run(fc_router.get_forecast(job["id"], USER2))
    assert ei.value.status_code == 404
    # other tenant's history never leaks into suggestions
    for i in range(3):
        seed_completed_job(env, tenant=TID2, jid=f"x-{i}",
                           est={"labor": 100000}, actual={"labor": 150000})
    sugg = run(JF.budget_suggestion(TID, {"labor": 100000}))
    assert sugg["available"] is False


def test_plain_job_behaves_as_today(env):
    body = jobs_router.JobBody(customer_name="Walk-in",
                               estimated_revenue=2000.0,
                               estimated_costs={"labor": 500.0})
    job = run(jobs_router.create_job(body, USER))
    assert job["status"] == "estimate"
    assert "baseline_costs_cents" not in job
    fc = run(fc_router.get_forecast(job["id"], USER))
    assert fc["baseline"]["source"] == "job"
    assert fc["baseline_total_cents"] == 50000
    assert fc["totals"]["forecast_cents"] == 50000
