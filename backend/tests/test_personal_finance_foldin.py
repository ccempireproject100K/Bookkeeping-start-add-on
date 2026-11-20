"""Personal finance layer (slice 3 — budget-app fold-in): envelope
budgeting (health states, per-envelope cycles, rollover carry, payday
suggestions), the family safe-to-spend view, timed savings vaults,
the savings-methods library, the bills+envelopes forecast, and the
FIFO age-of-money counter. The household gate must hold for every
new endpoint, and business data must never leak in.

No live MongoDB or network needed (db collections are faked).

Run from the backend directory with:
    python -m pytest tests/test_personal_finance_foldin.py
"""
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from routers import personal as P
from routers.personal import (EnvelopeIn, GoalIn, HouseholdSettingsIn,
                              SavingsActionIn, SavingsEnrollIn, VaultIn,
                              VaultAmountIn)
from test_personal_finance import (BIZ, BIZ_OWNER, HOUSE, OTHER, OWNER,
                                   FakeCollection, _run, _txn)

OTHER_OWNER = {"id": "u2", "email": "other@home.test", "tenant_id": OTHER,
               "role": "owner"}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _fake_db(monkeypatch, **collections):
    base = dict(
        tenants=FakeCollection([
            {"id": HOUSE, "name": "Home", "kind": "household", "settings": {}},
            {"id": OTHER, "name": "Other home", "kind": "household",
             "settings": {}},
            {"id": BIZ, "name": "Shop"}]),  # no kind = business
        users=FakeCollection([{"id": "u1", "email": "owner@home.test",
                               "tenant_id": BIZ, "role": "owner",
                               "memberships": [{"tenant_id": BIZ,
                                                "role": "owner"}]}]),
        accounts=FakeCollection(),
        bank_accounts=FakeCollection(),
        bank_transactions=FakeCollection(),
        personal_budgets=FakeCollection(),
        personal_recurring_bills=FakeCollection(),
        personal_goals=FakeCollection(),
        personal_assets=FakeCollection(),
        personal_account_meta=FakeCollection(),
        personal_goal_funding_events=FakeCollection(),
        personal_envelopes=FakeCollection(),
        personal_vaults=FakeCollection(),
        personal_savings_methods=FakeCollection(),
        personal_savings_events=FakeCollection(),
    )
    base.update(collections)
    fake = SimpleNamespace(**base)
    monkeypatch.setattr(P, "db", fake)
    return fake


def _env_doc(**kw):
    d = {"id": "env1", "tenant_id": HOUSE, "name": "Groceries",
         "category": "Groceries", "allocated_cents": 50000,
         "cycle_start_day": 1, "rollover": True, "members": [],
         "visibility": "shared", "active": True,
         "created_at": _today().isoformat() + "T00:00:00+00:00",
         "updated_at": _today().isoformat() + "T00:00:00+00:00"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- gate

def test_foldin_endpoints_all_403_for_business(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        calls = [
            P.create_envelope(EnvelopeIn(name="G", category="Groceries",
                                         allocated_cents=1000), BIZ_OWNER),
            P.list_envelopes(BIZ_OWNER),
            P.envelope_suggestions(100000, BIZ_OWNER),
            P.get_safe_to_spend(BIZ_OWNER),
            P.create_vault(VaultIn(name="V"), BIZ_OWNER),
            P.list_vaults(BIZ_OWNER),
            P.list_savings_methods(BIZ_OWNER),
            P.enroll_savings_method("day_of_month", SavingsEnrollIn(),
                                    BIZ_OWNER),
            P.get_forecast(6, BIZ_OWNER),
            P.get_age_of_money(BIZ_OWNER),
        ]
        for call in calls:
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 403
    _run(run())


# ---------------------------------------------------------------- envelopes

def test_envelope_health_states(monkeypatch):
    async def run():
        today = _today().isoformat()
        txns = [_txn(id="s1", txn_date=today, description="STORE",
                     amount_cents=-30000, category_name="Groceries")]
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))
        env = await P.create_envelope(EnvelopeIn(
            name="Groceries", category="Groceries", allocated_cents=60000),
            OWNER)
        assert env["state"] == "green" and env["remaining_cents"] == 30000
        assert env["remaining_pct"] == 50.0
        # spend down to exactly 20% remaining -> yellow
        fake_txns = P.db.bank_transactions
        fake_txns.docs.append(_txn(id="s2", txn_date=today, description="STORE",
                                   amount_cents=-18000,
                                   category_name="Groceries"))
        env = (await P.list_envelopes(OWNER))[0]
        assert env["state"] == "yellow" and env["remaining_cents"] == 12000
        # spend past the allocation -> red
        fake_txns.docs.append(_txn(id="s3", txn_date=today, description="STORE",
                                   amount_cents=-15000,
                                   category_name="Groceries"))
        env = (await P.list_envelopes(OWNER))[0]
        assert env["state"] == "red" and env["remaining_cents"] == -3000
    _run(run())


