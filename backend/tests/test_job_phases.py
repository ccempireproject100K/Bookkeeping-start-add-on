"""Job phase / cost-code costing (owner-approved 2026-11-14, ask #4).

Direct-call tests: no mongod, no network. Endpoint functions are awaited
directly with a fake user dict and an in-memory FakeDB monkeypatched into
the `db` attribute of the modules under test. The ledger posting seam is
faked (entry numbers only); everything else — phases, sum-checks,
variance math, drift firing, field-entry flow — runs for real.
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

from lib import jobphases as JP
from lib import ledger
from routers import jobs, receipts, time as time_router

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}
TECH = {"id": "tech-1", "email": "tech@test.com", "name": "Tech",
        "role": "barber", "tenant_id": TID}


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
                elif op == "$exists":
                    if (val is not _MISSING) != bool(operand):
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
        elif op == "$unset":
            for k in fields:
                doc.pop(k, None)
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


def _sort_key(v):
    return (v is None, v if isinstance(v, (str, int, float)) else str(v))


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        items = [spec] if isinstance(spec, tuple) else list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: _sort_key(d.get(field)),
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
        return SimpleNamespace(inserted_ids=[])

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return SimpleNamespace(matched_count=0, modified_count=0)
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
    # Merged tree (2026-11-14): get_job/add_cost also consult the overrun
    # alerts + committed-cost config through those libs' own db handles.
    from lib import jobalerts as _JA, jobcosting as _JC
    from lib import retainage as _RT
    for mod in (jobs, receipts, time_router, JP, _JA, _JC, _RT):
        monkeypatch.setattr(mod, "db", fake)

    counter = {"n": 0}

    async def fake_post(db_, tenant_id, user, payload, status="posted"):
        counter["n"] += 1
        return {"number": f"JE-{counter['n']:04d}"}

    async def fake_audit(*a, **k):
        return None

    monkeypatch.setattr(ledger, "post_journal_entry", fake_post)
    monkeypatch.setattr(ledger, "audit", fake_audit)
    monkeypatch.setattr(receipts, "audit", fake_audit)
    # tech with a $50/hr pay rate for the time-entry flow
    fake.users.docs.append(
        {"id": "tech-1", "tenant_id": TID, "name": "Tech",
         "pay_settings": {"hourly_rate_cents": 5000}})
    return fake


def make_job(env, est_costs=None, user=USER, activate=True):
    est_costs = est_costs if est_costs is not None else {"material": 6000, "labor": 4000}
    job = run(jobs.create_job(jobs.JobBody(
        customer_name="Acme HOA", estimated_revenue=15000,
        estimated_costs=est_costs), user))
    if activate:
        run(jobs.update_status(job["id"], jobs.StatusBody(status="active"), user))
        job["status"] = "active"
    return job


def add_phase(env, job, name, budget, user=USER):
    return run(jobs.add_phase(job["id"], jobs.PhaseBody(name=name, budget=budget), user))


def phase_view(env, job, user=USER):
    return run(jobs.get_phases(job["id"], user))


def notifs(env, job_id=None):
    rows = env.app_notifications.docs
    if job_id:
        rows = [r for r in rows if r.get("job_id") == job_id]
    return [r for r in rows if r.get("kind") == "job_phase_drift"]


# ============================ sum-check ============================
def test_phase_budgets_sum_checked_with_warning(env):
    job = make_job(env)  # estimate total $10,000
    r1 = add_phase(env, job, "Rough-in", {"material": 6000})
    assert r1["budget_check"]["phases_budget_cents"] == 600_000
    assert r1["budget_check"]["reconciled"] is False
    assert "under by $4,000.00" in r1["budget_check"]["warning"]

    r2 = add_phase(env, job, "Trim-out", {"labor": 3000})
    assert r2["budget_check"]["reconciled"] is False
    assert "under by $1,000.00" in r2["budget_check"]["warning"]

    trim_id = r2["phase"]["id"]
    r3 = run(jobs.update_phase(job["id"], trim_id,
                               jobs.PhaseBody(name="Trim-out", budget={"labor": 4000}), USER))
    assert r3["budget_check"]["reconciled"] is True
    assert r3["budget_check"]["warning"] is None
    assert r3["budget_check"]["difference_cents"] == 0


def test_phase_budget_over_estimate_warns(env):
    job = make_job(env, est_costs={"material": 1000})
    r = add_phase(env, job, "Everything", {"material": 1250})
    assert r["budget_check"]["reconciled"] is False
    assert "over by $250.00" in r["budget_check"]["warning"]


# ============================ variance math ============================
def test_per_phase_variance_math(env):
    job = make_job(env)
    a = add_phase(env, job, "Rough-in", {"material": 5000})["phase"]
    b = add_phase(env, job, "Trim-out", {"labor": 5000})["phase"]
    run(jobs.add_cost(job["id"], jobs.CostBody(
        category="material", amount=2500, phase_id=a["id"]), USER))
    run(jobs.add_cost(job["id"], jobs.CostBody(
        category="labor", hours=10, rate=100, phase_id=b["id"]), USER))
    run(jobs.add_cost(job["id"], jobs.CostBody(
        category="other", amount=500), USER))  # unphased

    view = phase_view(env, job)
    pa = next(p for p in view["phases"] if p["phase_id"] == a["id"])
    pb = next(p for p in view["phases"] if p["phase_id"] == b["id"])
    assert pa["budget_cents"] == 500_000 and pa["actual_cents"] == 250_000
    assert pa["pct_used"] == 50.0 and pa["variance_cents"] == -250_000
    assert pa["actual_by_category_cents"] == {"material": 250_000}
    assert pa["committed_cents"] == 0
    assert pb["actual_cents"] == 100_000 and pb["pct_used"] == 20.0
    assert view["unphased"]["actual_cents"] == 50_000
    assert view["unphased"]["cost_count"] == 1


def test_committed_slots_in_from_phase_tagged_commitments(env):
    job = make_job(env)
    a = add_phase(env, job, "Rough-in", {"material": 6000})["phase"]
    # committed-lane sources, phase-tagged the way that lane will emit
    env.purchase_orders.docs.append({
        "id": "po-1", "tenant_id": TID, "job_id": job["id"],
        "phase_id": a["id"], "status": "approved",
        "lines": [{"quantity": 2, "unit_cost_cents": 100_000}]})
    env.job_commitments.docs.append({
        "id": "jc-1", "tenant_id": TID, "job_id": job["id"],
        "phase_id": a["id"], "status": "approved",
        "amount_cents": 300_000, "fulfilled_cents": 50_000})
    view = phase_view(env, job)
    pa = view["phases"][0]
    assert pa["committed_cents"] == 200_000 + 250_000


# ============================ drift flags ============================
def test_drift_fires_per_phase_once_then_overrun(env):
    job = make_job(env, est_costs={"material": 20000})
    a = add_phase(env, job, "Rough-in", {"material": 1000})["phase"]
    add_phase(env, job, "Trim-out", {"material": 19000})
    run(jobs.add_cost(job["id"], jobs.CostBody(
        category="material", amount=600, phase_id=a["id"]), USER))
    # 60% of phase budget while the job overall is at 3% -> drift
    run(jobs.get_job(job["id"], USER))
    ns = notifs(env, job["id"])
    assert len(ns) == 1
    assert ns[0]["level"] == "drift" and ns[0]["phase_id"] == a["id"]
    assert ns[0]["tenant_wide"] is True and ns[0]["read"] is False
    # re-reading does not re-fire
    run(jobs.get_job(job["id"], USER))
    assert len(notifs(env, job["id"])) == 1
    # push the phase over budget -> a second, overrun, notification
    run(jobs.add_cost(job["id"], jobs.CostBody(
        category="material", amount=500, phase_id=a["id"]), USER))
    run(jobs.get_job(job["id"], USER))
    ns = notifs(env, job["id"])
    assert len(ns) == 2
    assert ns[-1]["level"] == "overrun"


def test_no_drift_for_unphased_job(env):
    job = make_job(env, est_costs={"material": 1000})
    run(jobs.add_cost(job["id"], jobs.CostBody(
        category="material", amount=5000), USER))
    run(jobs.get_job(job["id"], USER))
    assert notifs(env, job["id"]) == []


# ============================ field tagging ============================
def test_time_entry_phase_flows_into_actuals(env):
    job = make_job(env)
    a = add_phase(env, job, "Rough-in", {"labor": 4000})["phase"]
    entry = run(time_router.log_entry(time_router.ManualEntryBody(
        date="2026-11-14", hours=2, notes="Rough-in work",
        job_id=job["id"], phase_id=a["id"]), TECH))
    assert entry["job_id"] == job["id"] and entry["phase_id"] == a["id"]
    run(time_router.approve_entry(entry["id"], USER))
    costs = [c for c in env.job_costs.docs if c.get("source") == "time_entry"]
    assert len(costs) == 1
    assert costs[0]["phase_id"] == a["id"]
    assert costs[0]["amount_cents"] == 10_000  # 2h x $50/hr
    view = phase_view(env, job)
    assert view["phases"][0]["actual_cents"] == 10_000
    assert view["phases"][0]["actual_by_category_cents"] == {"labor": 10_000}


def test_receipt_phase_flows_into_actuals(env):
    job = make_job(env)
    a = add_phase(env, job, "Rough-in", {"material": 6000})["phase"]
    env.receipts.docs.append({"id": "rcpt-1", "tenant_id": TID,
                              "status": "scanned"})
    out = run(receipts.post_receipt("rcpt-1", receipts.PostBody(
        vendor="SupplyCo", category_code="5700", amount=250.0,
        job_id=job["id"], phase_id=a["id"], job_category="material"), USER))
    assert out["job_cost_id"]
    view = phase_view(env, job)
    assert view["phases"][0]["actual_cents"] == 25_000
    # pre-tagged receipt: tag first, post without job fields
    env.receipts.docs.append({"id": "rcpt-2", "tenant_id": TID,
                              "status": "scanned"})
    run(receipts.tag_receipt_job("rcpt-2", receipts.JobTagBody(
        job_id=job["id"], phase_id=a["id"]), USER))
    run(receipts.post_receipt("rcpt-2", receipts.PostBody(
        vendor="SupplyCo", category_code="5700", amount=50.0), USER))
    view = phase_view(env, job)
    assert view["phases"][0]["actual_cents"] == 30_000


def test_phase_tag_validation(env):
    job = make_job(env)
    with pytest.raises(HTTPException) as ei:
        run(time_router.log_entry(time_router.ManualEntryBody(
            date="2026-11-14", hours=1, phase_id="ph-x"), TECH))
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        run(jobs.add_cost(job["id"], jobs.CostBody(
            category="material", amount=10, phase_id="nope"), USER))
    assert ei.value.status_code == 422


# ============================ unphased invariance ============================
def test_unphased_job_behaves_as_before(env):
    job = make_job(env)
    run(jobs.add_cost(job["id"], jobs.CostBody(
        category="material", amount=1500), USER))
    detail = run(jobs.get_job(job["id"], USER))
    assert detail["phases"] == []
    assert detail["phase_variance"]["phases"] == []
    assert detail["phase_variance"]["budget_check"]["has_phases"] is False
    assert detail["phase_variance"]["budget_check"]["reconciled"] is True
    assert detail["profit"]["actual_cost_cents"] == 150_000
    assert detail["costs"][0]["phase_id"] is None


def test_delete_phase_rules(env):
    job = make_job(env)
    a = add_phase(env, job, "Rough-in", {"material": 1000})["phase"]
    b = add_phase(env, job, "Trim-out", {"material": 1000})["phase"]
    run(jobs.add_cost(job["id"], jobs.CostBody(
        category="material", amount=100, phase_id=a["id"]), USER))
    with pytest.raises(HTTPException) as ei:
        run(jobs.delete_phase(job["id"], a["id"], USER))
    assert ei.value.status_code == 409
    out = run(jobs.delete_phase(job["id"], b["id"], USER))
    assert out["deleted"] == b["id"]


# ============================ tenant isolation ============================
def test_tenant_isolation(env):
    job = make_job(env)
    add_phase(env, job, "Rough-in", {"material": 1000})
    with pytest.raises(HTTPException) as ei:
        run(jobs.get_job(job["id"], USER2))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        run(jobs.add_phase(job["id"], jobs.PhaseBody(
            name="Nope", budget={"material": 1}), USER2))
    assert ei.value.status_code == 404
    rows = run(jobs.list_jobs(USER2))
    assert rows == []
