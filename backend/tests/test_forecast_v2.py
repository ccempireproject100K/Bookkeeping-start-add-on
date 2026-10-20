"""Forecast v2 (correctness release): two-layer known-curve model.

Covers: the double-count regression (a materialized recurring invoice is
counted exactly once; the run-rate band excludes journal sources already
in the known curve), recurring bills read from the real bills collection
(the phantom db.recurring read is gone), payroll + tax layers, manual
events, scenarios, expected pay dates, the 13-week roll-up, and low-cash
alerting. No live MongoDB or network needed (db collections are faked).

Run from the backend directory with:
    python -m pytest tests/test_forecast_v2.py
"""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from routers import budget as B
from routers import taxforecast as TF

TID = "tenant-1"
OWNER = {"id": "u1", "email": "owner@shop.test", "tenant_id": TID, "role": "owner"}
TODAY = "2026-10-20"  # a Tuesday; forecast window 90d ends 2027-01-18


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

    def _match(self, doc, filt):
        for key, cond in (filt or {}).items():
            val = doc.get(key)
            if isinstance(cond, dict):
                for op, operand in cond.items():
                    if op == "$gte" and not (val is not None and val >= operand):
                        return False
                    if op == "$gt" and not (val is not None and val > operand):
                        return False
                    if op == "$lt" and not (val is not None and val < operand):
                        return False
                    if op == "$lte" and not (val is not None and val <= operand):
                        return False
                    if op == "$in" and val not in operand:
                        return False
                    if op == "$nin" and val in operand:
                        return False
                    if op == "$ne" and val == operand:
                        return False
            elif val != cond:
                return False
        return True

    def find(self, filt=None, projection=None):
        return FakeCursor([d for d in self.docs if self._match(d, filt)])

    async def find_one(self, filt=None, projection=None):
        for d in self.docs:
            if self._match(d, filt):
                return d
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, filt, update):
        n = 0
        for d in self.docs:
            if self._match(d, filt):
                d.update(update.get("$set", {}))
                n += 1
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def delete_one(self, filt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, filt)]
        return SimpleNamespace(deleted_count=before - len(self.docs))


COLLECTIONS = ["monthly_budgets", "expenses", "invoices", "bills", "recurring",
               "recurring_invoices", "pay_schedules", "payroll_runs",
               "tax_forecast_config", "tax_payments", "tax_jurisdictions",
               "journal_entries", "journal_lines", "forecast_events",
               "forecast_scenarios", "security_alerts"]


def _fake_db(monkeypatch, start_cash=1_000_000, ytd_net=0, **colls):
    fake = SimpleNamespace(**{name: FakeCollection(colls.get(name, ()))
                              for name in COLLECTIONS})
    monkeypatch.setattr(B, "db", fake)
    monkeypatch.setattr(B, "today_str", lambda: TODAY)
    monkeypatch.setattr(B.L, "_balances",
                        AsyncMock(return_value={"1020": {"debit": start_cash, "credit": 0}}))
    monkeypatch.setattr(B.L, "pnl", AsyncMock(return_value={"net_income": ytd_net}))
    monkeypatch.setattr(B.L, "audit", AsyncMock())
    # default: alerts never touch the real motor client in tests; the
    # alert-specific tests override this with a recording fake
    monkeypatch.setattr(B.AL, "raise_alert", AsyncMock())
    return fake


import asyncio as _asyncio
_LOOP = _asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _inv(**kw):
    d = {"id": "inv1", "tenant_id": TID, "client_name": "Acme", "customer_id": None,
         "status": "issued", "total_cents": 10000, "paid_cents": 0,
         "due_date": "2026-10-25", "payments": [], "created_at": "2026-10-01T00:00:00"}
    d.update(kw)
    return d


def _bill(**kw):
    d = {"id": "bill1", "tenant_id": TID, "vendor": "Landlord", "status": "open",
         "amount_cents": 50000, "paid_cents": 0, "due_date": "2026-11-01",
         "bill_date": "2026-10-01", "recurring": False, "frequency": None,
         "expense_code": "5700"}
    d.update(kw)
    return d