def test_envelope_rollover_carries_unspent_and_overspend(monkeypatch):
    async def run():
        today = _today()
        first_this_month = today.replace(day=1)
        last_month = first_this_month - timedelta(days=1)
        created = last_month.replace(day=1).isoformat() + "T00:00:00+00:00"
        txns = [
            _txn(id="lm", txn_date=last_month.isoformat(), description="STORE",
                 amount_cents=-20000, category_name="Groceries"),
            _txn(id="tm", txn_date=today.isoformat(), description="STORE",
                 amount_cents=-5000, category_name="Groceries"),
        ]
        envs = FakeCollection([
            _env_doc(id="roll", created_at=created, rollover=True),
            _env_doc(id="flat", name="Flat", created_at=created,
                     rollover=False),
        ])
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns),
                 personal_envelopes=envs)
        views = {v["id"]: v for v in await P.list_envelopes(OWNER)}
        # 50000 allocated, 20000 spent last cycle -> 30000 carries in;
        # effective 80000, 5000 spent this cycle -> 75000 remaining.
        assert views["roll"]["carried_in_cents"] == 30000
        assert views["roll"]["effective_allocated_cents"] == 80000
        assert views["roll"]["remaining_cents"] == 75000
        # rollover off: last cycle is forgotten
        assert views["flat"]["carried_in_cents"] == 0
        assert views["flat"]["effective_allocated_cents"] == 50000
        assert views["flat"]["remaining_cents"] == 45000
    _run(run())


def test_envelope_own_cycle_start_day(monkeypatch):
    async def run():
        today = _today()
        cs, _ce = P._cycle_window(today, 15)
        created = cs.isoformat() + "T00:00:00+00:00"
        txns = [
            _txn(id="in", txn_date=cs.isoformat(), description="STORE",
                 amount_cents=-7000, category_name="Groceries"),
            _txn(id="out", txn_date=(cs - timedelta(days=1)).isoformat(),
                 description="STORE", amount_cents=-99000,
                 category_name="Groceries"),
        ]
        envs = FakeCollection([_env_doc(cycle_start_day=15, rollover=False,
                                        created_at=created)])
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns),
                 personal_envelopes=envs)
        view = (await P.list_envelopes(OWNER))[0]
        assert view["cycle_start"] == cs.isoformat()
        assert view["spent_cents"] == 7000  # yesterday's spend is last cycle's
    _run(run())


def test_envelope_tenant_isolation(monkeypatch):
    async def run():
        _fake_db(monkeypatch,
                 personal_envelopes=FakeCollection([_env_doc()]))
        assert len(await P.list_envelopes(OWNER)) == 1
        assert await P.list_envelopes(OTHER_OWNER) == []
    _run(run())


def test_envelope_suggestions_from_history(monkeypatch):
    async def run():
        today = _today()
        txns = []
        for back in (0, 1, 2):
            y = today.year + (today.month - 1 - back) // 12
            m = (today.month - 1 - back) % 12 + 1
            txns.append(_txn(id=f"g{back}", txn_date=f"{y:04d}-{m:02d}-10",
                             description="STORE", amount_cents=-25000,
                             category_name="Groceries"))
            txns.append(_txn(id=f"d{back}", txn_date=f"{y:04d}-{m:02d}-11",
                             description="CAFE", amount_cents=-25000,
                             category_name="Dining"))
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))
        out = await P.envelope_suggestions(400000, OWNER)
        assert out["suggestion_only"] is True
        rows = {r["category"]: r for r in out["rows"]}
        assert rows["Groceries"]["suggested_cents"] == 200000
        assert rows["Dining"]["suggested_cents"] == 200000
        assert rows["Groceries"]["share_pct"] == 50.0
    _run(run())


# ---------------------------------------------------------------- safe-to-spend

