"""Budgeting / forecasting: budget CRUD, copy-forward, variance math, quarterly
periods, annual budget-vs-actual, tenant isolation.

No live MongoDB or network needed (db collections are faked).

Run from the backend directory with:
    python -m pytest tests/test_budgeting.py
"""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from routers import budget as B
from routers import reports as R
from routers.budget import BudgetIn, CopyIn

TID = "tenant-1"
TID2 = "tenant-2"
OWNER = {"id": "u1", "email": "owner@shop.test", "tenant_id": TID, "role": "owner"}


# ---------------------------------------------------------------- fakes

class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec):
            self.docs.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=0):
        return self.docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]
        self.seen_filters = []

    def _match(self, doc, filt):
        for key, cond in (filt or {}).items():
            if isinstance(cond, dict):
                for op, val in cond.items():
                    if op == "$gte" and not (doc.get(key) is not None and doc.get(key) >= val):
                        return False
                    if op == "$lt" and not (doc.get(key) is not None and doc.get(key) < val):
                        return False
                    if op == "$lte" and not (doc.get(key) is not None and doc.get(key) <= val):
                        return False
                    if op == "$in" and doc.get(key) not in val:
                        return False
            elif doc.get(key) != cond:
                return False
        return True

    def find(self, filt=None, projection=None):
        self.seen_filters.append(filt or {})
        return FakeCursor([d for d in self.docs if self._match(d, filt)])

    async def find_one_and_update(self, filt, update, upsert=False,
                                  return_document=False, projection=None):
        self.seen_filters.append(filt)
        for d in self.docs:
            if self._match(d, filt):
                d.update(update.get("$set", {}))
                for k, v in update.get("$setOnInsert", {}).items():
                    d.setdefault(k, v)
                return dict(d)
        if upsert:
            d = dict(filt)
            d.update(update.get("$setOnInsert", {}))
            d.update(update.get("$set", {}))
            self.docs.append(d)
            return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def delete_one(self, filt):
        self.seen_filters.append(filt)
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, filt)]
        return SimpleNamespace(deleted_count=before - len(self.docs))


def _fake_budget_db(monkeypatch, budgets=(), expenses=()):
    fake = SimpleNamespace(
        monthly_budgets=FakeCollection(budgets),
        expenses=FakeCollection(expenses),
    )
    monkeypatch.setattr(B, "db", fake)
    monkeypatch.setattr(B.L, "audit", AsyncMock())
    return fake


def _exp(**kw):
    d = {"id": "e1", "tenant_id": TID, "category_name": "Supplies",
         "amount_cents": 7000, "expense_date": "2026-10-15"}
    d.update(kw)
    return d


def _bud(**kw):
    d = {"id": "b1", "tenant_id": TID, "period": "2026-10",
         "category": "Supplies", "amount_cents": 10000}
    d.update(kw)
    return d


# ---------------- period parsing ----------------

def test_period_accepts_month_and_quarter():
    assert B._period("2026-10") == "2026-10"
    assert B._period("2026-Q2") == "2026-Q2"
    assert B._period("2026-Q4") == "2026-Q4"


def test_period_rejects_junk():
    for bad in ("2026-13", "2026-Q5", "2026-Q0", "2026", "Oct 2026", "2026-1", ""):
        with pytest.raises(HTTPException) as exc:
            B._period(bad)
        assert exc.value.status_code == 400


def test_period_range_month_and_quarter():
    assert B._period_range("2026-10") == ("2026-10-01", "2026-11-01")
    assert B._period_range("2026-12") == ("2026-12-01", "2027-01-01")
    assert B._period_range("2026-Q1") == ("2026-01-01", "2026-04-01")
    assert B._period_range("2026-Q2") == ("2026-04-01", "2026-07-01")
    assert B._period_range("2026-Q4") == ("2026-10-01", "2027-01-01")


# ---------------- CRUD ----------------

def test_upsert_creates_then_updates_same_line(monkeypatch):
    async def run():
        fake = _fake_budget_db(monkeypatch)
        doc = await B.upsert_budget(BudgetIn(period="2026-10", category="Supplies", amount=100.0), OWNER)
        assert doc["amount_cents"] == 10000
        doc2 = await B.upsert_budget(BudgetIn(period="2026-10", category="Supplies", amount=150.0), OWNER)
        assert doc2["amount_cents"] == 15000
        assert len(fake.monthly_budgets.docs) == 1  # updated, not duplicated
        B.L.audit.assert_awaited()
    _run(run())