# ---------------- the double-count regression (mandatory) ----------------

def test_materialized_recurring_invoice_counted_exactly_once(monkeypatch):
    """A template whose October occurrence already materialized into an open
    invoice: the money appears ONCE as AR (on the invoice), the template
    projects only from its advanced next_run, and with the run-rate band
    ON the historical invoice_paid cash is excluded from the band."""
    async def run():
        fake = _fake_db(
            monkeypatch,
            invoices=[_inv(id="inv-mat", total_cents=10000, due_date="2026-10-25")],
            recurring_invoices=[{
                "id": "tpl1", "tenant_id": TID, "name": "Acme monthly",
                "client_name": "Acme", "customer_id": None, "active": True,
                "frequency": "monthly", "next_run": "2026-11-01",
                "lines": [{"amount": 100.0}], "tax": 0}],
            journal_entries=[
                {"id": "e1", "tenant_id": TID, "status": "posted",
                 "entry_date": "2026-09-15", "source": "invoice_paid"},
                {"id": "e2", "tenant_id": TID, "status": "posted",
                 "entry_date": "2026-09-10", "source": "bank_txn"}],
            journal_lines=[
                {"id": "l1", "tenant_id": TID, "entry_id": "e1", "status": "posted",
                 "entry_date": "2026-09-15", "account_code": "1020",
                 "debit": 90000, "credit": 0},   # $900 past AR collection
                {"id": "l2", "tenant_id": TID, "entry_id": "e2", "status": "posted",
                 "entry_date": "2026-09-10", "account_code": "1020",
                 "debit": 9000, "credit": 0}],  # $90 unscheduled cash in
        )
        fc = await B.forecast(90, run_rate=True, user=OWNER)

        # band: only the unscheduled $90 / 90 days = 100c/day; the $900
        # invoice_paid collection is excluded (it belongs to the AR category)
        assert fc["layers"]["run_rate"]["daily_inflow_cents"] == 100
        assert fc["avg_daily_inflow_cents"] == 100

        pts = {p["date"]: p for p in fc["points"]}
        # materialized invoice: once, on its due date, as AR
        assert pts["2026-10-25"]["known_in_cents"] == 10000
        # template occurrences: next_run cadence only (Nov 1, Dec 1, Jan 1)
        for d in ("2026-11-01", "2026-12-01", "2027-01-01"):
            assert pts[d]["known_in_cents"] == 10000, d
        recin = [i for i in fc["items"] if i["kind"] == "recurring_invoice"]
        assert len(recin) == 3 and all(i["date"] >= "2026-11-01" for i in recin)
        # total known inflow = 4 x $100 = the money counted exactly once each
        assert sum(p["known_in_cents"] for p in fc["points"]) == 40000
        assert fc["scheduled"]["ar_cents"] == 10000
        assert fc["scheduled"]["recurring_inflow_cents"] == 30000
    _run(run())


def test_phantom_recurring_collection_is_ignored(monkeypatch):
    """db.recurring (the v0 expense-template collection) has NO writer in the
    codebase; seeding it must not move the forecast. Recurring outflows come
    from bills chains instead."""
    async def run():
        _fake_db(monkeypatch,
                 recurring=[{"id": "ghost", "tenant_id": TID, "active": True,
                             "day_of_month": 5, "amount": 500.0}])
        fc = await B.forecast(90, user=OWNER)
        assert fc["scheduled"]["recurring_outflow_cents"] == 0
        assert [i for i in fc["items"] if i["kind"] == "recurring_bill"] == []
    _run(run())


# ---------------- recurring bills (fixed source: db.bills chains) ----------------