def _sts_fixture(monkeypatch, settings=None, extra_txns=()):
    """Balance 500000 in one account; payday = today+1 (weekly rhythm,
    last payday 6 days ago)."""
    today = _today()
    tenants = FakeCollection([
        {"id": HOUSE, "name": "Home", "kind": "household",
         "settings": settings if settings is not None else {
             "last_payday_date": (today - timedelta(days=6)).isoformat(),
             "payday_cadence": "weekly"}},
        {"id": BIZ, "name": "Shop"}])
    metas = FakeCollection([{"id": "m1", "tenant_id": HOUSE,
                             "bank_account_id": "ba1",
                             "opening_balance_cents": 500000,
                             "opening_date": (today - timedelta(days=30)).isoformat()}])
    accounts = FakeCollection([{"id": "ba1", "tenant_id": HOUSE,
                                "name": "Checking", "kind": "bank"}])
    bills = FakeCollection([
        {"id": "rent", "tenant_id": HOUSE, "name": "Rent",
         "category": "Housing", "amount_cents": 100000, "cadence": "monthly",
         "next_due_date": (today + timedelta(days=1)).isoformat(),
         "active": True, "source": "manual"},
        {"id": "gym", "tenant_id": HOUSE, "name": "Gym",
         "category": "Health", "amount_cents": 50000, "cadence": "monthly",
         "next_due_date": (today + timedelta(days=5)).isoformat(),
         "active": True, "source": "manual"},
        {"id": "clean", "tenant_id": HOUSE, "name": "Cleaning",
         "category": "Home", "amount_cents": 2000, "cadence": "weekly",
         "next_due_date": today.isoformat(),
         "active": True, "source": "manual"},
    ])
    # envelope spend rides an unregistered account id so the measured
    # balance stays 500000 (category spend reads the whole stream,
    # exactly like the slice-1 budgets do).
    txns = FakeCollection([
        _txn(id="sp", bank_account_id="ba-other", txn_date=today.isoformat(),
             description="STORE", amount_cents=-25000,
             category_name="Groceries"),
        *extra_txns])
    envs = FakeCollection([_env_doc(allocated_cents=100000)])
    vaults = FakeCollection([{"id": "v1", "tenant_id": HOUSE, "name": "Vault",
                              "balance_cents": 30000,
                              "withdrawal_delay_hours": 48,
                              "pending_withdrawal": None}])
    goals = FakeCollection([
        {"id": "g1", "tenant_id": HOUSE, "name": "Locked goal",
         "target_cents": 100000, "saved_cents": 20000, "locked": True},
        {"id": "g2", "tenant_id": HOUSE, "name": "Open goal",
         "target_cents": 100000, "saved_cents": 99900, "locked": False},
    ])
    _fake_db(monkeypatch, tenants=tenants, personal_account_meta=metas,
             bank_accounts=accounts, personal_recurring_bills=bills,
             bank_transactions=txns, personal_envelopes=envs,
             personal_vaults=vaults, personal_goals=goals)
    return today


def test_safe_to_spend_full_math(monkeypatch):
    async def run():
        today = _sts_fixture(monkeypatch)
        out = await P.get_safe_to_spend(OWNER)
        assert out["payday_estimated"] is False
        assert out["next_payday_date"] == (today + timedelta(days=1)).isoformat()
        parts = out["parts"]
        assert parts["account_balance_cents"] == 500000
        # Rent (due on payday, counted) 100000 + Cleaning (due today)
        # 2000; Gym is due after payday and stays out.
        assert parts["upcoming_bills_before_payday_cents"] == 102000
        assert parts["envelope_obligations_cents"] == 75000
        assert parts["vault_balances_cents"] == 30000
        assert parts["locked_goals_cents"] == 20000
        assert parts["vault_locks_cents"] == 50000
        assert out["safe_to_spend_cents"] == 273000
        assert out["raw_cents"] == 273000
        names = {c["name"] for c in out["bills_considered"]}
        assert names == {"Rent", "Cleaning"}
        assert out["largest_envelope"]["name"] == "Groceries"
    _run(run())


def test_safe_to_spend_estimated_payday_horizon(monkeypatch):
    async def run():
        today = _sts_fixture(monkeypatch, settings={})
        out = await P.get_safe_to_spend(OWNER)
        assert out["payday_estimated"] is True
        assert out["next_payday_date"] == (today + timedelta(days=14)).isoformat()
        # With the 14-day horizon the Gym bill (due in 5 days) counts
        # too, and weekly Cleaning occurs three times (today, +7, +14):
        # 100000 + 50000 + 3*2000 = 156000 of bills before "payday".
        assert out["parts"]["upcoming_bills_before_payday_cents"] == 156000
        assert out["safe_to_spend_cents"] == 219000
    _run(run())


