"""Phase 2 multi-currency: exchange-rate service.

Covers conversion math, EUR-pivot cross-rate derivation, the
same-currency shortcut, missing-rate errors, idempotent re-fetch,
ECB XML parsing, graceful network degradation, and history seeding.
No live MongoDB or network needed (fully mocked).

Run from the backend directory with: python -m pytest tests/test_fx_phase2.py
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import lib.fx as fx
from lib.fx import FXRateError

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
    xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube>
    <Cube time="2026-10-15">
      <Cube currency="USD" rate="1.0823"/>
      <Cube currency="JPY" rate="162.45"/>
      <Cube currency="GBP" rate="0.8532"/>
    </Cube>
    <Cube time="2026-10-14">
      <Cube currency="USD" rate="1.0791"/>
      <Cube currency="JPY" rate="161.90"/>
      <Cube currency="GBP" rate="0.8511"/>
    </Cube>
  </Cube>
</gesmes:Envelope>"""


class FakeCollection:
    """Minimal async Mongo stand-in: find_one/$lte/sort, upsert, count."""

    def __init__(self):
        self.docs = []

    def _match(self, doc, filt):
        for key, cond in filt.items():
            if isinstance(cond, dict) and "$lte" in cond:
                if not (doc.get(key) is not None
                        and doc.get(key) <= cond["$lte"]):
                    return False
            elif doc.get(key) != cond:
                return False
        return True

    async def find_one(self, filt, sort=None):
        cands = [d for d in self.docs if self._match(d, filt)]
        if sort:
            for key, direction in reversed(sort):
                cands.sort(key=lambda d: d[key], reverse=(direction == -1))
        return cands[0] if cands else None

    async def update_one(self, filt, update, upsert=False):
        for doc in self.docs:
            if self._match(doc, filt):
                doc.update(update["$set"])
                return SimpleNamespace(matched_count=1, upserted_id=None)
        if upsert:
            new = dict(filt)
            new.update(update["$set"])
            self.docs.append(new)
            return SimpleNamespace(matched_count=0, upserted_id="new")
        return SimpleNamespace(matched_count=0, upserted_id=None)

    async def count_documents(self, filt):
        return sum(1 for d in self.docs if self._match(d, filt))


def _db_with(*rate_docs):
    coll = FakeCollection()
    coll.docs.extend(rate_docs)
    return SimpleNamespace(rates=coll)


def _doc(base, quote, date, rate):
    return {"base": base, "quote": quote, "date": date, "rate": rate,
            "source": "ecb"}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_same_currency_returns_one_without_db_hit():
    coll = FakeCollection()
    coll.find_one = AsyncMock(side_effect=AssertionError("db hit!"))
    db = SimpleNamespace(rates=coll)
    assert run(fx.get_rate("USD", "USD", "2026-10-15", db=db)) == 1.0
    assert run(fx.convert(123.45, "eur", "EUR", "2026-10-15", db=db)) == 123.45


def test_direct_rate_lookup():
    db = _db_with(_doc("EUR", "USD", "2026-10-15", 1.0823))
    assert run(fx.get_rate("EUR", "USD", "2026-10-15", db=db)) == 1.0823


def test_latest_on_or_before_date():
    db = _db_with(
        _doc("EUR", "USD", "2026-10-10", 1.0700),
        _doc("EUR", "USD", "2026-10-15", 1.0823),
    )
    # Exact day, in-between day (no weekend ECB publish), and future day.
    assert run(fx.get_rate("EUR", "USD", "2026-10-15", db=db)) == 1.0823
    assert run(fx.get_rate("EUR", "USD", "2026-10-12", db=db)) == 1.0700
    assert run(fx.get_rate("EUR", "USD", "2026-12-01", db=db)) == 1.0823


def test_cross_rate_derivation_via_eur():
    db = _db_with(
        _doc("EUR", "USD", "2026-10-15", 1.10),
        _doc("EUR", "GBP", "2026-10-15", 0.85),
    )
    rate = run(fx.get_rate("USD", "GBP", "2026-10-15", db=db))
    assert rate == pytest.approx(0.85 / 1.10)
    # 100 USD -> GBP at 0.7727... rounds to 77.27
    assert run(fx.convert(100, "USD", "GBP", "2026-10-15", db=db)) == 77.27