def test_recurring_bills_project_from_chain_head(monkeypatch):
    async def run():
        _fake_db(monkeypatch, bills=[
            _bill(id="billA", vendor="RentCo", amount_cents=200000,
                  bill_date="2026-08-01", due_date="2026-08-31",
                  recurring=True, frequency="monthly", status="paid",
                  paid_cents=200000),
            _bill(id="billB", vendor="RentCo", amount_cents=200000,
                  bill_date="2026-08-31", due_date="2026-09-30",
                  recurring=True, frequency="monthly", status="paid",
                  paid_cents=200000),
        ])
        fc = await B.forecast(90, user=OWNER)
        rec = [i for i in fc["items"] if i["kind"] == "recurring_bill"]
        # cadence mirrors generate-next (+30d bill dates, +30d due offset):
        # bills Sep 30 / Oct 30 / Nov 29 -> dues Oct 30 / Nov 29 / Dec 29
        assert [i["date"] for i in rec] == ["2026-10-30", "2026-11-29", "2026-12-29"]
        assert all(i["amount_cents"] == 200000 for i in rec)
        assert fc["scheduled"]["recurring_outflow_cents"] == 600000
    _run(run())


def test_recurring_bill_open_head_counted_once_in_ap(monkeypatch):
    async def run():
        _fake_db(monkeypatch, bills=[
            _bill(id="billB", vendor="RentCo", amount_cents=200000,
                  bill_date="2026-08-31", due_date="2026-09-30",
                  recurring=True, frequency="monthly", status="open"),
        ])
        fc = await B.forecast(90, user=OWNER)
        ap = [i for i in fc["items"] if i["kind"] == "ap"]
        assert len(ap) == 1 and ap[0]["date"] == TODAY  # overdue -> today
        rec = [i for i in fc["items"] if i["kind"] == "recurring_bill"]
        assert rec and all(i["date"] > "2026-09-30" for i in rec)
        # the head's own amount is never repeated as a recurring item
        assert sum(1 for i in fc["items"]
                   if i["amount_cents"] == 200000 and i["date"] == TODAY) == 1
    _run(run())


# ---------------- payroll layer ----------------

def _schedule(**kw):
    d = {"id": "sch1", "tenant_id": TID, "name": "Main crew",
         "frequency": "biweekly", "next_run": "2026-10-23", "active": True}
    d.update(kw)
    return d


def _posted_run(**kw):
    d = {"id": "run1", "tenant_id": TID, "schedule_id": "sch1",
         "status": "posted", "period_start": "2026-09-25",
         "period_end": "2026-10-08",
         "totals": {"gross_cents": 500000, "booth_rent_cents": 50000,
                    "employer_tax_cents": 38250, "net_cents": 400000,
                    "tax_deductions_cents": 70000}}
    d.update(kw)
    return d


def test_payroll_layer_from_last_posted_run(monkeypatch):
    async def run():
        _fake_db(monkeypatch,
                 pay_schedules=[_schedule(),
                                _schedule(id="sch2", name="Weekend",
                                          frequency="weekly",
                                          next_run="2026-10-24")],
                 payroll_runs=[_posted_run()])
        fc = await B.forecast(90, user=OWNER)
        pay = [i for i in fc["items"] if i["kind"] == "payroll"]
        # cash cost = gross - booth rent (stays in shop) + employer taxes
        assert pay and all(i["amount_cents"] == 488250 for i in pay)
        assert [i["date"] for i in pay] == [
            "2026-10-23", "2026-11-06", "2026-11-20", "2026-12-04",
            "2026-12-18", "2027-01-01", "2027-01-15"]
        assert all("based on last run 2026-10-08" in i["label"] for i in pay)
        assert fc["layers"]["payroll"]["projected_cents"] == 488250 * 7
        # schedule with no posted run -> explicit gap note, no silent zero
        assert any("Weekend" in n and "no posted run" in n for n in fc["notes"])
    _run(run())


