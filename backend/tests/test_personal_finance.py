"""Personal finance layer (slice 1): household gate + creation, net-worth
math, balance-history replay, cash-flow transfer exclusion, rollover budget
carry (incl. negative), recurring due flags, goal progress, tenant isolation.

No live MongoDB or network needed (db collections are faked).

Run from the backend directory with:
    python -m pytest tests/test_personal_finance.py
"""
import os
from datetime import date, timedelta, timezone, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from routers import personal as P
from routers.personal import (AssetIn, GoalIn, HouseholdIn, OpeningIn,
                              PersonalBudgetIn, RecurringBillIn)

HOUSE = "tenant-house"
OTHER = "tenant-other"
BIZ = "tenant-biz"
OWNER = {"id": "u1", "email": "owner@home.test", "tenant_id": HOUSE, "role": "owner"}
BIZ_OWNER = {"id": "u1", "email": "owner@home.test", "tenant_id": BIZ, "role": "owner"}


# ---------------------------------------------------------------- fakes

class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec):
            self.docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                           reverse=(direction == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=None):
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
                    if op == "$gt" and not (doc.get(key) is not None and doc.get(key) > val):
                        return False
                    if op == "$lt" and not (doc.get(key) is not None and doc.get(key) < val):
                        return False
                    if op == "$lte" and not (doc.get(key) is not None and doc.get(key) <= val):
                        return False
                    if op == "$in" and doc.get(key) not in val:
                        return False
                    if op == "$ne" and doc.get(key) == val:
                        return False
            elif doc.get(key) != cond:
                return False
        return True

    def find(self, filt=None, projection=None):
        self.seen_filters.append(filt or {})
        return FakeCursor([d for d in self.docs if self._match(d, filt)])

    async def find_one(self, filt=None, projection=None):
        self.seen_filters.append(filt or {})
        for d in self.docs:
            if self._match(d, filt):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, filt, update, upsert=False):
        self.seen_filters.append(filt or {})
        for d in self.docs:
            if self._match(d, filt):
                d.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            d = dict(filt)
            d.update(update.get("$setOnInsert", {}))
            d.update(update.get("$set", {}))
            self.docs.append(d)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=d.get("id"))
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, filt):
        self.seen_filters.append(filt or {})
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, filt)]
        return SimpleNamespace(deleted_count=before - len(self.docs))


def _fake_db(monkeypatch, **collections):
    base = dict(
        tenants=FakeCollection([{"id": HOUSE, "name": "Home", "kind": "household"},
                                {"id": BIZ, "name": "Shop"}]),  # no kind = business
        users=FakeCollection([{"id": "u1", "email": "owner@home.test",
                               "tenant_id": BIZ, "role": "owner",
                               "memberships": [{"tenant_id": BIZ, "role": "owner"}]}]),
        accounts=FakeCollection(),
        bank_accounts=FakeCollection(),
        bank_transactions=FakeCollection(),
        personal_budgets=FakeCollection(),
        personal_recurring_bills=FakeCollection(),
        personal_goals=FakeCollection(),
        personal_assets=FakeCollection(),
        personal_account_meta=FakeCollection(),
    )
    base.update(collections)
    fake = SimpleNamespace(**base)
    monkeypatch.setattr(P, "db", fake)
    return fake


def _txn(**kw):
    d = {"id": "t1", "tenant_id": HOUSE, "bank_account_id": "ba1",
         "txn_date": "2026-11-05", "description": "x", "amount_cents": -1000}
    d.update(kw)
    return d


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------- gate