def test_inverse_rate():
    db = _db_with(_doc("EUR", "USD", "2026-10-15", 1.10))
    rate = run(fx.get_rate("USD", "EUR", "2026-10-15", db=db))
    assert rate == pytest.approx(1 / 1.10)


def test_missing_rate_raises_clear_error():
    db = _db_with(_doc("EUR", "USD", "2026-10-15", 1.10))
    with pytest.raises(FXRateError) as ei:
        run(fx.get_rate("USD", "JPY", "2026-10-15", db=db))
    assert "USD->JPY" in str(ei.value)
    # Date before any stored rate also raises.
    with pytest.raises(FXRateError):
        run(fx.get_rate("EUR", "USD", "2020-01-01", db=db))
    # Unknown currency codes are rejected by Phase-1 validation.
    with pytest.raises(ValueError):
        run(fx.get_rate("USD", "XXX", "2026-10-15", db=db))


def test_convert_rounds_to_quote_decimals():
    db = _db_with(_doc("EUR", "USD", "2026-10-15", 1.10),
                  _doc("EUR", "JPY", "2026-10-15", 162.45))
    # JPY is zero-decimal: no fractional yen.
    # 10 USD * (162.45/1.10) = 1476.82 -> 1477
    assert run(fx.convert(10, "USD", "JPY", "2026-10-15", db=db)) == 1477.0
    assert run(fx.convert(10, "USD", "JPY", "2026-10-15", db=db)) % 1 == 0


def test_parse_ecb_xml():
    parsed = fx.parse_ecb_xml(SAMPLE_XML)
    assert len(parsed) == 2
    assert parsed[0][0] == "2026-10-15"
    assert parsed[0][1]["USD"] == pytest.approx(1.0823)
    assert parsed[0][1]["JPY"] == pytest.approx(162.45)
    assert parsed[1][0] == "2026-10-14"


def test_store_rates_idempotent():
    db = _db_with()
    n1 = run(fx.store_rates("2026-10-15", {"USD": 1.0823, "JPY": 162.45,
                                           "GBP": 0.8532}, db=db))
    n2 = run(fx.store_rates("2026-10-15", {"USD": 1.0823, "JPY": 162.45,
                                           "GBP": 0.8532}, db=db))
    assert n1 == n2 == 3
    assert len(db.rates.docs) == 3  # no duplicates on re-fetch
    assert all(d["base"] == "EUR" and d["source"] == "ecb"
               for d in db.rates.docs)


def test_fetch_daily_stores_eur_pairs(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return SAMPLE_XML

    monkeypatch.setattr(fx, "_fetch_url", fake_fetch)
    db = _db_with()
    report = run(fx.fetch_daily_rates(db=db))
    assert fx.ECB_DAILY_URL in calls[0]
    assert report["fetched"] == 6  # 3 currencies x 2 days
    assert set(report["dates"]) == {"2026-10-15", "2026-10-14"}
    assert run(fx.get_rate("EUR", "USD", "2026-10-15", db=db)) == pytest.approx(1.0823)


def test_fetch_graceful_on_network_error(monkeypatch):
    def boom(url):
        raise OSError("no route to host")

    monkeypatch.setattr(fx, "_fetch_url", boom)
    db = _db_with()
    report = run(fx.fetch_daily_rates(db=db))  # must not raise
    assert report["fetched"] == 0
    assert "error" in report


def test_seed_backfill_uses_history_feed_then_daily(monkeypatch):
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return SAMPLE_XML

    monkeypatch.setattr(fx, "_fetch_url", fake_fetch)
    db = _db_with()
    report = run(fx.refresh_rates(db=db))
    assert report["seed"]["seeded"] is True
    assert fx.ECB_HIST_URL in seen[0]
    # Second refresh: collection no longer empty -> daily feed only.
    seen.clear()
    report2 = run(fx.refresh_rates(db=db))
    assert report2["seed"]["seeded"] is False
    assert seen == [fx.ECB_DAILY_URL]


def test_backfill_keeps_only_recent_days(monkeypatch):
    old_xml = SAMPLE_XML.replace("2026-10-14", "2020-01-02")
    monkeypatch.setattr(fx, "_fetch_url", lambda url: old_xml)
    db = _db_with()
    report = run(fx.backfill_history(days=30, db=db))
    dates = {d["date"] for d in db.rates.docs}
    assert "2020-01-02" not in dates
    assert "2026-10-15" in dates
    assert report["fetched"] == 3