def test_payroll_gap_note_when_no_schedules(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        fc = await B.forecast(90, user=OWNER)
        assert any("no active pay schedule" in n for n in fc["notes"])
        assert fc["layers"]["payroll"]["projected_cents"] == 0
    _run(run())


# ---------------- tax layers ----------------

def test_income_tax_layer_spreads_remaining_over_remaining_dues(monkeypatch):
    async def run():
        ytd_net = 29_300_000  # annualizes to exactly $365,000 on Oct 20
        _fake_db(monkeypatch, ytd_net=ytd_net,
                 tax_payments=[{"id": "p1", "tenant_id": TID, "year": "2026",
                                "quarter": "Q1", "amount_cents": 100000}])
        fc = await B.forecast(90, user=OWNER)
        settings = {"brackets": TF.DEFAULT_BRACKETS,
                    "std_deduction": TF.DEFAULT_STD_DEDUCTION,
                    "ytd_other_income_cents": 0}
        expected_total = int(round(TF._estimate(36_500_000, settings)["total"] * 100))
        remaining = expected_total - 100000
        assert remaining > 0
        tax_items = [i for i in fc["items"] if i["kind"] == "tax"]
        # from Oct 20 the only remaining 2026 due date in window is Q4's
        assert len(tax_items) == 1
        assert tax_items[0]["date"] == "2027-01-15"
        assert tax_items[0]["amount_cents"] == remaining
        assert fc["scheduled"]["tax_cents"] == remaining
    _run(run())


def test_income_tax_fully_covered_is_a_note_not_a_zero_item(monkeypatch):
    async def run():
        _fake_db(monkeypatch, ytd_net=0,
                 tax_payments=[{"id": "p1", "tenant_id": TID, "year": "2026",
                                "quarter": "Q1", "amount_cents": 5000}])
        fc = await B.forecast(90, user=OWNER)
        assert [i for i in fc["items"] if i["kind"] == "tax"] == []
        assert any("fully covered" in n for n in fc["notes"])
    _run(run())


def test_sales_tax_layer_uses_configured_filing_schedule(monkeypatch):
    async def run():
        _fake_db(
            monkeypatch,
            tax_jurisdictions=[{"id": "j1", "tenant_id": TID,
                                "name": "Travis County, TX", "state": "TX",
                                "rate_pct": 8.25, "active": True,
                                "filing_frequency": "monthly", "filing_due_day": 20}],
            invoices=[
                _inv(id="s1", tax_jurisdiction_id="j1", tax_cents=5000,
                     created_at="2026-10-05T00:00:00"),
                _inv(id="s2", tax_jurisdiction_id="j1", tax_cents=3000,
                     created_at="2026-10-12T00:00:00"),
                _inv(id="s3", tax_jurisdiction_id="j1", tax_cents=9999,
                     created_at="2026-09-12T00:00:00"),  # prior period: excluded
            ])
        fc = await B.forecast(90, user=OWNER)
        st = [i for i in fc["items"] if i["kind"] == "sales_tax"]
        assert [i["date"] for i in st] == ["2026-10-20", "2026-11-20", "2026-12-20"]
        assert all(i["amount_cents"] == 8000 for i in st)
        assert fc["scheduled"]["sales_tax_cents"] == 24000
    _run(run())


def test_sales_tax_unconfigured_is_a_gap_note(monkeypatch):
    async def run():
        _fake_db(monkeypatch,
                 tax_jurisdictions=[{"id": "j2", "tenant_id": TID, "name": "Dallas, TX",
                                     "state": "TX", "rate_pct": 8.25, "active": True}])
        fc = await B.forecast(90, user=OWNER)
        assert [i for i in fc["items"] if i["kind"] == "sales_tax"] == []
        assert any("no jurisdiction has a filing schedule" in n for n in fc["notes"])
    _run(run())


# ---------------- manual events + simulator shapes ----------------

def test_manual_events_crud_and_projection(monkeypatch):
    async def run():
        fake = _fake_db(monkeypatch)
        ev = await B.create_forecast_event(B.ForecastEventIn(
            name="Truck payment", direction="out", amount=250.0,
            date="2026-11-05"), OWNER)
        assert ev["amount_cents"] == 25000
        rec = await B.create_forecast_event(B.ForecastEventIn(
            name="Retainer", direction="in", amount=400.0, date="2026-10-25",
            frequency="monthly"), OWNER)
        hire = await B.create_forecast_event(B.ForecastEventIn(
            name="New barber", direction="out", shape="hire",
            annual_salary=120000.0, burden_percent=25.0, date="2026-11-01"), OWNER)
        # hire shape: 120000/12 * 1.25 = $12,500/mo, forced monthly
        assert hire["amount_cents"] == 1250000 and hire["frequency"] == "monthly"

        fc = await B.forecast(90, user=OWNER)
        ev_items = [i for i in fc["items"] if i["kind"] == "event"]
        assert any(i["label"] == "Truck payment" and i["date"] == "2026-11-05"
                   and i["amount_cents"] == 25000 for i in ev_items)
        assert [i["date"] for i in ev_items if i["series_key"] == f"event:{rec['id']}"] == \
            ["2026-10-25", "2026-11-25", "2026-12-25"]
        assert fc["scheduled"]["events_out_cents"] >= 25000 + 1250000

        await B.delete_forecast_event(ev["id"], OWNER)
        fc2 = await B.forecast(90, user=OWNER)
        assert not any(i["label"] == "Truck payment" for i in fc2["items"])
        with pytest.raises(HTTPException) as exc:
            await B.delete_forecast_event("nope", OWNER)
        assert exc.value.status_code == 404
    _run(run())


def test_scenario_events_only_apply_to_their_scenario(monkeypatch):
    async def run():
        _fake_db(monkeypatch)
        sc = await B.create_scenario(B.ScenarioIn(name="Expansion"), OWNER)
        await B.create_forecast_event(B.ForecastEventIn(
            name="Second location deposit", direction="out", amount=5000.0,
            date="2026-12-01", scenario_id=sc["id"]), OWNER)
        base = await B.forecast(90, user=OWNER)
        assert not any(i["label"] == "Second location deposit" for i in base["items"])
        scen = await B.forecast(90, scenario_id=sc["id"], user=OWNER)
        assert any(i["label"] == "Second location deposit" for i in scen["items"])
        assert scen["scenario"]["name"] == "Expansion"
    _run(run())


# ---------------- scenarios: haircut / shift / exclusions / toggles ----------------

def test_scenario_haircut_shift_exclusion_and_layer_toggle(monkeypatch):
    async def run():
        _fake_db(monkeypatch,
                 invoices=[_inv(id="inv1", total_cents=10000, due_date="2026-10-25")],
                 bills=[_bill(id="billX", amount_cents=70000, due_date="2026-11-01")],
                 pay_schedules=[_schedule()], payroll_runs=[_posted_run()])
        sc = await B.create_scenario(B.ScenarioIn(
            name="Tight month", ar_haircut_pct=50.0, ar_shift_days=10,
            excluded_keys=["ap:billX"], payroll=False), OWNER)
        fc = await B.forecast(90, scenario_id=sc["id"], user=OWNER)
        ar = [i for i in fc["items"] if i["kind"] == "ar"]
        assert len(ar) == 1
        assert ar[0]["amount_cents"] == 5000          # 50% haircut
        assert ar[0]["date"] == "2026-11-04"          # due + 10d shift
        assert not any(i["kind"] == "ap" for i in fc["items"])       # excluded
        assert not any(i["kind"] == "payroll" for i in fc["items"])  # layer off

        base = await B.forecast(90, user=OWNER)  # base view unaffected
        assert any(i["kind"] == "ap" for i in base["items"])
        assert any(i["kind"] == "payroll" for i in base["items"])
        ar0 = [i for i in base["items"] if i["kind"] == "ar"]
        assert ar0[0]["amount_cents"] == 10000 and ar0[0]["date"] == "2026-10-25"

        await B.update_scenario(sc["id"], B.ScenarioIn(
            name="Tight month", ar_haircut_pct=0.0), OWNER)
        fc2 = await B.forecast(90, scenario_id=sc["id"], user=OWNER)
        assert [i for i in fc2["items"] if i["kind"] == "ar"][0]["amount_cents"] == 10000
        await B.delete_scenario(sc["id"], OWNER)
        with pytest.raises(HTTPException) as exc:
            await B.forecast(90, scenario_id=sc["id"], user=OWNER)
        assert exc.value.status_code == 404
    _run(run())


# ---------------- expected pay dates ----------------

def _paid_invoice(cid, name, due, pay_date, n):
    return _inv(id=f"paid{n}", customer_id=cid, client_name=name, status="paid",
                total_cents=10000, paid_cents=10000, due_date=due,
                payments=[{"date": pay_date, "amount_cents": 10000}])


def test_expected_pay_dates_from_customer_history(monkeypatch):
    async def run():
        _fake_db(monkeypatch, invoices=[
            _paid_invoice("c1", "SlowCo", "2026-08-01", "2026-08-07", 1),   # +6
            _paid_invoice("c1", "SlowCo", "2026-08-05", "2026-08-08", 2),   # +3
            _paid_invoice("c1", "SlowCo", "2026-08-10", "2026-08-19", 3),   # +9
            _paid_invoice("c2", "NewCo", "2026-08-01", "2026-08-20", 4),    # only 2
            _paid_invoice("c2", "NewCo", "2026-08-05", "2026-08-24", 5),
            _inv(id="open1", customer_id="c1", client_name="SlowCo",
                 due_date="2026-10-25"),
            _inv(id="open2", customer_id="c2", client_name="NewCo",
                 due_date="2026-10-25"),
        ])
        fc = await B.forecast(90, expected_pay_dates=True, user=OWNER)
        by_label = {i["label"]: i for i in fc["items"] if i["kind"] == "ar"}
        assert by_label["Invoice — SlowCo"]["date"] == "2026-10-31"  # +6 avg
        assert by_label["Invoice — NewCo"]["date"] == "2026-10-25"   # <3 samples
        adj = fc["layers"]["expected_pay_dates"]["adjustments"]
        assert adj == [{"customer": "SlowCo", "avg_days_late": 6, "samples": 3}]

        strict = await B.forecast(90, expected_pay_dates=False, user=OWNER)
        by_label = {i["label"]: i for i in strict["items"] if i["kind"] == "ar"}
        assert by_label["Invoice — SlowCo"]["date"] == "2026-10-25"
    _run(run())


# ---------------- run-rate band: cash basis, exclusions ----------------

def test_run_rate_band_is_cash_basis_and_excludes_scheduled_sources(monkeypatch):
    async def run():
        _fake_db(
            monkeypatch,
            journal_entries=[
                {"id": "e1", "tenant_id": TID, "status": "posted",
                 "entry_date": "2026-09-01", "source": "expense"},
                {"id": "e2", "tenant_id": TID, "status": "posted",
                 "entry_date": "2026-09-02", "source": "ap_payment"},
                {"id": "e3", "tenant_id": TID, "status": "posted",
                 "entry_date": "2026-09-03", "source": "invoice_issue"},
                {"id": "e4", "tenant_id": TID, "status": "draft",
                 "entry_date": "2026-09-04", "source": "bank_txn"}],
            journal_lines=[
                {"id": "l1", "tenant_id": TID, "entry_id": "e1", "status": "posted",
                 "entry_date": "2026-09-01", "account_code": "1020",
                 "debit": 0, "credit": 45000},          # unscheduled cash out
                {"id": "l2", "tenant_id": TID, "entry_id": "e2", "status": "posted",
                 "entry_date": "2026-09-02", "account_code": "1020",
                 "debit": 0, "credit": 900000},         # AP payment: excluded
                {"id": "l3", "tenant_id": TID, "entry_id": "e3", "status": "posted",
                 "entry_date": "2026-09-03", "account_code": "4000",
                 "debit": 777777, "credit": 0},         # non-cash account: ignored
                {"id": "l4", "tenant_id": TID, "entry_id": "e4", "status": "draft",
                 "entry_date": "2026-09-04", "account_code": "1020",
                 "debit": 555555, "credit": 0},         # draft: ignored
            ])
        fc = await B.forecast(90, run_rate=True, user=OWNER)
        band = fc["layers"]["run_rate"]
        assert band["basis"] == "cash" and band["window_days"] == 90
        assert band["daily_outflow_cents"] == 500   # 45000 / 90 only
        assert band["daily_inflow_cents"] == 0
        assert "ap_payment" in band["excluded_sources"]
        # with the band on, every point carries it in outflow
        assert all(p["outflow_cents"] == p["known_out_cents"] + 500
                   for p in fc["points"])
        off = await B.forecast(90, run_rate=False, user=OWNER)
        assert all(p["outflow_cents"] == p["known_out_cents"] for p in off["points"])
    _run(run())


# ---------------- weekly roll-up + response shape ----------------

def test_weekly_rollup_is_13_weeks_and_ties_to_daily(monkeypatch):
    async def run():
        _fake_db(monkeypatch,
                 invoices=[_inv(id="inv1", total_cents=70000, due_date="2026-10-25")])
        fc = await B.forecast(90, user=OWNER)
        assert len(fc["weekly"]) == 13
        assert sum(w["inflow_cents"] for w in fc["weekly"]) == \
            sum(p["inflow_cents"] for p in fc["points"])
        assert fc["weekly"][-1]["end_cash_cents"] == fc["end_cash_cents"]
        assert fc["weekly"][0]["start_date"] == TODAY
        # legacy response fields the Budget panel already consumes
        p0 = fc["points"][0]
        assert {"inflow_cents", "outflow_cents", "cash_cents",
                "known_in_cents", "known_out_cents"} <= set(p0)
        assert {"ar_cents", "bills_cents", "recurring_inflow_cents",
                "recurring_outflow_cents"} <= set(fc["scheduled"])
        assert isinstance(fc["assumptions"], list) and fc["assumptions"]
        short = await B.forecast(30, user=OWNER)
        assert len(short["weekly"]) == 5  # 31 points -> 4 full weeks + 3 days
    _run(run())


# ---------------- low-cash alerting ----------------

def test_low_cash_alert_raised_once_and_deduped(monkeypatch):
    async def run():
        fake = _fake_db(monkeypatch, start_cash=10000,
                        bills=[_bill(id="big", amount_cents=50000,
                                     due_date="2026-10-30")])
        raised = []

        async def fake_raise(tenant_id, user_id, atype, title, detail=""):
            raised.append(detail)
            await fake.security_alerts.insert_one(
                {"tenant_id": tenant_id, "type": atype, "detail": detail,
                 "acknowledged_at": None})
        monkeypatch.setattr(B.AL, "raise_alert", fake_raise)

        fc = await B.forecast(90, user=OWNER)
        assert fc["min_cash_cents"] == -40000
        assert fc["alerts"]["first_below_date"] == "2026-10-30"
        assert fc["alerts"]["alert_raised"] is True and len(raised) == 1

        fc2 = await B.forecast(90, user=OWNER)  # same projection -> deduped
        assert fc2["alerts"]["alert_raised"] is False and len(raised) == 1
    _run(run())


def test_no_alert_when_cash_stays_positive(monkeypatch):
    async def run():
        _fake_db(monkeypatch, start_cash=10000)
        raise_mock = AsyncMock()
        monkeypatch.setattr(B.AL, "raise_alert", raise_mock)
        fc = await B.forecast(90, user=OWNER)
        assert fc["alerts"]["first_below_date"] is None
        assert fc["alerts"]["alert_raised"] is False
        raise_mock.assert_not_awaited()
    _run(run())