def test_business_tenant_gets_403(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        for call in (P.get_net_worth(BIZ_OWNER),
                     P.get_cash_flow("2026-11", BIZ_OWNER),
                     P.list_goals(BIZ_OWNER),
                     P.get_personal_budgets("2026-11", BIZ_OWNER)):
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 403
    _run(run())


def test_missing_tenant_gets_403(monkeypatch):
    async def run():
        _fake_db(monkeypatch, tenants=FakeCollection())
        with pytest.raises(HTTPException) as exc:
            await P.get_net_worth(OWNER)
        assert exc.value.status_code == 403
    _run(run())


# ---------------------------------------------------------------- household creation

def test_household_creation_stamps_kind_membership_and_seeds(monkeypatch):
    async def run():
        fake = _fake_db(monkeypatch)
        doc = await P.create_household(HouseholdIn(name="The Home"), BIZ_OWNER)
        assert doc["kind"] == "household"
        assert doc["name"] == "The Home"
        assert doc["niche_id"]  # a niche was stamped (default niche)
        stored = [t for t in fake.tenants.docs if t["id"] == doc["id"]]
        assert len(stored) == 1 and stored[0]["kind"] == "household"
        # owner membership appended to the user doc
        user_doc = fake.users.docs[0]
        assert {"tenant_id": doc["id"], "role": "owner"} in user_doc["memberships"]
        # base COA seeded for the new tenant
        seeded = [a for a in fake.accounts.docs if a.get("tenant_id") == doc["id"]]
        assert len(seeded) >= 40
        codes = {a["code"] for a in seeded}
        assert "1020" in codes and "2500" in codes  # bank asset + loan liability
    _run(run())


# ---------------------------------------------------------------- net worth

def test_net_worth_math_includes_manual_items(monkeypatch):
    async def run():
        _fake_db(monkeypatch, personal_assets=FakeCollection([
            {"id": "a1", "tenant_id": HOUSE, "name": "House", "kind": "property",
             "value_cents": 1000000, "as_of_date": "2026-11-01"},
            {"id": "a2", "tenant_id": HOUSE, "name": "Car loan", "kind": "loan",
             "value_cents": 400000, "as_of_date": "2026-11-01"},
            {"id": "a3", "tenant_id": OTHER, "name": "Not mine", "kind": "property",
             "value_cents": 999999, "as_of_date": "2026-11-01"},
        ]))
        monkeypatch.setattr(P.L, "_balances", AsyncMock(return_value={
            "1020": {"debit": 500000, "credit": 0},     # asset
            "2050": {"debit": 0, "credit": 120000},     # liability (credit card)
            "5000": {"debit": 7000, "credit": 0},       # expense -> excluded
            "4900": {"debit": 3000, "credit": 0},       # revenue -> excluded
        }))
        d = await P.get_net_worth(OWNER)
        assert d["gl_assets_cents"] == 500000
        assert d["gl_liabilities_cents"] == 120000
        assert d["manual_assets_cents"] == 1000000
        assert d["manual_liabilities_cents"] == 400000
        assert d["assets_cents"] == 1500000
        assert d["liabilities_cents"] == 520000
        assert d["net_worth_cents"] == 980000
        assert {a["code"] for a in d["accounts"]} == {"1020", "2050"}
    _run(run())


# ---------------------------------------------------------------- balance history

def test_balance_history_replay(monkeypatch):
    async def run():
        _fake_db(monkeypatch,
                 bank_accounts=FakeCollection([
                     {"id": "ba1", "tenant_id": HOUSE, "name": "Checking",
                      "kind": "bank", "gl_code": "1020"},
                     {"id": "ba2", "tenant_id": HOUSE, "name": "Savings",
                      "kind": "bank", "gl_code": "1021"},
                 ]),
                 personal_account_meta=FakeCollection([
                     {"id": "m1", "tenant_id": HOUSE, "bank_account_id": "ba1",
                      "opening_balance_cents": 100000, "opening_date": "2026-11-01"},
                 ]),
                 bank_transactions=FakeCollection([
                     _txn(id="t1", bank_account_id="ba1", txn_date="2026-11-02", amount_cents=5000),
                     _txn(id="t2", bank_account_id="ba1", txn_date="2026-11-03", amount_cents=-2000),
                     _txn(id="t3", bank_account_id="ba1", txn_date="2026-11-03", amount_cents=-500),
                     _txn(id="t4", bank_account_id="ba2", txn_date="2026-11-03", amount_cents=1000),
                     _txn(id="t5", bank_account_id="ba1", txn_date="2026-12-25", amount_cents=777),
                 ]),
                 personal_assets=FakeCollection([
                     {"id": "a1", "tenant_id": HOUSE, "name": "House", "kind": "property",
                      "value_cents": 10000, "as_of_date": "2026-11-01"},
                     {"id": "a2", "tenant_id": HOUSE, "name": "Loan", "kind": "loan",
                      "value_cents": 3000, "as_of_date": "2026-11-01"},
                 ]))
        d = await P.get_balance_history(date_from=None, date_to="2026-11-04", user=OWNER)
        accts = {a["bank_account_id"]: a for a in d["accounts"]}
        pts1 = {p["date"]: p["balance_cents"] for p in accts["ba1"]["points"]}
        assert pts1 == {"2026-11-01": 100000, "2026-11-02": 105000,
                        "2026-11-03": 102500, "2026-11-04": 102500}
        assert accts["ba1"]["current_balance_cents"] == 102500  # Dec txn not in window
        pts2 = {p["date"]: p["balance_cents"] for p in accts["ba2"]["points"]}
        assert pts2 == {"2026-11-03": 1000, "2026-11-04": 1000}  # default opening 0
        comb = {p["date"]: p for p in d["combined"]["points"]}
        assert d["combined"]["manual_items_held_flat"] is True
        assert d["combined"]["manual_net_cents"] == 7000
        assert comb["2026-11-03"]["accounts_balance_cents"] == 103500
        assert comb["2026-11-04"]["net_worth_cents"] == 103500 + 7000
    _run(run())


# ---------------------------------------------------------------- cash flow

def test_cash_flow_excludes_transfers(monkeypatch):
    async def run():
        _fake_db(monkeypatch, bank_transactions=FakeCollection([
            _txn(id="t1", txn_date="2026-11-01", amount_cents=200000),
            _txn(id="t2", txn_date="2026-11-02", amount_cents=-50000),
            _txn(id="t3", txn_date="2026-11-03", amount_cents=-30000, is_transfer=True),
            _txn(id="t4", txn_date="2026-11-03", amount_cents=30000, is_transfer=True),
            _txn(id="t5", txn_date="2026-10-31", amount_cents=-999),  # other month
            _txn(id="t6", tenant_id=OTHER, txn_date="2026-11-04", amount_cents=-888),
        ]))
        d = await P.get_cash_flow("2026-11", OWNER)
        assert d == {"period": "2026-11", "in_cents": 200000, "out_cents": 50000,
                     "net_cents": 150000, "transaction_count": 2}
        with pytest.raises(HTTPException) as exc:
            await P.get_cash_flow("2026-13", OWNER)
        assert exc.value.status_code == 400
    _run(run())


# ---------------------------------------------------------------- budgets + rollover

def _budget_db(monkeypatch, txns):
    return _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))


