"""Time-tracking wiring: approved time entries -> payroll hours + job labor costs.

No live MongoDB needed (fully mocked). Run from the backend directory with:
    python -m pytest tests/test_time_wiring.py

Conservative rules under test:
  - only approved/closed entries count (logged/open never)
  - payroll counts billable AND non-billable hours; jobs count billable only
  - explicit run hours always override time entries
  - every query is tenant-scoped
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from lib import timeutil as T
from routers import payroll, jobs
from routers.payroll import RunItemIn

# Dedicated loop: asyncio.run() would unset the thread's current event loop and
# break legacy asyncio.get_event_loop() users in other test modules.
_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _e(eid, user_id, date, hours, status="approved", billable=True, job_id=None):
    return {"id": eid, "user_id": user_id, "user_name": "Tech",
            "date": date, "hours": hours, "status": status,
            "billable": billable, "job_id": job_id}


# ---------------- lib.timeutil.period_hours ----------------

def test_period_hours_sums_approved_and_closed_in_range():
    entries = [
        _e("a", "u1", "2026-10-05", 8.0, "approved"),
        _e("b", "u1", "2026-10-06", 7.5, "closed"),
        _e("c", "u1", "2026-10-07", 4.0, "logged"),      # not approved -> excluded
        _e("d", "u1", "2026-10-08", 8.0, "open"),        # excluded
        _e("e", "u1", "2026-09-30", 8.0, "approved"),    # out of range -> excluded
        _e("f", "u2", "2026-10-05", 8.0, "approved"),    # other user -> excluded
    ]
    hours, ids = T.period_hours(entries, "u1", "2026-10-01", "2026-10-15")
    assert hours == 15.5
    assert sorted(ids) == ["a", "b"]


def test_period_hours_counts_non_billable_for_payroll():
    entries = [_e("a", "u1", "2026-10-05", 6.0, "approved", billable=False)]
    hours, ids = T.period_hours(entries, "u1", "2026-10-01", "2026-10-31")
    assert hours == 6.0  # hours worked are hours paid


def test_period_hours_empty():
    assert T.period_hours([], "u1", "2026-10-01", "2026-10-31") == (0.0, [])
    assert T.period_hours(None, "u1", "2026-10-01", "2026-10-31") == (0.0, [])


# ---------------- lib.timeutil.job_time_labor ----------------

def test_job_time_labor_aggregates_linked_billable():
    entries = [
        _e("a", "u1", "2026-10-05", 8.0, "approved", True, "job1"),
        _e("b", "u2", "2026-10-06", 4.0, "closed", True, "job1"),
        _e("c", "u1", "2026-10-06", 3.0, "logged", True, "job1"),      # unapproved
        _e("d", "u1", "2026-10-06", 2.0, "approved", False, "job1"),   # non-billable
        _e("e", "u1", "2026-10-06", 5.0, "approved", True, "job2"),    # other job
        _e("f", "u1", "2026-10-06", 5.0, "approved", True, None),      # unlinked
    ]
    rates = {"u1": 2500, "u2": 3000}  # cents/hr
    out = T.job_time_labor(entries, "job1", rates)
    assert out["hours"] == 12.0
    assert out["cost_cents"] == 8 * 2500 + 4 * 3000
    assert out["entry_count"] == 2


def test_job_time_labor_missing_rate_counts_hours_not_cost():
    entries = [_e("a", "u9", "2026-10-05", 8.0, "approved", True, "job1")]
    out = T.job_time_labor(entries, "job1", {})
    assert out["hours"] == 8.0
    assert out["cost_cents"] == 0
    assert out["entry_count"] == 1


# ---------------- payroll._resolve_hours ----------------

def _user(tenant="t1"):
    return {"id": "owner1", "tenant_id": tenant, "role": "owner", "email": "o@x.com"}


def _member(hourly_cents=2500, salary_cents=0):
    return {"id": "u1", "name": "Tech",
            "hourly_rate_cents": hourly_cents, "salary_monthly_cents": salary_cents}


def test_resolve_hours_prefills_from_approved_entries():
    entries = [_e("a", "u1", "2026-10-05", 8.0, "approved"),
               _e("b", "u1", "2026-10-20", 7.0, "approved")]
    async def go():
        with patch.object(payroll, "_fetch_period_entries",
                          AsyncMock(return_value=entries)) as fetch:
            hours, source, ids = await payroll._resolve_hours(
                _user(), RunItemIn(user_id="u1"), _member(), "2026-10-01", "2026-10-31")
            fetch.assert_awaited_once()
        return hours, source, ids
    hours, source, ids = _run(go())
    assert hours == 15.0
    assert source == "time_entries"
    assert sorted(ids) == ["a", "b"]


def test_resolve_hours_manual_override_wins_and_skips_fetch():
    async def go():
        with patch.object(payroll, "_fetch_period_entries",
                          AsyncMock()) as fetch:
            hours, source, ids = await payroll._resolve_hours(
                _user(), RunItemIn(user_id="u1", hours=0), _member(),
                "2026-10-01", "2026-10-31")
            fetch.assert_not_awaited()
        return hours, source, ids
    hours, source, ids = _run(go())
    assert (hours, source, ids) == (0, "manual", [])


def test_resolve_hours_salary_ignores_time_entries():
    async def go():
        with patch.object(payroll, "_fetch_period_entries",
                          AsyncMock()) as fetch:
            hours, source, _ = await payroll._resolve_hours(
                _user(), RunItemIn(user_id="u1"), _member(hourly_cents=0, salary_cents=500000),
                "2026-10-01", "2026-10-31")
            fetch.assert_not_awaited()
        return hours, source
    assert _run(go()) == (0.0, "manual")


# ---------------- tenant isolation ----------------

class _FakeCursor:
    def __init__(self, coll, q):
        self.coll, self.q = coll, q
    def sort(self, _): return self
    def limit(self, _): return self
    async def to_list(self, length=None): return []


class _FakeCollection:
    def __init__(self): self.queries = []
    def find(self, q, projection=None):
        self.queries.append(q)
        return _FakeCursor(self, q)


def test_fetch_period_entries_is_tenant_scoped():
    fake = _FakeCollection()
    async def go():
        from lib.base import Scoped
        scoped = Scoped(fake, "tenant-ABC")
        # NOTE: payroll.db is the real (lazy) motor handle; db.time_entries only
        # builds a collection reference — no I/O happens. Scoped is mocked.
        with patch("routers.payroll.Scoped", return_value=scoped):
            await payroll._fetch_period_entries(_user("tenant-ABC"), "u1",
                                                "2026-10-01", "2026-10-31")
    _run(go())
    assert len(fake.queries) == 1
    q = fake.queries[0]
    assert q["tenant_id"] == "tenant-ABC"
    assert q["user_id"] == "u1"
    assert set(q["status"]["$in"]) == {"approved", "closed"}
    assert q["date"] == {"$gte": "2026-10-01", "$lte": "2026-10-31"}


def test_job_time_block_is_tenant_scoped_and_filters():
    fake = _FakeCollection()
    entries = [_e("a", "u1", "2026-10-05", 8.0, "approved", True, "job1"),
               _e("b", "u1", "2026-10-06", 2.0, "logged", True, "job1")]
    async def go():
        from lib.base import Scoped
        real = Scoped(fake, "tenant-XYZ")
        with patch("routers.jobs.Scoped", return_value=real):
            # feed canned entries through find_list by patching the cursor
            with patch.object(_FakeCursor, "to_list", AsyncMock(return_value=entries)):
                return await jobs._job_time_block(_user("tenant-XYZ"),
                                                  {"id": "job1"}, {"u1": 2500})
    block = _run(go())
    assert fake.queries[0]["tenant_id"] == "tenant-XYZ"
    assert fake.queries[0]["job_id"] == "job1"
    assert block["time_labor_hours"] == 8.0          # logged entry excluded
    assert block["time_labor_cents"] == 8 * 2500
    assert block["time_entry_count"] == 1
    assert "not posted" in block["time_labor_note"]
