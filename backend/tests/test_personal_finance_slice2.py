"""Personal finance layer (slice 2): recurring-bill auto-detection
(suggest-and-confirm), goal self-funding (idempotent catch-up), loan
amortization schedules, multi-currency net-worth conversion, household
settings, insights, and the one-call summary. The household gate must
hold for every new endpoint.

No live MongoDB or network needed (db collections are faked).

Run from the backend directory with:
    python -m pytest tests/test_personal_finance_slice2.py
"""
import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from routers import personal as P
from routers.personal import (AssetIn, GoalIn, HouseholdSettingsIn,
                              OpeningIn, RecurringBillIn)
from test_personal_finance import (BIZ, BIZ_OWNER, HOUSE, OTHER, OWNER,
                                   FakeCollection, _run, _txn)


def _fake_db(monkeypatch, **collections):
    base = dict(
        tenants=FakeCollection([{"id": HOUSE, "name": "Home", "kind": "household",
                                 "settings": {}},
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
        personal_goal_funding_events=FakeCollection(),
    )
    base.update(collections)
    fake = SimpleNamespace(**base)
    monkeypatch.setattr(P, "db", fake)
    return fake


def _monthly_txns(desc, amount, months, day=5, **kw):
    out = []
    for i, (y, m) in enumerate(months):
        out.append(_txn(id=f"{desc}-{i}", txn_date=f"{y:04d}-{m:02d}-{day:02d}",
                        description=desc, amount_cents=amount, **kw))
    return out


# ---------------------------------------------------------------- gate

def test_slice2_endpoints_all_403_for_business(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        calls = [
            P.detect_recurring_bills(BIZ_OWNER),
            P.fund_due_goals(BIZ_OWNER),
            P.get_insights("2026-11", BIZ_OWNER),
            P.get_summary(BIZ_OWNER),
            P.update_household_settings(HouseholdSettingsIn(base_currency="EUR"), BIZ_OWNER),
            P.get_loan_schedule("nope", BIZ_OWNER),
        ]
        for call in calls:
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 403
    _run(run())


# ---------------------------------------------------------------- detection

def test_detection_finds_monthly_bill_and_skips_irregular(monkeypatch):
    async def run():
        txns = _monthly_txns("NETFLIX.COM", -1599,
                             [(2026, 8), (2026, 9), (2026, 10), (2026, 11)],
                             category_name="Streaming")
        # irregular amounts/dates: a habit, not a bill
        txns += [
            _txn(id="c1", txn_date="2026-09-03", description="COFFEE SHOP", amount_cents=-600),
            _txn(id="c2", txn_date="2026-09-20", description="COFFEE SHOP", amount_cents=-600),
            _txn(id="c3", txn_date="2026-11-01", description="COFFEE SHOP", amount_cents=-600),
        ]
        # only two occurrences: not enough evidence
        txns += _monthly_txns("GYM", -3000, [(2026, 10), (2026, 11)])
        # income and transfers never count
        txns.append(_txn(id="pay", txn_date="2026-11-01", description="PAYROLL",
                         amount_cents=300000))
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))
        d = await P.detect_recurring_bills(OWNER)
        names = {s["merchant_key"] for s in d["suggestions"]}
        assert names == {"NETFLIX COM"}
        s = d["suggestions"][0]
        assert s["cadence"] == "monthly" and s["amount_cents"] == 1599
        assert s["occurrences"] == 4 and s["category"] == "Streaming"
        assert s["suggested_next_due_date"] == "2026-12-05"
        assert s["confidence"] == 0.75
    _run(run())


def test_detection_excludes_already_registered_bills(monkeypatch):
    async def run():
        txns = _monthly_txns("SPOTIFY", -1199, [(2026, 9), (2026, 10), (2026, 11)])
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))
        await P.create_recurring_bill(RecurringBillIn(
            name="Spotify", amount_cents=1199, cadence="monthly",
            next_due_date="2026-12-05", source="detected"), OWNER)
        d = await P.detect_recurring_bills(OWNER)
        assert d["suggestions"] == []
        bills = await P.list_recurring_bills(OWNER)
        assert bills[0]["source"] == "detected"  # confirm path stamps provenance
    _run(run())