def test_budget_rollover_carries_leftover(monkeypatch):
    async def run():
        _budget_db(monkeypatch, [
            _txn(id="t1", txn_date="2026-10-10", amount_cents=-60000, category_name="Groceries"),
            _txn(id="t2", txn_date="2026-11-05", amount_cents=-10000, category_name="Groceries"),
            # category resolved from the GL code the pipeline set (5700 = Supplies & Office)
            _txn(id="t3", txn_date="2026-10-11", amount_cents=-5000, category_code="5700"),
            _txn(id="t4", txn_date="2026-10-12", amount_cents=-7000, category_name="Groceries",
                 is_transfer=True),  # transfers never count as spend
        ])
        await P.upsert_personal_budget(
            PersonalBudgetIn(category="Groceries", period="2026-10",
                             amount_cents=100000, rollover=True), OWNER)
        await P.upsert_personal_budget(
            PersonalBudgetIn(category="Supplies & Office", period="2026-10",
                             amount_cents=20000, rollover=True), OWNER)
        d = await P.get_personal_budgets("2026-11", OWNER)
        rows = {r["category"]: r for r in d["rows"]}
        g = rows["Groceries"]  # Oct: 100000 - 60000 = 40000 carried into Nov (no Nov line)
        assert g["carried_in_cents"] == 40000
        assert g["budgeted_cents"] == 40000
        assert g["spent_cents"] == 10000
        assert g["remaining_cents"] == 30000
        s = rows["Supplies & Office"]  # Oct: 20000 - 5000 = 15000 carried
        assert s["carried_in_cents"] == 15000 and s["budgeted_cents"] == 15000
        assert d["totals"]["budgeted_cents"] == 55000
    _run(run())