def test_safe_to_spend_floors_at_zero(monkeypatch):
    async def run():
        today = _today()
        bills = FakeCollection([
            {"id": "big", "tenant_id": HOUSE, "name": "Big bill",
             "category": "Housing", "amount_cents": 10000,
             "cadence": "monthly",
             "next_due_date": (today + timedelta(days=1)).isoformat(),
             "active": True, "source": "manual"}])
        _fake_db(monkeypatch, personal_recurring_bills=bills)
        out = await P.get_safe_to_spend(OWNER)
        # No accounts at all: balance 0 against a 10000 bill — the raw
        # figure stays honest (-10000) while the family number floors.
        assert out["raw_cents"] == -10000
        assert out["safe_to_spend_cents"] == 0
    _run(run())


def test_payday_settings_roundtrip(monkeypatch):
    async def run():
        today = _today()
        _fake_db(monkeypatch)
        out = await P.update_household_settings(HouseholdSettingsIn(
            base_currency="USD",
            last_payday_date=(today - timedelta(days=3)).isoformat(),
            payday_cadence="weekly"), OWNER)
        assert out["payday_cadence"] == "weekly"
        sts = await P.get_safe_to_spend(OWNER)
        assert sts["payday_estimated"] is False
        assert sts["next_payday_date"] == (today + timedelta(days=4)).isoformat()
    _run(run())