def test_detection_weekly_cadence(monkeypatch):
    async def run():
        base = date(2026, 10, 5)
        txns = [_txn(id=f"w{i}", txn_date=(base + timedelta(days=7 * i)).isoformat(),
                     description="CLEANING SVC", amount_cents=-8000)
                for i in range(5)]
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))
        d = await P.detect_recurring_bills(OWNER)
        assert len(d["suggestions"]) == 1
        assert d["suggestions"][0]["cadence"] == "weekly"
        assert d["suggestions"][0]["confidence"] == 0.9
    _run(run())


# ---------------------------------------------------------------- goal funding

def _funded_goal(monkeypatch, start="2026-09-01", amount=10000, target=100000):
    async def make():
        # ba1 must exist: goal creation validates named funding accounts
        # against the household's own bank accounts (completion pass).
        _fake_db(monkeypatch, bank_accounts=FakeCollection([
            {"id": "ba1", "tenant_id": HOUSE, "name": "Checking",
             "kind": "checking"}]))
        return await P.create_goal(GoalIn(
            name="Emergency fund", target_cents=target,
            funding_amount_cents=amount, funding_cadence="monthly",
            funding_start_date=start, auto_fund=True,
            funding_account_id="ba1"), OWNER)
    return _run(make())


def test_goal_funding_catches_up_and_is_idempotent(monkeypatch):
    goal = _funded_goal(monkeypatch)
    async def run():
        fake = P.db  # the fake installed by _funded_goal's _fake_db
        res = await P.fund_due_goals(OWNER)
        # Sep, Oct, Nov 2026 are due relative to the real "today" only if
        # today >= Nov; compute expected from the goal's own event ledger
        # instead of the wall clock: run the internal funder at a fixed date.
        assert res["goals_checked"] == 1
        events = await P.list_goal_funding_events(goal["id"], OWNER)
        assert events, "at least the start month should be funded"
        assert all(e["amount_cents"] == 10000 for e in events)
        saved_after_first = (await P.list_goals(OWNER))[0]["saved_cents"]
        assert saved_after_first == 10000 * len(events)
        res2 = await P.fund_due_goals(OWNER)  # re-run: no double funding
        assert res2["funded_events"] == 0
        assert (await P.list_goals(OWNER))[0]["saved_cents"] == saved_after_first
        assert len(fake.personal_goal_funding_events.docs) == len(events)
    _run(run())


