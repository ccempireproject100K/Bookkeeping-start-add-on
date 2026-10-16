"""Phase 4 multi-currency: report translation, Stripe presentment math.

Covers: _needs_translation detection, per-line translation at entry-date
rates, translated flag + currency metadata on trial_balance/pnl/balance_sheet,
byte-identical amounts for USD-only books, and the Stripe minor-units
conversion for foreign-currency invoices. Fully mocked (no live MongoDB).

Run from the backend directory with: python -m pytest tests/test_currency_phase4.py
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import lib.ledger as L
from lib.currencies import to_minor_units


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = list(docs)

    def find(self, filt=None, proj=None):
        filt = filt or {}
        out = []
        for d in self.docs:
            ok = True
            for k, v in filt.items():
                if isinstance(v, dict):
                    for op, val in v.items():
                        if op == "$lte" and not (d.get(k) <= val):
                            ok = False
                        if op == "$gte" and not (d.get(k) >= val):
                            ok = False
                elif d.get(k) != v:
                    ok = False
            if ok:
                out.append(d)
        return FakeCursor(out)

    async def aggregate(self, pipeline):
        # Tests stub this per-case via monkeypatching _translated_balances'
        # dependencies; default returns empty.
        return []


def make_db(entries=(), lines=()):
    return SimpleNamespace(
        accounts=FakeCollection(),
        journal_entries=FakeCollection(entries),
        journal_lines=FakeCollection(lines),
        fiscal_periods=FakeCollection(),
        audit_log=FakeCollection(),
        counters=FakeCollection(),
    )


def run(coro):
    # Dedicated loop, NOT asyncio.get_event_loop(): other test modules call
    # asyncio.run(), which unsets/closes the thread's current loop on exit,
    # so the legacy get_event_loop() pattern broke (RuntimeError: no current
    # event loop) whenever suite order shifted. Same fix PR #123 applied.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------- _needs_translation ----------------

def test_needs_translation_false_for_usd_only():
    db = make_db(entries=[
        {"tenant_id": "t", "id": "e1", "status": "posted", "currency": "USD",
         "entry_date": "2026-10-01"},
    ])
    assert run(L._needs_translation(db, "t", "USD", as_of="2026-10-31")) is False


def test_needs_translation_true_with_foreign_entry():
    db = make_db(entries=[
        {"tenant_id": "t", "id": "e1", "status": "posted", "currency": "EUR",
         "entry_date": "2026-10-01"},
    ])
    assert run(L._needs_translation(db, "t", "USD", as_of="2026-10-31")) is True


def test_needs_translation_missing_currency_treated_as_usd():
    db = make_db(entries=[
        {"tenant_id": "t", "id": "e1", "status": "posted",
         "entry_date": "2026-10-01"},  # no currency field (pre-migration)
    ])
    assert run(L._needs_translation(db, "t", "USD", as_of="2026-10-31")) is False


# ---------------- _translated_balances ----------------

def _joined_rows():
    # Simulates the $lookup output: lines already joined to entry currency/date.
    return [
        {"account_code": "4020", "debit": 0, "credit": 10000,
         "currency": "EUR", "entry_date": "2026-10-01"},
        {"account_code": "4020", "debit": 0, "credit": 20000,
         "currency": "USD", "entry_date": "2026-10-02"},
    ]


def test_translated_balances_converts_at_entry_date_rate(monkeypatch):
    async def fake_aggregate(pipeline):
        return _joined_rows()

    async def fake_get_rate(frm, to, date, db=None, _pivot=True):
        assert (frm, to) == ("EUR", "USD")
        assert date == "2026-10-01"
        return 1.10

    monkeypatch.setattr("lib.fx.get_rate", fake_get_rate)
    db = make_db()
    # patch the Scoped aggregate path: _translated_balances uses s["lines"].aggregate
    import lib.base as base

    orig_scoped = L._scoped

    def fake_scoped(db_, tid):
        s = orig_scoped(db_, tid)
        s["lines"] = SimpleNamespace(aggregate=fake_aggregate)
        return s

    monkeypatch.setattr(L, "_scoped", fake_scoped)
    bals = run(L._translated_balances(db, "t", "USD", as_of="2026-10-31"))
    # EUR 100.00 @1.10 = USD 110.00 -> 11000 x100; USD 200.00 untouched
    assert bals["4020"]["credit"] == 11000 + 20000
    assert bals["4020"]["debit"] == 0


def test_translated_balances_same_currency_untouched(monkeypatch):
    async def fake_aggregate(pipeline):
        return [{"account_code": "1010", "debit": 5000, "credit": 0,
                 "currency": "USD", "entry_date": "2026-10-01"}]

    async def boom(*a, **k):
        raise AssertionError("no rate lookup should happen for same-currency")

    monkeypatch.setattr("lib.fx.get_rate", boom)
    import lib.base as base
    orig_scoped = L._scoped

    def fake_scoped(db_, tid):
        s = orig_scoped(db_, tid)
        s["lines"] = SimpleNamespace(aggregate=fake_aggregate)
        return s

    monkeypatch.setattr(L, "_scoped", fake_scoped)
    bals = run(L._translated_balances(make_db(), "t", "USD"))
    assert bals["1010"] == {"debit": 5000, "credit": 0}


# ---------------- report metadata ----------------

def test_trial_balance_metadata_usd_only(monkeypatch):
    async def fake_balances(db, tenant_id, as_of=None, date_from=None):
        return {"1010": {"debit": 1000, "credit": 0}}

    async def fake_needs(db, tid, target, as_of=None, date_from=None):
        return False

    monkeypatch.setattr(L, "_balances", fake_balances)
    monkeypatch.setattr(L, "_needs_translation", fake_needs)
    out = run(L.trial_balance(make_db(), "t", "2026-10-31"))
    assert out["currency"] == "USD"
    assert out["translated"] is False
    assert out["total_debit"] == 1000


def test_pnl_metadata_translated(monkeypatch):
    async def fake_translated(db, tid, target, as_of=None, date_from=None):
        return {"4020": {"debit": 0, "credit": 31000}}

    async def fake_needs(db, tid, target, as_of=None, date_from=None):
        return True

    monkeypatch.setattr(L, "_translated_balances", fake_translated)
    monkeypatch.setattr(L, "_needs_translation", fake_needs)
    monkeypatch.setattr(L, "ACCOUNT_TYPE_OF", {"4020": "revenue"})
    monkeypatch.setattr(L, "NORMAL_BALANCE_OF", {"4020": "credit"})
    monkeypatch.setattr(L, "NAME_OF", {"4020": "Sales"})
    out = run(L.pnl(make_db(), "t", "2026-10-01", "2026-10-31", target_currency="EUR"))
    assert out["currency"] == "EUR"
    assert out["translated"] is True
    assert out["revenue_total"] == 31000


# ---------------- Stripe presentment math ----------------

def test_stripe_amount_usd_identity():
    # balance stored x100; USD minor units are cents -> identity
    assert to_minor_units(10050 / 100.0, "USD") == 10050


def test_stripe_amount_jpy_zero_decimal():
    # JPY 500 invoice stored as 50000 (x100); Stripe wants 500
    assert to_minor_units(50000 / 100.0, "JPY") == 500


def test_stripe_amount_eur_rounding():
    assert to_minor_units(9999 / 100.0, "EUR") == 9999