def test_budget_rollover_off_carries_nothing(monkeypatch):
    async def run():
        _budget_db(monkeypatch, [
            _txn(id="t1", txn_date="2026-10-10", amount_cents=-20000, category_name="Dining"),
        ])
        await P.upsert_personal_budget(
            PersonalBudgetIn(category="Dining", period="2026-10",
                             amount_cents=50000, rollover=False), OWNER)
        await P.upsert_personal_budget(
            PersonalBudgetIn(category="Dining", period="2026-11",
                             amount_cents=50000, rollover=False), OWNER)
        d = await P.get_personal_budgets("2026-11", OWNER)
        row = {r["category"]: r for r in d["rows"]}["Dining"]
        assert row["carried_in_cents"] == 0
        assert row["budgeted_cents"] == 50000  # set amount only, Oct leftover dropped
    _run(run())


def test_budget_overspend_carries_negative(monkeypatch):
    async def run():
        _budget_db(monkeypatch, [
            _txn(id="t1", txn_date="2026-10-10", amount_cents=-25000, category_name="Fun"),
        ])
        await P.upsert_personal_budget(
            PersonalBudgetIn(category="Fun", period="2026-10",
                             amount_cents=10000, rollover=True), OWNER)
        d = await P.get_personal_budgets("2026-11", OWNER)
        row = {r["category"]: r for r in d["rows"]}["Fun"]
        assert row["carried_in_cents"] == -15000
        assert row["budgeted_cents"] == -15000
        assert row["remaining_cents"] == -15000
    _run(run())


def test_budget_unbudgeted_spend_still_appears(monkeypatch):
    async def run():
        _budget_db(monkeypatch, [
            _txn(id="t1", txn_date="2026-11-10", amount_cents=-4200, category_name="Surprise"),
        ])
        d = await P.get_personal_budgets("2026-11", OWNER)
        row = {r["category"]: r for r in d["rows"]}["Surprise"]
        assert row["budgeted_cents"] == 0 and row["spent_cents"] == 4200
        assert row["remaining_cents"] == -4200
    _run(run())


def test_budget_upsert_updates_and_delete_404s_for_other_tenant(monkeypatch):
    async def run():
        fake = _budget_db(monkeypatch, [])
        doc = await P.upsert_personal_budget(
            PersonalBudgetIn(category="Groceries", period="2026-11",
                             amount_cents=10000, rollover=True), OWNER)
        doc2 = await P.upsert_personal_budget(
            PersonalBudgetIn(category="Groceries", period="2026-11",
                             amount_cents=12000, rollover=False), OWNER)
        assert doc2["id"] == doc["id"] and doc2["amount_cents"] == 12000
        assert len(fake.personal_budgets.docs) == 1
        # a line owned by another tenant cannot be deleted through this tenant
        fake.personal_budgets.docs.append(
            {"id": "alien", "tenant_id": OTHER, "category": "X", "period": "2026-11",
             "amount_cents": 1, "rollover": False})
        with pytest.raises(HTTPException) as exc:
            await P.delete_personal_budget("alien", OWNER)
        assert exc.value.status_code == 404
        assert await P.delete_personal_budget(doc["id"], OWNER) == {"ok": True}
    _run(run())


# ---------------------------------------------------------------- recurring bills

