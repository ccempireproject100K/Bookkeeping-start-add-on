"""Phase 1 multi-currency data model: metadata, validation, minor units,
and the idempotent backfill. No live MongoDB needed (fully mocked).

Run from the backend directory with: python -m pytest tests/test_currency_phase1.py
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib import currencies as C
from lib.currency_migration import backfill_currency_fields


def test_twenty_currencies_most_used_first():
    assert len(C.CURRENCIES) == 20
    assert list(C.CURRENCIES)[:4] == ["USD", "EUR", "JPY", "GBP"]
    for code, meta in C.CURRENCIES.items():
        assert meta["stripe"] is True  # all 20 support Stripe presentment
        assert meta["decimals"] in (0, 2)


def test_zero_decimal_currencies():
    assert C.is_zero_decimal("JPY")
    assert C.is_zero_decimal("KRW")
    assert not C.is_zero_decimal("USD")
    assert C.decimals_for("JPY") == 0
    assert C.decimals_for("EUR") == 2


def test_normalize_validates():
    assert C.normalize("usd") == "USD"
    assert C.normalize(" eur ") == "EUR"
    assert C.normalize(None) == "USD"
    with pytest.raises(ValueError):
        C.normalize("XXX")


def test_minor_units_zero_decimal():
    assert C.to_minor_units(500, "JPY") == 500
    assert C.to_minor_units(5.00, "USD") == 500
    assert C.to_minor_units(10.50, "EUR") == 1050
    assert C.from_minor_units(500, "JPY") == 500
    assert C.from_minor_units(500, "USD") == 5.00


def test_get_base_currency_prefers_new_key_then_legacy():
    assert C.get_base_currency({"base_currency": "EUR"}) == "EUR"
    assert C.get_base_currency({"currency": "GBP"}) == "GBP"
    assert C.get_base_currency({"base_currency": "EUR", "currency": "GBP"}) == "EUR"
    assert C.get_base_currency({}) == "USD"
    assert C.get_base_currency(None) == "USD"
    assert C.get_base_currency({"base_currency": "XXX"}) == "USD"  # invalid -> default


def _fake_collection(matched):
    res = SimpleNamespace(matched_count=matched)
    coll = SimpleNamespace(update_many=AsyncMock(return_value=res))
    return coll


def test_backfill_is_idempotent_and_scoped():
    calls = []

    def make(name, matched):
        async def update_many(filt, update):
            calls.append((name, filt, update))
            return SimpleNamespace(matched_count=matched)
        return SimpleNamespace(update_many=update_many)

    db = SimpleNamespace(**{n: make(n, 3) for n in
                             ("journal_entries", "invoices", "bills", "bank_accounts")},
                         tenants=make("tenants", 1))
    report = asyncio.run(backfill_currency_fields(db))
    assert report == {"journal_entries": 3, "invoices": 3, "bills": 3,
                      "bank_accounts": 3, "tenants.base_currency": 1}
    for name, filt, update in calls:
        if name == "tenants":
            assert filt == {"settings.base_currency": {"$exists": False}}
            assert update == {"$set": {"settings.base_currency": "USD"}}
        else:
            assert filt == {"currency": {"$exists": False}}
            assert update == {"$set": {"currency": "USD"}}


def test_backfill_second_run_stamps_nothing():
    db = SimpleNamespace(**{n: _fake_collection(0) for n in
                             ("journal_entries", "invoices", "bills", "bank_accounts")},
                         tenants=_fake_collection(0))
    report = asyncio.run(backfill_currency_fields(db))
    assert sum(report.values()) == 0
