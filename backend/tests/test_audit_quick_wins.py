"""Audit quick-wins regression tests.

Covers:
  1. KPI date fixes — invoices are stamped with issue_date at creation;
     revenue_trend falls back to created_at for legacy docs; recurring_share
     reads the real `active: bool` field.
  2. Owner permission fix — POST /fx/revalue and POST /benchmarks/opt-in no
     longer use the nonexistent "owner" permission key (which 403'd everyone).

No live MongoDB or network needed (db collections are faked).

Run from the backend directory with: python -m pytest tests/test_audit_quick_wins.py
"""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import db as dbmod
from lib.auth import PERMISSIONS, require
from lib.base import now_iso, today_str
from routers import kpis as K

TID = "tenant-1"
OWNER = {"id": "u1", "email": "owner@shop.test", "tenant_id": TID, "role": "owner"}
BARBER = {"id": "u2", "email": "staff@shop.test", "tenant_id": TID, "role": "barber"}


# ---------------------------------------------------------------- fakes

class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec):
            self.docs.sort(key=lambda d: d.get(key),
                           reverse=(direction == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=0):
        return self.docs


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    def _match(self, doc, filt):
        or_clauses = filt.get("$or")
        if or_clauses is not None:
            rest = {k: v for k, v in filt.items() if k != "$or"}
            return self._match(doc, rest) and any(
                self._match(doc, c) for c in or_clauses)
        for key, cond in filt.items():
            if key == "tenant_id":
                continue  # Scoped tenant filter; fakes are single-tenant
            if isinstance(cond, dict):
                for op, val in cond.items():
                    if op == "$gte" and not (doc.get(key) is not None
                                             and doc.get(key) >= val):
                        return False
                    if op == "$lte" and not (doc.get(key) is not None
                                             and doc.get(key) <= val):
                        return False
            elif doc.get(key) != cond:
                return False
        return True

    def find(self, filt=None, projection=None):
        return FakeCursor([d for d in self.docs if self._match(d, filt or {})])


def _fake_db(monkeypatch, invoices=(), estimates=(), jobs=(), recurring=()):
    fake = SimpleNamespace(
        invoices=FakeCollection(invoices),
        estimates=FakeCollection(estimates),
        jobs=FakeCollection(jobs),
        recurring_invoices=FakeCollection(recurring),
    )
    monkeypatch.setattr(K, "db", fake)
    return fake


def _inv(**kw):
    d = {"id": "i1", "total_cents": 10000, "customer_id": "c1",
         "status": "issued", "created_at": now_iso(), "issue_date": today_str()}
    d.update(kw)
    return d


# ------------------------------------------------- Fix 1: KPI date fields

def test_kpis_see_issued_invoices(monkeypatch):
    asyncio.run(_test_kpis_see_issued_invoices(monkeypatch))


async def _test_kpis_see_issued_invoices(monkeypatch):
    """Invoices stamped with issue_date fall inside the KPI trailing window."""
    _fake_db(monkeypatch, invoices=[_inv(id="i1"), _inv(id="i2", customer_id="c2")])
    k = await K._compute({"tenant_id": TID}, days=90)
    assert k["avg_ticket"]["samples"] == 2
    assert k["avg_ticket"]["value"] == 100.0


def test_kpis_exclude_stale_invoices(monkeypatch):
    asyncio.run(_test_kpis_exclude_stale_invoices(monkeypatch))


async def _test_kpis_exclude_stale_invoices(monkeypatch):
    """Invoices older than the window are excluded by the issue_date filter."""
    old = (date.fromisoformat(today_str()) - timedelta(days=400)).isoformat()
    _fake_db(monkeypatch, invoices=[_inv(id="i1"),
                                    _inv(id="i2", issue_date=old)])
    k = await K._compute({"tenant_id": TID}, days=90)
    assert k["avg_ticket"]["samples"] == 1


def test_revenue_trend_falls_back_to_created_at(monkeypatch):
    asyncio.run(_test_revenue_trend_falls_back_to_created_at(monkeypatch))


async def _test_revenue_trend_falls_back_to_created_at(monkeypatch):
    """Legacy invoices without issue_date still count via created_at."""
    legacy = _inv(id="i1")
    del legacy["issue_date"]
    _fake_db(monkeypatch, invoices=[legacy])
    k = await K._compute({"tenant_id": TID}, days=90)
    # invoice is within the last 30 days -> r1 bucket -> 100% trend
    assert k["revenue_trend"]["value"] == 100.0


def test_recurring_share_reads_active_flag(monkeypatch):
    asyncio.run(_test_recurring_share_reads_active_flag(monkeypatch))


async def _test_recurring_share_reads_active_flag(monkeypatch):
    """recurring_share uses active: bool (not a status string)."""
    recs = [{"id": "r1", "amount_cents": 5000, "active": True},
            {"id": "r2", "amount_cents": 5000, "active": False}]
    _fake_db(monkeypatch, invoices=[_inv()], recurring=recs)
    k = await K._compute({"tenant_id": TID}, days=90)
    # 5000 recurring of 10000 invoiced -> 50%
    assert k["recurring_share"]["value"] == 50.0


# --------------------------------------- Fix 2: owner permission lookups

def test_ledger_admin_permits_owner_only():
    assert PERMISSIONS["ledger:admin"] == {"owner"}
    assert PERMISSIONS["settings:write"] == {"owner"}


def test_owner_passes_revalue_permission():
    asyncio.run(_test_owner_passes_revalue_permission())


async def _test_owner_passes_revalue_permission():
    dep = require("ledger:admin")  # what POST /fx/revalue now requires
    assert await dep(OWNER) is OWNER


def test_non_owner_denied_revalue_permission():
    asyncio.run(_test_non_owner_denied_revalue_permission())


async def _test_non_owner_denied_revalue_permission():
    dep = require("ledger:admin")
    with pytest.raises(HTTPException) as exc:
        await dep(BARBER)
    assert exc.value.status_code == 403


def test_owner_passes_benchmark_optin_permission():
    asyncio.run(_test_owner_passes_benchmark_optin_permission())


async def _test_owner_passes_benchmark_optin_permission():
    dep = require("settings:write")  # what POST /benchmarks/opt-in now requires
    assert await dep(OWNER) is OWNER


def test_no_role_named_owner_permission_key():
    """Guard against require("<role>") misuse: 'owner' is a role, not a permission."""
    import pathlib
    for path in (pathlib.Path(dbmod.__file__).parent / "routers").glob("*.py"):
        src = path.read_text()
        assert 'require("owner")' not in src, f"{path.name} still uses require(\"owner\")"
        assert "require('owner')" not in src, f"{path.name} still uses require('owner')"