def test_goal_locked_flag_stored(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        goal = await P.create_goal(GoalIn(name="House deposit",
                                          target_cents=500000, locked=True),
                                   OWNER)
        assert goal["locked"] is True
        rows = await P.list_goals(OWNER)
        assert rows[0]["locked"] is True
    _run(run())


# ---------------------------------------------------------------- vaults

def test_vault_timer_flow(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        v = await P.create_vault(VaultIn(name="Rainy day", balance_cents=10000,
                                         withdrawal_delay_hours=0), OWNER)
        assert v["balance_cents"] == 10000
        v = await P.deposit_vault(v["id"], VaultAmountIn(amount_cents=5000),
                                  OWNER)
        assert v["balance_cents"] == 15000
        v = await P.request_vault_withdrawal(
            v["id"], VaultAmountIn(amount_cents=4000), OWNER)
        assert v["pending_withdrawal"]["ready"] is True  # zero delay
        v = await P.withdraw_vault(v["id"], OWNER)
        assert v["balance_cents"] == 11000
        assert v["pending_withdrawal"] is None
    _run(run())


def test_vault_timer_blocks_early_withdrawal(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        v = await P.create_vault(VaultIn(name="Slow vault",
                                         balance_cents=10000,
                                         withdrawal_delay_hours=48), OWNER)
        with pytest.raises(HTTPException) as exc:
            await P.request_vault_withdrawal(
                v["id"], VaultAmountIn(amount_cents=20000), OWNER)
        assert exc.value.status_code == 400  # exceeds balance
        v = await P.request_vault_withdrawal(
            v["id"], VaultAmountIn(amount_cents=4000), OWNER)
        assert v["pending_withdrawal"]["ready"] is False
        with pytest.raises(HTTPException) as exc:
            await P.request_vault_withdrawal(
                v["id"], VaultAmountIn(amount_cents=1000), OWNER)
        assert exc.value.status_code == 400  # already counting down
        with pytest.raises(HTTPException) as exc:
            await P.withdraw_vault(v["id"], OWNER)
        assert exc.value.status_code == 400  # timer has not elapsed
    _run(run())


# ---------------------------------------------------------------- savings methods

def test_savings_day_of_month(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            await P.savings_method_action(
                "day_of_month", SavingsActionIn(action="save_day"), OWNER)
        assert exc.value.status_code == 400  # not enrolled yet
        with pytest.raises(HTTPException) as exc:
            await P.enroll_savings_method("nope", SavingsEnrollIn(), OWNER)
        assert exc.value.status_code == 404
        view = await P.enroll_savings_method("day_of_month",
                                             SavingsEnrollIn(), OWNER)
        assert view["enrolled"] is True
        assert view["yearly_pace"]["amount_cents"] == 595200
        assert view["yearly_pace"]["label"] == "EXACT"
        out = await P.savings_method_action(
            "day_of_month", SavingsActionIn(action="save_day"), OWNER)
        today = _today()
        assert out["action_result"]["amount_cents"] == today.day * 100
        assert out["saved_total_cents"] == today.day * 100
        assert out["next_action"] is None  # today's dollar is in
        assert out["pace"] == ("on_pace" if today.day == 1 else "behind")
    _run(run())


def test_savings_fifty_two_week_forward_and_reverse(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        await P.enroll_savings_method("fifty_two_week", SavingsEnrollIn(),
                                      OWNER)
        out = await P.savings_method_action(
            "fifty_two_week", SavingsActionIn(action="save_week"), OWNER)
        assert out["action_result"]["amount_cents"] == 100
        out = await P.savings_method_action(
            "fifty_two_week", SavingsActionIn(action="save_week"), OWNER)
        assert out["action_result"]["amount_cents"] == 200
        assert out["saved_total_cents"] == 300
        assert out["config"]["week_index"] == 2
    _run(run())


def test_savings_fifty_two_week_reverse_starts_high(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        view = await P.enroll_savings_method(
            "fifty_two_week", SavingsEnrollIn(variant="reverse"), OWNER)
        assert view["variant"] == "reverse"
        out = await P.savings_method_action(
            "fifty_two_week", SavingsActionIn(action="save_week"), OWNER)
        assert out["action_result"]["amount_cents"] == 5200
    _run(run())


def test_savings_hundred_envelope(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        await P.enroll_savings_method("hundred_envelope", SavingsEnrollIn(),
                                      OWNER)
        out = await P.savings_method_action(
            "hundred_envelope", SavingsActionIn(action="draw_envelope"),
            OWNER)
        assert out["action_result"]["amount_cents"] == 100  # envelope #1
        out = await P.savings_method_action(
            "hundred_envelope",
            SavingsActionIn(action="draw_envelope", number=100), OWNER)
        assert out["action_result"]["amount_cents"] == 10000
        with pytest.raises(HTTPException) as exc:
            await P.savings_method_action(
                "hundred_envelope",
                SavingsActionIn(action="draw_envelope", number=100), OWNER)
        assert exc.value.status_code == 400  # already drawn
    _run(run())


def test_savings_round_ups_sweep_is_idempotent(monkeypatch):
    async def run():
        today = _today().isoformat()
        txns = [
            _txn(id="r1", txn_date=today, description="CAFE",
                 amount_cents=-1050),
            _txn(id="r2", txn_date=today, description="STORE",
                 amount_cents=-2000),  # whole dollars sweep 0
            _txn(id="r3", txn_date=today, description="SHOP",
                 amount_cents=-99),
        ]
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))
        await P.enroll_savings_method("round_ups", SavingsEnrollIn(), OWNER)
        out = await P.savings_method_action(
            "round_ups", SavingsActionIn(action="sweep_roundups"), OWNER)
        assert out["action_result"]["amount_cents"] == 51  # 50 + 0 + 1
        out = await P.savings_method_action(
            "round_ups", SavingsActionIn(action="sweep_roundups"), OWNER)
        assert out["action_result"]["amount_cents"] == 0
        assert out["saved_total_cents"] == 51
    _run(run())


def test_savings_five_dollar_and_no_spend_days(monkeypatch):
    async def run():
        today = _today()
        txns = [
            _txn(id="n1", txn_date=(today - timedelta(days=2)).isoformat(),
                 description="CAFE", amount_cents=-1000,
                 category_name="Dining"),
            _txn(id="n2", txn_date=(today - timedelta(days=1)).isoformat(),
                 description="CAFE", amount_cents=-3000,
                 category_name="Dining"),
        ]
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))
        await P.enroll_savings_method("five_dollar_rule", SavingsEnrollIn(),
                                      OWNER)
        out = await P.savings_method_action(
            "five_dollar_rule", SavingsActionIn(action="log_five"), OWNER)
        assert out["action_result"]["amount_cents"] == 500
        view = await P.enroll_savings_method("no_spend_days",
                                             SavingsEnrollIn(), OWNER)
        # average daily discretionary = (1000 + 3000) / 2 days
        assert view["config"]["avg_daily_discretionary_cents"] == 2000
        out = await P.savings_method_action(
            "no_spend_days", SavingsActionIn(action="keep_day"), OWNER)
        assert out["action_result"]["amount_cents"] == 2000
    _run(run())


def test_savings_payday_escalator_rise_flow(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        await P.enroll_savings_method("payday_escalator", SavingsEnrollIn(
            config={"payday_amount_cents": 100000, "percent": 5,
                    "rise_every_paydays": 1}), OWNER)
        out = await P.savings_method_action(
            "payday_escalator", SavingsActionIn(action="record_payday"),
            OWNER)
        assert out["action_result"]["amount_cents"] == 5000
        assert out["config"]["pending_rise"] is True
        assert out["next_action"]["action"] == "approve_rise"
        out = await P.savings_method_action(
            "payday_escalator", SavingsActionIn(action="approve_rise"),
            OWNER)
        assert out["config"]["percent"] == 6
        out = await P.savings_method_action(
            "payday_escalator", SavingsActionIn(action="record_payday"),
            OWNER)
        assert out["action_result"]["amount_cents"] == 6000
    _run(run())


def test_savings_unenroll_keeps_history(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        await P.enroll_savings_method("five_dollar_rule", SavingsEnrollIn(),
                                      OWNER)
        await P.savings_method_action(
            "five_dollar_rule", SavingsActionIn(action="log_five"), OWNER)
        view = await P.unenroll_savings_method("five_dollar_rule", OWNER)
        assert view["enrolled"] is False
        assert view["saved_total_cents"] == 500  # history survives
        with pytest.raises(HTTPException) as exc:
            await P.savings_method_action(
                "five_dollar_rule", SavingsActionIn(action="log_five"), OWNER)
        assert exc.value.status_code == 400  # inactive enrollment
    _run(run())


def test_savings_library_lists_all_methods(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        views = await P.list_savings_methods(OWNER)
        keys = [v["key"] for v in views]
        assert keys == ["day_of_month", "fifty_two_week", "hundred_envelope",
                        "round_ups", "five_dollar_rule", "no_spend_days",
                        "payday_escalator"]
        assert all(v["enrolled"] is False for v in views)
    _run(run())


# ---------------------------------------------------------------- forecast

def test_forecast_bills_and_envelope_projection(monkeypatch):
    async def run():
        today = _today()
        cur = today.isoformat()[:7]
        nxt = P._shift_period(cur, 1)
        txns = []
        for back in (1, 2, 3):
            period = P._shift_period(cur, -back)
            txns.append(_txn(id=f"f{back}", txn_date=f"{period}-10",
                             description="STORE", amount_cents=-30000,
                             category_name="Groceries"))
        bills = FakeCollection([
            {"id": "rent", "tenant_id": HOUSE, "name": "Rent",
             "category": "Housing", "amount_cents": 120000,
             "cadence": "monthly", "next_due_date": f"{nxt}-01",
             "active": True, "source": "manual"}])
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns),
                 personal_recurring_bills=bills,
                 personal_envelopes=FakeCollection([_env_doc(
                     allocated_cents=40000)]))
        out = await P.get_forecast(3, OWNER)
        assert out["label"] == "PROJECTION"
        months = {m["month"]: m for m in out["months"]}
        assert months[nxt]["bills_due_cents"] == 120000
        proj = months[nxt]["envelopes"][0]
        assert proj["projected_spent_cents"] == 30000  # 3-month average
        assert proj["projected_remaining_cents"] == 10000
        assert proj["state"] == "green"
    _run(run())


# ---------------------------------------------------------------- age of money

def test_age_of_money_fifo(monkeypatch):
    async def run():
        today = _today()
        txns = [
            _txn(id="old-transfer", txn_date=(today - timedelta(days=50)).isoformat(),
                 description="MOVE", amount_cents=999999, is_transfer=True),
            _txn(id="inc1", txn_date=(today - timedelta(days=40)).isoformat(),
                 description="PAYROLL", amount_cents=100000),
            _txn(id="inc2", txn_date=(today - timedelta(days=10)).isoformat(),
                 description="PAYROLL", amount_cents=50000),
            _txn(id="sp1", txn_date=(today - timedelta(days=5)).isoformat(),
                 description="STORE", amount_cents=-30000),
        ]
        _fake_db(monkeypatch, bank_transactions=FakeCollection(txns))
        out = await P.get_age_of_money(OWNER)
        # 30000 of the 40-day-old paycheck is spent; the oldest unspent
        # dollar is still from 40 days ago. The transfer never counts.
        assert out["age_days"] == 40
        assert out["oldest_unspent_income_date"] == (
            today - timedelta(days=40)).isoformat()
        assert out["unspent_income_cents"] == 120000
    _run(run())


def test_age_of_money_empty(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        out = await P.get_age_of_money(OWNER)
        assert out["age_days"] == 0
        assert out["unspent_income_cents"] == 0
        assert out["oldest_unspent_income_date"] is None
    _run(run())
