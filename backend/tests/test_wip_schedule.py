"""WIP schedule (job-costing ask #5) — direct-call tests: no mongod, no
network. The pure compute in lib/wip is exercised directly; the async
assembly (build_schedule / take_snapshot / compare) runs against an
in-memory FakeDB monkeypatched into lib.wip's `db`. Percent-complete,
earned and over/under math, snapshot freeze, MoM deltas, the catch-up
flag, story-note correctness and tenant isolation are all covered.
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

from lib import wip as W

TID = "tenant-wip"
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
        if isinstance(cond, dict) and cond and all(k.startswith("$")
                                                   for k in cond):
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
            self._docs.sort(key=lambda d: (d.get(field) is None,
                                           d.get(field)),
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


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(W, "db", fake)
    return fake


# ============================ fixtures ============================

def mk_job(**kw):
    job = {"id": "job-1", "job_number": "JOB-0001",
           "customer_name": "Acme Mechanical", "status": "active",
           "estimated_revenue_cents": 200_000,          # $2,000 contract
           "estimated_costs_cents": {"material": 40_000, "labor": 40_000,
                                     "equipment": 10_000,
                                     "subcontractor": 5_000,
                                     "other": 5_000},  # $1,000 est cost
           "change_orders": [], "created_at": "2026-10-01T00:00:00Z"}
    job.update(kw)
    return job


def cost(cat, cents, **kw):
    d = {"id": uuid.uuid4().hex, "job_id": "job-1", "category": cat,
         "amount_cents": cents}
    d.update(kw)
    return d


def invoice(cents, **kw):
    d = {"id": uuid.uuid4().hex, "job_id": "job-1",
         "subtotal_cents": cents, "status": "issued"}
    d.update(kw)
    return d


def seed(env, jobs, costs=(), invoices=(), tenant_id=TID):
    for j in jobs:
        env.jobs.docs.append({**j, "tenant_id": tenant_id})
    for c in costs:
        env.job_costs.docs.append({**c, "tenant_id": tenant_id})
    for i in invoices:
        env.invoices.docs.append({**i, "tenant_id": tenant_id})


# ============================ % complete math ============================

def test_pct_complete_basic():
    assert W.pct_complete(25_000, 100_000) == 25.0
    assert W.pct_complete(0, 100_000) == 0.0
    assert W.pct_complete(100_000, 100_000) == 100.0


def test_pct_complete_no_estimate_is_none_not_zero():
    assert W.pct_complete(50_000, 0) is None
    assert W.pct_complete(0, 0) is None


def test_pct_complete_over_100_is_honest():
    assert W.pct_complete(130_000, 100_000) == 130.0


def test_row_math_half_done():
    row = W.job_wip_row(mk_job(), [cost("material", 30_000),
                                   cost("labor", 20_000)],
                        [invoice(60_000)])
    assert row["pct_complete"] == 50.0
    assert row["earned_revenue_cents"] == 100_000      # 50% of $2,000
    assert row["billed_to_date_cents"] == 60_000
    assert row["over_under_cents"] == -40_000          # under-billed
    assert row["over_billed"] is False


def test_row_earned_capped_at_contract_when_over_100():
    row = W.job_wip_row(mk_job(), [cost("material", 120_000)], [])
    assert row["pct_complete"] == 120.0                # shown honestly
    assert row["earned_revenue_cents"] == 200_000      # capped at contract


def test_row_no_estimate_no_fake_numbers():
    job = mk_job(estimated_costs_cents={})
    row = W.job_wip_row(job, [cost("labor", 10_000)], [invoice(5_000)])
    assert row["pct_complete"] is None
    assert row["earned_revenue_cents"] is None
    assert row["over_under_cents"] is None
    assert row["billed_to_date_cents"] == 5_000        # fact still shown


def test_approved_change_orders_move_contract_and_estimate():
    job = mk_job(change_orders=[
        {"approved": True, "revenue_delta_cents": 50_000,
         "cost_delta_cents": 25_000},
        {"approved": False, "revenue_delta_cents": 999_00,
         "cost_delta_cents": 999_00}])
    row = W.job_wip_row(job, [cost("labor", 62_500)], [])
    assert row["contract_cents"] == 250_000
    assert row["estimated_costs_cents"] == 125_000
    assert row["pct_complete"] == 50.0
    assert row["earned_revenue_cents"] == 125_000


# ============================ story note ============================

def test_note_names_biggest_category_driver():
    costs = [cost("material", 79_600), cost("labor", 10_000)]
    # material estimated $400 -> actual $796 = 99% over its share
    row = W.job_wip_row(mk_job(), costs, [invoice(200_000)])
    assert "Material is the biggest driver" in row["note"]
    assert "99% over" in row["note"]


def test_note_says_billing_trails_earned():
    row = W.job_wip_row(mk_job(), [cost("labor", 50_000)], [])
    assert "Billing trails earned revenue by $1,000.00" in row["note"]


def test_note_over_100_percent_is_plain():
    row = W.job_wip_row(mk_job(), [cost("other", 110_000)], [])
    assert "Costs have passed the estimate" in row["note"]


def test_note_tracking_to_plan_when_nothing_notable():
    row = W.job_wip_row(mk_job(), [cost("material", 40_000),
                                   cost("labor", 40_000),
                                   cost("equipment", 10_000),
                                   cost("subcontractor", 5_000),
                                   cost("other", 5_000)],
                        [invoice(200_000)])
    assert row["pct_complete"] == 100.0
    assert "tracking to plan" in row["note"]


# ============================ catch-up flag ============================

def test_catchup_flag_fires_on_jump_without_billing():
    prev = {"pct_complete": 40.0, "billed_to_date_cents": 60_000}
    row = W.job_wip_row(mk_job(), [cost("labor", 50_000)],
                        [invoice(60_000)], prev_row=prev)
    assert row["pct_complete"] == 50.0
    assert row["billing_behind"] is True
    assert "work is ahead of billing" in row["note"]


def test_catchup_flag_quiet_when_billing_moved():
    prev = {"pct_complete": 40.0, "billed_to_date_cents": 50_000}
    row = W.job_wip_row(mk_job(), [cost("labor", 50_000)],
                        [invoice(60_000)], prev_row=prev)
    assert row["billing_behind"] is False


def test_catchup_flag_quiet_on_small_move():
    prev = {"pct_complete": 47.0, "billed_to_date_cents": 60_000}
    row = W.job_wip_row(mk_job(), [cost("labor", 50_000)],
                        [invoice(60_000)], prev_row=prev)
    assert row["billing_behind"] is False


# ============================ phases ============================

def test_phase_rows_split_by_phase_budget_share():
    job = mk_job(phases=[
        {"id": "ph-1", "name": "Rough-in",
         "budget_cents": {"material": 40_000, "labor": 20_000}},
        {"id": "ph-2", "name": "Trim-out",
         "budget_cents": {"labor": 20_000, "equipment": 10_000,
                          "subcontractor": 5_000, "other": 5_000}}])
    costs = [cost("material", 30_000, phase_id="ph-1"),
             cost("labor", 10_000, phase_id="ph-2"),
             cost("other", 5_000)]                      # unphased cost
    row = W.job_wip_row(job, costs, [])
    assert row["costs_to_date_cents"] == 45_000
    p1, p2 = row["phases"]
    assert p1["budget_cents"] == 60_000
    assert p1["pct_complete"] == 50.0
    assert p1["contract_share_cents"] == 120_000       # 60% of contract
    assert p1["earned_revenue_cents"] == 60_000
    assert p1["billed_to_date_cents"] is None          # job-level only
    assert p2["pct_complete"] == 25.0
    assert p2["earned_revenue_cents"] == 20_000        # 40% x 25%


def test_unphased_job_has_empty_phase_rows():
    row = W.job_wip_row(mk_job(), [cost("labor", 1_000)], [])
    assert row["phases"] == []


# ============================ live assembly ============================

def test_build_schedule_open_jobs_only_and_totals(env):
    seed(env, [mk_job(), mk_job(id="job-2", job_number="JOB-0002",
                                status="closed")],
         costs=[cost("labor", 50_000)],
         invoices=[invoice(60_000)])
    sched = run(W.build_schedule(TID))
    assert [r["job_number"] for r in sched["rows"]] == ["JOB-0001"]
    t = sched["totals"]
    assert t["contract_cents"] == 200_000
    assert t["earned_revenue_cents"] == 100_000
    assert t["over_under_cents"] == -40_000
    assert t["pct_complete"] == 50.0


def test_tenant_isolation(env):
    seed(env, [mk_job()], costs=[cost("labor", 50_000)],
         invoices=[invoice(60_000)])
    seed(env, [mk_job(id="job-x", job_number="JOB-9999")],
         costs=[cost("labor", 99_000, job_id="job-x")],
         invoices=[invoice(99_000, job_id="job-x")], tenant_id=TID2)
    sched = run(W.build_schedule(TID))
    assert [r["job_number"] for r in sched["rows"]] == ["JOB-0001"]
    sched2 = run(W.build_schedule(TID2))
    assert [r["job_number"] for r in sched2["rows"]] == ["JOB-9999"]


# ============================ snapshots ============================

def test_snapshot_freezes_figures(env):
    seed(env, [mk_job()], costs=[cost("labor", 50_000)],
         invoices=[invoice(60_000)])
    snap = run(W.take_snapshot(TID, USER, "2026-10"))
    assert snap["period"] == "2026-10"
    assert snap["rows"][0]["earned_revenue_cents"] == 100_000

    # later activity moves the live schedule…
    seed(env, [], costs=[cost("labor", 50_000)],
         invoices=[invoice(140_000)])
    live = run(W.build_schedule(TID))
    assert live["rows"][0]["pct_complete"] == 100.0

    # …but the frozen snapshot stands exactly as taken
    stored = run(W.snapshots_coll(TID).find_one({"id": snap["id"]}))
    assert stored["rows"][0]["pct_complete"] == 50.0
    assert stored["rows"][0]["earned_revenue_cents"] == 100_000
    assert stored["rows"][0]["billed_to_date_cents"] == 60_000


def test_compare_mom_deltas_and_catchup(env):
    seed(env, [mk_job()], costs=[cost("labor", 40_000)],
         invoices=[invoice(60_000)])
    snap = run(W.take_snapshot(TID, USER, "2026-09"))
    seed(env, [], costs=[cost("labor", 10_000)])       # +10 points, no bill
    live = run(W.build_schedule(TID))
    rows = W.compare_schedules(snap["rows"], live["rows"])
    (r,) = rows
    assert r["presence"] == "both"
    assert r["pct_complete_delta"] == 10.0
    assert r["billed_delta_cents"] == 0
    assert r["billing_behind"] is True
    # and the live row carries the flag too (snapshot is the latest)
    assert live["rows"][0]["billing_behind"] is True


def test_compare_marks_new_and_gone_jobs(env):
    base = [W.job_wip_row(mk_job(), [], [])]
    against = [W.job_wip_row(mk_job(id="job-2", job_number="JOB-0002"),
                             [], [])]
    rows = {r["job_number"]: r
            for r in W.compare_schedules(base, against)}
    assert rows["JOB-0001"]["presence"] == "gone"
    assert rows["JOB-0002"]["presence"] == "new"
    assert rows["JOB-0001"]["pct_complete_delta"] is None