def test_upsert_rejects_bad_period(monkeypatch):
    async def run():
        _fake_budget_db(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            await B.upsert_budget(BudgetIn(period="2026-Q9", category="Supplies", amount=10.0), OWNER)
        assert exc.value.status_code == 400
    _run(run())


def test_delete_ok_and_missing_404(monkeypatch):
    async def run():
        _fake_budget_db(monkeypatch, budgets=[_bud()])
        r = await B.delete_budget("b1", OWNER)
        assert r == {"ok": True}
        with pytest.raises(HTTPException) as exc:
            await B.delete_budget("b1", OWNER)
        assert exc.value.status_code == 404
    _run(run())


# ---------------- copy-forward ----------------

def test_copy_forward_skips_existing_and_rejects_same_period(monkeypatch):
    async def run():
        fake = _fake_budget_db(monkeypatch, budgets=[
            _bud(id="b1", period="2026-10", category="Supplies", amount_cents=10000),
            _bud(id="b2", period="2026-10", category="Travel", amount_cents=5000),
            _bud(id="b3", period="2026-11", category="Supplies", amount_cents=9000),
        ])
        r = await B.copy_budget(CopyIn(from_period="2026-10", to_period="2026-11"), OWNER)
        assert r["copied"] == 1  # Travel copied; Supplies already had a line
        nov = [d for d in fake.monthly_budgets.docs if d["period"] == "2026-11"]
        assert {d["category"]: d["amount_cents"] for d in nov} == \
            {"Supplies": 9000, "Travel": 5000}
        with pytest.raises(HTTPException) as exc:
            await B.copy_budget(CopyIn(from_period="2026-10", to_period="2026-10"), OWNER)
        assert exc.value.status_code == 400
    _run(run())


def test_copy_forward_into_next_quarter(monkeypatch):
    async def run():
        fake = _fake_budget_db(monkeypatch, budgets=[
            _bud(id="b1", period="2026-Q3", category="Supplies", amount_cents=30000)])
        r = await B.copy_budget(CopyIn(from_period="2026-Q3", to_period="2026-Q4"), OWNER)
        assert r["copied"] == 1
        q4 = [d for d in fake.monthly_budgets.docs if d["period"] == "2026-Q4"]
        assert len(q4) == 1 and q4[0]["amount_cents"] == 30000
    _run(run())


# ---------------- variance math (monthly/quarterly) ----------------

def test_variance_math_and_zero_budget_edge(monkeypatch):
    async def run():
        _fake_budget_db(monkeypatch, budgets=[
            _bud(id="b1", category="Supplies", amount_cents=10000),
            _bud(id="b2", category="Travel", amount_cents=5000),
        ], expenses=[
            _exp(category_name="Supplies", amount_cents=7000),
            _exp(id="e2", category_name="Travel", amount_cents=6000),
            _exp(id="e3", category_name="Surprise", amount_cents=2000),
        ])
        d = await B.get_budget("2026-10", OWNER)
        rows = {r["category"]: r for r in d["rows"]}
        assert d["granularity"] == "month"

        sup = rows["Supplies"]  # 10000 budget, 7000 actual -> 3000 under, 30.0%
        assert sup["budgeted_cents"] == 10000 and sup["actual_cents"] == 7000
        assert sup["variance_cents"] == 3000 and sup["variance_pct"] == 30.0

        trv = rows["Travel"]  # 5000 budget, 6000 actual -> -1000 over, -20.0%
        assert trv["variance_cents"] == -1000 and trv["variance_pct"] == -20.0

        sur = rows["Surprise"]  # 0 budget, 2000 actual -> pct must be None, not div-by-zero
        assert sur["budgeted_cents"] == 0 and sur["actual_cents"] == 2000
        assert sur["variance_cents"] == -2000 and sur["variance_pct"] is None

        t = d["totals"]
        assert t["variance_cents"] == 15000 - 15000 == 0
        assert t["variance_pct"] == 0.0
    _run(run())


def test_zero_budget_and_zero_actual_pct_is_none(monkeypatch):
    async def run():
        _fake_budget_db(monkeypatch, budgets=[_bud(amount_cents=0)], expenses=[])
        d = await B.get_budget("2026-10", OWNER)
        rows = {r["category"]: r for r in d["rows"]}
        assert rows["Supplies"]["variance_pct"] is None
        assert d["totals"]["variance_pct"] is None
    _run(run())


def test_quarter_aggregates_all_three_months(monkeypatch):
    async def run():
        fake = _fake_budget_db(monkeypatch, budgets=[
            _bud(id="b1", period="2026-Q2", category="Supplies", amount_cents=30000)],
            expenses=[
                _exp(id="e1", expense_date="2026-04-10", amount_cents=4000),
                _exp(id="e2", expense_date="2026-05-10", amount_cents=5000),
                _exp(id="e3", expense_date="2026-06-10", amount_cents=6000),
                _exp(id="e4", expense_date="2026-07-10", amount_cents=999),  # Q3 -> excluded
            ])
        d = await B.get_budget("2026-Q2", OWNER)
        assert d["granularity"] == "quarter"
        rows = {r["category"]: r for r in d["rows"]}
        assert rows["Supplies"]["actual_cents"] == 15000
        assert rows["Supplies"]["variance_cents"] == 15000
        assert rows["Supplies"]["variance_pct"] == 50.0
        # expense query was scoped to the quarter window
        filt = fake.expenses.seen_filters[0]
        assert filt["expense_date"] == {"$gte": "2026-04-01", "$lt": "2026-07-01"}
    _run(run())


# ---------------- annual budget-vs-actual ----------------

def test_annual_variance_pct_and_zero_budget_edge(monkeypatch):
    async def run():
        budgets = SimpleNamespace()
        budgets_c = FakeCollection([
            {"id": "a1", "tenant_id": TID, "year": "2026",
             "account_code": "4000", "amount_cents": 180000},
            {"id": "a2", "tenant_id": TID, "year": "2026",
             "account_code": "5000", "amount_cents": 50000},
        ])
        fake = SimpleNamespace(budgets=budgets_c)
        monkeypatch.setattr(R, "db", fake)
        # actuals come from the journal (revenue lines debit the revenue account
        # per this repo's ledger convention): 4000 revenue 200000, 5000 expense 40000
        monkeypatch.setattr(R.L, "_balances", AsyncMock(return_value={
            "4000": {"debit": 200000, "credit": 0},
            "5000": {"debit": 40000, "credit": 0},
            "6100": {"debit": 1000, "credit": 0},  # unknown type -> excluded
        }))
        d = await R.budget_vs_actual("2026", OWNER)
        rows = {r["code"]: r for r in d["rows"]}

        rev = rows["4000"]  # revenue: actual 200000 - budget 180000 = +20000
        assert rev["variance_cents"] == 20000 and rev["variance_pct"] == 11.1

        exp = rows["5000"]  # expense: budget 50000 - actual 40000 = +10000 under
        assert exp["variance_cents"] == 10000 and exp["variance_pct"] == 20.0

        assert "6100" not in rows  # not revenue/expense -> excluded
    _run(run())


def test_annual_unbudgeted_account_pct_is_none(monkeypatch):
    async def run():
        fake = SimpleNamespace(budgets=FakeCollection([]))
        monkeypatch.setattr(R, "db", fake)
        monkeypatch.setattr(R.L, "_balances", AsyncMock(return_value={
            "4000": {"debit": 100000, "credit": 0},
        }))
        d = await R.budget_vs_actual("2026", OWNER)
        rows = {r["code"]: r for r in d["rows"]}
        assert rows["4000"]["budget_cents"] == 0
        assert rows["4000"]["variance_cents"] == 100000
        assert rows["4000"]["variance_pct"] is None  # no div-by-zero
    _run(run())


# ---------------- tenant isolation ----------------

def test_queries_are_tenant_scoped(monkeypatch):
    async def run():
        fake = _fake_budget_db(monkeypatch, budgets=[
            _bud(tenant_id=TID2, period="2026-10"),  # other tenant's budget
        ], expenses=[
            _exp(tenant_id=TID2),
        ])
        d = await B.get_budget("2026-10", OWNER)
        assert d["rows"] == []  # other tenant's docs invisible
        for filt in fake.monthly_budgets.seen_filters + fake.expenses.seen_filters:
            assert filt.get("tenant_id") == TID
    _run(run())


def test_delete_cannot_touch_other_tenants_line(monkeypatch):
    async def run():
        _fake_budget_db(monkeypatch, budgets=[_bud(tenant_id=TID2)])
        with pytest.raises(HTTPException) as exc:
            await B.delete_budget("b1", OWNER)
        assert exc.value.status_code == 404
    _run(run())


import asyncio as _asyncio  # noqa: E402
_LOOP = _asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)