def test_goal_funding_fixed_date_math_and_target_cap(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        goal = await P.create_goal(GoalIn(
            name="Small", target_cents=25000, funding_amount_cents=10000,
            funding_cadence="monthly", funding_start_date="2026-08-15",
            auto_fund=True), OWNER)
        res = await P._fund_goal(OWNER, goal, date(2026, 11, 20))
        # due: Aug 15, Sep 15, Oct 15 (Nov 15 stops: target hit in Oct)
        # amounts 10000 + 10000 + 5000 (capped at the 25000 target)
        assert res["funded_events"] == 3
        assert res["added_cents"] == 25000
        assert res["saved_cents"] == 25000
        again = await P._fund_goal(OWNER, {**goal, "saved_cents": 25000}, date(2026, 12, 20))
        assert again["funded_events"] == 0  # target reached: plan stops
    _run(run())


def test_goal_without_plan_rejected_and_validation(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        plain = await P.create_goal(GoalIn(name="Plain", target_cents=5000), OWNER)
        with pytest.raises(HTTPException) as exc:
            await P.fund_due_goal(plain["id"], OWNER)
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            await P.create_goal(GoalIn(name="Bad", target_cents=5000, auto_fund=True), OWNER)
        assert exc.value.status_code == 400
    _run(run())


def _two_accounts():
    return FakeCollection([
        {"id": "ba-src", "tenant_id": HOUSE, "name": "Checking", "kind": "checking"},
        {"id": "ba-dst", "tenant_id": HOUSE, "name": "Savings", "kind": "savings"},
        {"id": "ba-foreign", "tenant_id": OTHER, "name": "Not yours", "kind": "checking"},
    ])


def test_goal_create_rejects_foreign_same_and_missing_accounts(monkeypatch):
    async def run():
        _fake_db(monkeypatch, bank_accounts=_two_accounts())
        base = dict(name="X", target_cents=5000, auto_fund=True,
                    funding_amount_cents=1000, funding_cadence="monthly")
        for bad in (dict(funding_account_id="ba-foreign"),
                    dict(funding_account_id="nope"),
                    dict(funding_destination_account_id="ba-foreign"),
                    dict(funding_account_id="ba-src",
                         funding_destination_account_id="ba-src")):
            with pytest.raises(HTTPException) as exc:
                await P.create_goal(GoalIn(**base, **bad), OWNER)
            assert exc.value.status_code == 400, bad
        ok = await P.create_goal(GoalIn(
            **base, funding_account_id="ba-src",
            funding_destination_account_id="ba-dst"), OWNER)
        assert ok["funding_destination_account_id"] == "ba-dst"
    _run(run())


def test_goal_funding_writes_transfer_pair_between_own_accounts(monkeypatch):
    async def run():
        fake = _fake_db(monkeypatch, bank_accounts=_two_accounts())
        goal = await P.create_goal(GoalIn(
            name="Trip", target_cents=100000, funding_amount_cents=10000,
            funding_cadence="monthly", funding_start_date="2026-09-01",
            auto_fund=True, funding_account_id="ba-src",
            funding_destination_account_id="ba-dst"), OWNER)
        res = await P._fund_goal(OWNER, goal, date(2026, 11, 18))
        assert res["funded_events"] == 3  # Sep, Oct, Nov
        moves = [t for t in fake.bank_transactions.docs
                 if t.get("source") == "goal_funding"]
        assert len(moves) == 6  # one pair per funded period
        groups = {}
        for t in moves:
            assert t["is_transfer"] is True and t["goal_id"] == goal["id"]
            groups.setdefault(t["transfer_group_id"], []).append(t)
        assert len(groups) == 3
        for pair in groups.values():
            assert sorted(x["amount_cents"] for x in pair) == [-10000, 10000]
            assert {x["bank_account_id"] for x in pair} == {"ba-src", "ba-dst"}
        # Balance replay moves the money: source -30000, destination +30000.
        hist = await P.get_balance_history("2026-09-01", "2026-11-18", OWNER)
        by_id = {a["bank_account_id"]: a for a in hist["accounts"]}
        assert by_id["ba-src"]["current_balance_cents"] == -30000
        assert by_id["ba-dst"]["current_balance_cents"] == 30000
        # Cash flow ignores the moves entirely (own-money transfers).
        cf = await P.get_cash_flow("2026-10", OWNER)
        assert cf["in_cents"] == 0 and cf["out_cents"] == 0
        # Idempotent: a second catch-up writes nothing new.
        res2 = await P._fund_goal(OWNER, goal, date(2026, 11, 18))
        assert res2["funded_events"] == 0
        assert len([t for t in fake.bank_transactions.docs
                    if t.get("source") == "goal_funding"]) == 6
    _run(run())


def test_goal_funding_without_destination_stays_allocation_only(monkeypatch):
    async def run():
        fake = _fake_db(monkeypatch, bank_accounts=_two_accounts())
        goal = await P.create_goal(GoalIn(
            name="Buffer", target_cents=50000, funding_amount_cents=5000,
            funding_cadence="monthly", funding_start_date="2026-10-01",
            auto_fund=True, funding_account_id="ba-src"), OWNER)
        res = await P._fund_goal(OWNER, goal, date(2026, 11, 18))
        assert res["funded_events"] == 2 and res["added_cents"] == 10000
        assert fake.bank_transactions.docs == []  # no money moved
    _run(run())


# ---------------------------------------------------------------- loan schedule

def test_amortization_zero_rate_and_interest_math():
    s = P._amortization(120000, 0, 12, date(2026, 1, 15))
    assert s["payment_cents"] == 10000 and s["total_interest_cents"] == 0
    assert s["rows"][-1]["balance_cents"] == 0 and s["months"] == 12

    s = P._amortization(100000, 1200, 12, date(2026, 1, 15))  # 12% APR
    assert s["payment_cents"] == 8885  # standard annuity payment
    assert s["rows"][0]["interest_cents"] == 1000
    assert s["rows"][-1]["balance_cents"] == 0
    assert s["total_paid_cents"] == 100000 + s["total_interest_cents"]


def test_loan_schedule_endpoint_and_errors(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        loan = await P.create_asset(AssetIn(
            name="Car loan", kind="loan", value_cents=240000,
            principal_cents=240000, apr_bps=600, term_months=24,
            start_date="2026-10-01"), OWNER)
        d = await P.get_loan_schedule(loan["id"], OWNER)
        assert d["months"] == 24 and d["rows"][-1]["balance_cents"] == 0
        assert d["payoff_date"] == "2028-10-01"
        house = await P.create_asset(AssetIn(name="House", kind="property",
                                             value_cents=100), OWNER)
        with pytest.raises(HTTPException) as exc:
            await P.get_loan_schedule(house["id"], OWNER)
        assert exc.value.status_code == 400
        bare = await P.create_asset(AssetIn(name="IOU", kind="loan", value_cents=100), OWNER)
        with pytest.raises(HTTPException) as exc:
            await P.get_loan_schedule(bare["id"], OWNER)
        assert exc.value.status_code == 400  # no term_months
        with pytest.raises(HTTPException) as exc:
            await P.get_loan_schedule("missing", OWNER)
        assert exc.value.status_code == 404
    _run(run())


# ---------------------------------------------------------------- multi-currency

def test_net_worth_currency_conversion_and_pending_flag(monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        fake = _fake_db(monkeypatch, personal_assets=FakeCollection([
            {"id": "e1", "tenant_id": HOUSE, "name": "Berlin flat deposit",
             "kind": "other_asset", "value_cents": 100000, "currency": "EUR",
             "fx_rate_micro": 1100000, "as_of_date": "2026-11-01"},  # 1 EUR = 1.10 USD
            {"id": "e2", "tenant_id": HOUSE, "name": "Peso cash",
             "kind": "other_asset", "value_cents": 50000, "currency": "MXN",
             "as_of_date": "2026-11-01"},  # no rate: face value + flagged
        ]))
        monkeypatch.setattr(P.L, "_balances", AsyncMock(return_value={}))
        d = await P.get_net_worth(OWNER)
        assert d["base_currency"] == "USD"
        assert d["manual_assets_cents"] == 110000 + 50000
        assert d["fx_pending_count"] == 1
        items = {i["name"]: i for i in d["manual_items"]}
        assert items["Berlin flat deposit"]["converted_cents"] == 110000
        assert items["Berlin flat deposit"]["fx_applied"] is True
        assert items["Peso cash"]["fx_applied"] is False
        # switch base currency to EUR: the EUR item becomes native
        await P.update_household_settings(
            HouseholdSettingsIn(base_currency="EUR"), OWNER)
        d2 = await P.get_net_worth(OWNER)
        assert d2["base_currency"] == "EUR"
        items2 = {i["name"]: i for i in d2["manual_items"]}
        assert items2["Berlin flat deposit"]["fx_applied"] is True
        assert items2["Berlin flat deposit"]["converted_cents"] == 100000
    _run(run())


def test_fx_refresh_stamps_rate_from_house_fx(monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        import lib.fx as FX
        _fake_db(monkeypatch)
        asset = await P.create_asset(AssetIn(
            name="EUR savings", kind="other_asset", value_cents=1000,
            currency="EUR"), OWNER)
        monkeypatch.setattr(FX, "get_rate", AsyncMock(return_value=1.08))
        d = await P.refresh_asset_fx(asset["id"], OWNER)
        assert d["fx_applied"] is True and d["fx_rate_micro"] == 1080000
        usd = await P.create_asset(AssetIn(name="USD cash", kind="other_asset",
                                           value_cents=100), OWNER)
        d2 = await P.refresh_asset_fx(usd["id"], OWNER)
        assert d2["fx_applied"] is False  # already base currency
    _run(run())


def test_opening_meta_currency_roundtrip(monkeypatch):
    async def run():
        _fake_db(monkeypatch, bank_accounts=FakeCollection([
            {"id": "ba1", "tenant_id": HOUSE, "name": "Checking", "kind": "bank",
             "gl_code": "1020"}]))
        m = await P.set_account_opening("ba1", OpeningIn(
            opening_balance_cents=100, opening_date="2026-11-01",
            currency="eur"), OWNER)
        assert m["currency"] == "EUR"
        with pytest.raises(HTTPException) as exc:
            await P.set_account_opening("ba1", OpeningIn(
                opening_balance_cents=100, opening_date="2026-11-01",
                currency="EURO"), OWNER)
        assert exc.value.status_code == 400
    _run(run())


# ---------------------------------------------------------------- insights + summary

def test_insights_and_summary(monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        today = date.today()
        period = f"{today.year:04d}-{today.month:02d}"
        prev = P._shift_period(period, -1)
        txns = [
            _txn(id="i1", txn_date=f"{period}-03", amount_cents=200000,
                 description="PAYROLL"),
            _txn(id="o1", txn_date=f"{period}-04", amount_cents=-90000,
                 description="RENT PAYMENT", category_name="Housing"),
            _txn(id="o2", txn_date=f"{period}-05", amount_cents=-20000,
                 description="GROCERY STORE", category_name="Groceries"),
            _txn(id="p1", txn_date=f"{prev}-04", amount_cents=-50000,
                 description="RENT PAYMENT", category_name="Housing"),
        ]
        _fake_db(monkeypatch,
                 bank_transactions=FakeCollection(txns),
                 bank_accounts=FakeCollection([
                     {"id": "ba1", "tenant_id": HOUSE, "name": "Checking",
                      "kind": "bank", "gl_code": "1020"}]))
        monkeypatch.setattr(P.L, "_balances", AsyncMock(return_value={
            "1020": {"debit": 300000, "credit": 0}}))
        await P.upsert_personal_budget(P.PersonalBudgetIn(
            category="Housing", period=period, amount_cents=100000,
            rollover=False), OWNER)
        await P.create_goal(GoalIn(name="Trip", target_cents=100000,
                                   saved_cents=40000), OWNER)
        ins = await P.get_insights(period, OWNER)
        kinds = {c["kind"] for c in ins["cards"]}
        assert "spending_vs_last_month" in kinds  # 110000 out vs 50000 prev
        assert "savings_rate" in kinds
        assert "goal_progress" in kinds
        assert "largest_expense" in kinds
        assert ins["top_categories"][0]["category"] == "Housing"
        assert ins["cash_flow"]["in_cents"] == 200000

        s = await P.get_summary(OWNER)
        assert s["period"] == period
        assert s["net_worth"]["net_worth_cents"] == 300000
        assert s["accounts"][0]["name"] == "Checking"
        assert s["budgets"]["totals"]["budgeted_cents"] == 100000
        assert s["goals"][0]["progress_pct"] == 40.0
        assert isinstance(s["insights"], list)
    _run(run())
