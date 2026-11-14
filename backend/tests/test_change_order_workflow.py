"""Change-order workflow (owner-approved 2026-11-14, job-costing item #6).

Direct-call tests: no mongod, no network. The jobs router endpoints are
awaited directly with a fake user dict and an in-memory FakeDB
monkeypatched into the router module (harness shared with the sibling
job-alerts lane tests).
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


from routers import jobs as jobs_router
from lib import jobalerts as jobalerts_lib
from lib import jobcosting as jobcosting_lib

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
    monkeypatch.setattr(jobs_router, "db", fake)
    # Merged tree (2026-11-14): approval re-evaluates budget alerts through
    # the overrun lane's lib handle, and get_job reads committed-cost config.
    monkeypatch.setattr(jobalerts_lib, "db", fake)
    monkeypatch.setattr(jobcosting_lib, "db", fake)
    from lib import jobphases as jobphases_lib, retainage as retainage_lib
    monkeypatch.setattr(jobphases_lib, "db", fake)
    monkeypatch.setattr(retainage_lib, "db", fake)
    from lib import jobforecast as jobforecast_lib  # merge-train: forecast hook in add_cost
    monkeypatch.setattr(jobforecast_lib, "db", fake)
    return fake


def mk_job(**kw):
    job = {"id": "job-1", "job_number": "JOB-0001", "customer_name": "Acme",
           "status": "active", "tenant_id": TID,
           "estimated_revenue_cents": 200_000,
           "estimated_costs_cents": {"material": 50_000, "labor": 30_000,
                                      "equipment": 0, "subcontractor": 0,
                                      "other": 0},
           "change_orders": []}
    job.update(kw)
    return job


def seed(env, job=None):
    env.jobs.docs.append(copy.deepcopy(job or mk_job()))


def profit(env, user=USER):
    j = run(jobs_router.jobs(user).find_one({"id": "job-1"}))
    return run(jobs_router._profit(user, j))


def co_body(**kw):
    body = {"description": "Add duct run", "reason": "Customer asked",
            "revenue_delta": 1000.0, "cost_impact": {"material": 300.0, "labor": 200.0},
            "delay_days": 2}
    body.update(kw)
    return jobs_router.ChangeOrderBody(**body)


# ============================ tests ============================

def test_pending_co_does_not_move_budget(env):
    seed(env)
    before = profit(env)
    co = run(jobs_router.add_change_order("job-1", co_body(), USER))
    assert co["state"] == "pending" and co["approved"] is False
    after = profit(env)
    assert after["estimated_revenue_cents"] == before["estimated_revenue_cents"]
    assert after["estimated_cost_cents"] == before["estimated_cost_cents"]
    view = run(jobs_router.get_job("job-1", USER))
    assert view["co_summary"]["pending_count"] == 1
    assert view["co_summary"]["pending_revenue_cents"] == 100_000
    assert view["co_summary"]["pending_cost_cents"] == 50_000


def test_client_cannot_self_approve(env):
    seed(env)
    body = co_body()
    # even a payload begging for approval is born pending
    co = run(jobs_router.add_change_order("job-1", body, USER))
    assert co["state"] == "pending"


def test_approve_moves_budget_same_day_by_category(env):
    seed(env)
    co = run(jobs_router.add_change_order("job-1", co_body(), USER))
    out = run(jobs_router.approve_change_order("job-1", co["id"], USER))
    assert out["state"] == "approved" and out["approved"] is True
    p = profit(env)
    assert p["estimated_revenue_cents"] == 300_000          # +$1,000
    assert p["estimated_cost_cents"] == 130_000             # 80k + 50k impact
    assert p["estimated_by_category_cents"]["material"] == 80_000   # 50k + 30k
    assert p["estimated_by_category_cents"]["labor"] == 50_000      # 30k + 20k
    assert p["estimated_by_category_cents"]["equipment"] == 0


def test_approve_lump_sum_fallback(env):
    seed(env)
    co = run(jobs_router.add_change_order(
        "job-1", co_body(cost_impact={}, cost_delta=250.0), USER))
    run(jobs_router.approve_change_order("job-1", co["id"], USER))
    p = profit(env)
    assert p["estimated_cost_cents"] == 105_000             # 80k + $250 lump


def test_reject_keeps_budget_and_record(env):
    seed(env)
    co = run(jobs_router.add_change_order("job-1", co_body(), USER))
    out = run(jobs_router.reject_change_order("job-1", co["id"], USER))
    assert out["state"] == "rejected" and out["approved"] is False
    p = profit(env)
    assert p["estimated_revenue_cents"] == 200_000
    assert p["estimated_cost_cents"] == 80_000
    view = run(jobs_router.get_job("job-1", USER))
    assert len(view["change_orders"]) == 1                  # record kept
    assert view["co_summary"]["rejected_count"] == 1
    # a rejected CO writes no approval notification
    notes = [d for d in env.app_notifications.docs if d["tenant_id"] == TID]
    assert notes == []


def test_legacy_change_orders_behave_as_before(env):
    legacy_ok = {"id": "co-old-1", "description": "Old approved",
                 "revenue_delta_cents": 10_000, "cost_delta_cents": 4_000,
                 "approved": True, "created_at": "2026-01-01T00:00:00+00:00"}
    legacy_no = {"id": "co-old-2", "description": "Old unapproved",
                 "revenue_delta_cents": 99_000, "cost_delta_cents": 99_000,
                 "approved": False, "created_at": "2026-01-02T00:00:00+00:00"}
    seed(env, mk_job(change_orders=[legacy_ok, legacy_no]))
    p = profit(env)
    assert p["estimated_revenue_cents"] == 210_000          # only approved legacy counts
    assert p["estimated_cost_cents"] == 84_000
    view = run(jobs_router.get_job("job-1", USER))
    states = {c["id"]: c["state"] for c in view["change_orders"]}
    assert states["co-old-1"] == "approved"
    assert states["co-old-2"] == "pending"                  # never approved -> not in budget
    # legacy COs cannot be re-decided through the workflow
    with pytest.raises(HTTPException) as ei:
        run(jobs_router.approve_change_order("job-1", "co-old-1", USER))
    assert ei.value.status_code == 409


def test_approval_notification_fires_exactly_once(env):
    seed(env)
    co = run(jobs_router.add_change_order("job-1", co_body(), USER))
    run(jobs_router.approve_change_order("job-1", co["id"], USER))
    notes = [d for d in env.app_notifications.docs
             if d.get("kind") == "change_order_approved"]
    assert len(notes) == 1
    assert notes[0]["tenant_id"] == TID and notes[0]["tenant_wide"] is True
    assert notes[0]["dedupe_key"] == f"co-approved:{co['id']}"
    with pytest.raises(HTTPException) as ei:
        run(jobs_router.approve_change_order("job-1", co["id"], USER))
    assert ei.value.status_code == 409
    notes = [d for d in env.app_notifications.docs
             if d.get("kind") == "change_order_approved"]
    assert len(notes) == 1                                  # still exactly one
    out = run(jobs_router.list_notifications(USER))
    assert out["unread"] == 1
    run(jobs_router.mark_notification_read(notes[0]["id"], USER))
    assert run(jobs_router.list_notifications(USER))["unread"] == 0


def test_history_is_complete(env):
    seed(env)
    co = run(jobs_router.add_change_order("job-1", co_body(), USER))
    run(jobs_router.approve_change_order("job-1", co["id"], USER))
    view = run(jobs_router.get_job("job-1", USER))
    hist = view["co_history"]
    assert [h["event"] for h in hist] == ["created", "approved"]
    assert all(h["by_name"] == "Owner" and h["at"] for h in hist)
    assert hist[0]["revenue_delta_cents"] == 100_000
    saved = view["change_orders"][0]
    assert saved["decided_by"]["by_name"] == "Owner" and saved["decided_at"]
    assert saved["reason"] == "Customer asked"
    assert saved["delay_days"] == 2
    assert saved["cost_impact_cents"] == {"material": 30_000, "labor": 20_000}


def test_tenant_isolation(env):
    seed(env)
    co = run(jobs_router.add_change_order("job-1", co_body(), USER))
    with pytest.raises(HTTPException) as ei:
        run(jobs_router.get_job("job-1", USER2))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        run(jobs_router.approve_change_order("job-1", co["id"], USER2))
    assert ei.value.status_code == 404
    # the other tenant's decision attempt changed nothing
    assert profit(env)["estimated_revenue_cents"] == 200_000