def test_recurring_due_flags(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        today = _today()
        await P.create_recurring_bill(RecurringBillIn(
            name="Rent", amount_cents=150000, cadence="monthly",
            next_due_date=(today - timedelta(days=1)).isoformat()), OWNER)
        await P.create_recurring_bill(RecurringBillIn(
            name="Streaming", amount_cents=1500, cadence="monthly",
            next_due_date=(today + timedelta(days=3)).isoformat()), OWNER)
        await P.create_recurring_bill(RecurringBillIn(
            name="Insurance", amount_cents=20000, cadence="quarterly",
            next_due_date=(today + timedelta(days=30)).isoformat()), OWNER)
        rows = await P.list_recurring_bills(OWNER)
        states = {r["name"]: (r["due_state"], r["days_until_due"]) for r in rows}
        assert states["Rent"] == ("overdue", -1)
        assert states["Streaming"] == ("due_soon", 3)
        assert states["Insurance"] == ("upcoming", 30)
        with pytest.raises(HTTPException) as exc:
            await P.create_recurring_bill(RecurringBillIn(
                name="Bad", amount_cents=1, cadence="monthly",
                next_due_date="next Friday"), OWNER)
        assert exc.value.status_code == 400
    _run(run())


# ---------------------------------------------------------------- goals

def test_goal_progress(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        await P.create_goal(GoalIn(name="Emergency fund", target_cents=100000,
                                   saved_cents=25000, target_date="2027-01-01"), OWNER)
        await P.create_goal(GoalIn(name="Trip", target_cents=50000), OWNER)
        rows = {g["name"]: g for g in await P.list_goals(OWNER)}
        assert rows["Emergency fund"]["progress_pct"] == 25.0
        assert rows["Emergency fund"]["remaining_cents"] == 75000
        assert rows["Trip"]["progress_pct"] == 0.0
        gid = rows["Trip"]["id"]
        assert await P.delete_goal(gid, OWNER) == {"ok": True}
        with pytest.raises(HTTPException) as exc:
            await P.delete_goal(gid, OWNER)
        assert exc.value.status_code == 404
    _run(run())


# ---------------------------------------------------------------- assets + opening meta

def test_assets_crud_and_opening_meta(monkeypatch):
    async def run():
        fake = _fake_db(monkeypatch, bank_accounts=FakeCollection([
            {"id": "ba1", "tenant_id": HOUSE, "name": "Checking", "kind": "bank",
             "gl_code": "1020"},
            {"id": "baX", "tenant_id": OTHER, "name": "Alien", "kind": "bank",
             "gl_code": "1020"},
        ]))
        a = await P.create_asset(AssetIn(name="House", kind="property",
                                         value_cents=50000000), OWNER)
        assert a["as_of_date"]  # defaults to today
        rows = await P.list_assets(OWNER)
        assert [r["name"] for r in rows] == ["House"]
        # opening meta: unknown / other-tenant account -> 404
        with pytest.raises(HTTPException) as exc:
            await P.set_account_opening("baX", OpeningIn(
                opening_balance_cents=100, opening_date="2026-11-01"), OWNER)
        assert exc.value.status_code == 404
        m1 = await P.set_account_opening("ba1", OpeningIn(
            opening_balance_cents=100, opening_date="2026-11-01"), OWNER)
        m2 = await P.set_account_opening("ba1", OpeningIn(
            opening_balance_cents=250, opening_date="2026-10-15"), OWNER)
        assert m2["id"] == m1["id"] and m2["opening_balance_cents"] == 250
        assert len(fake.personal_account_meta.docs) == 1  # upserted, not duplicated
        assert await P.delete_asset(a["id"], OWNER) == {"ok": True}
        assert await P.list_assets(OWNER) == []
    _run(run())


# ---------------------------------------------------------------- isolation

def test_cross_tenant_isolation(monkeypatch):
    async def run():
        fake = _fake_db(
            monkeypatch,
            personal_budgets=FakeCollection([
                {"id": "b-other", "tenant_id": OTHER, "category": "AlienCat",
                 "period": "2026-11", "amount_cents": 99900, "rollover": True},
                {"id": "b-mine", "tenant_id": HOUSE, "category": "MineCat",
                 "period": "2026-11", "amount_cents": 5000, "rollover": False},
            ]),
            personal_assets=FakeCollection([
                {"id": "a-other", "tenant_id": OTHER, "name": "AlienHouse",
                 "kind": "property", "value_cents": 12300, "as_of_date": "2026-11-01"},
            ]),
            personal_recurring_bills=FakeCollection([
                {"id": "r-other", "tenant_id": OTHER, "name": "AlienBill",
                 "category": None, "amount_cents": 100, "cadence": "monthly",
                 "next_due_date": "2026-11-20", "active": True},
            ]),
            bank_transactions=FakeCollection([
                _txn(id="t-other", tenant_id=OTHER, txn_date="2026-11-03",
                     amount_cents=-99900, category_name="AlienCat"),
            ]))
        d = await P.get_personal_budgets("2026-11", OWNER)
        assert {r["category"] for r in d["rows"]} == {"MineCat"}
        assert await P.list_assets(OWNER) == []
        assert await P.list_recurring_bills(OWNER) == []
        # every read carried this tenant's stamp
        for coll in (fake.personal_budgets, fake.personal_assets,
                     fake.personal_recurring_bills, fake.bank_transactions):
            for filt in coll.seen_filters:
                assert filt.get("tenant_id") == HOUSE
    _run(run())


import asyncio as _asyncio  # noqa: E402
_LOOP = _asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)
