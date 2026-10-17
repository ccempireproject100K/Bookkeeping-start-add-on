"""Market-face wiring: packs -> tenant -> edition -> public resolve.

The 46 market packs have existed (with a working resolver and public
API) since the niche/market expansion, but nothing consumed them: no
tenant stored a market, /edition composed trade only, and no signup
picker existed. This file pins the wiring end to end, mirroring
test_niches_spa_contract.py's approach — exercise the real loaders,
the real resolver, the real router functions and the real
create_workspace/edition code paths (DB mocked, like
test_registration_workspace_isolation.py).

Run from the backend directory with:
    python -m pytest tests/test_market_face_wiring.py
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from lib import currencies as C
from lib import markets, niches
from lib.market_packs import resolve_market_pack
from routers import auth, edition
from routers.market_packs import list_market_packs, resolve

EXPECTED_CURRENCIES = {
    "MX": "MXN", "US": "USD", "CA": "CAD", "GB": "GBP", "DE": "EUR",
    "FR": "EUR", "ES": "EUR", "JP": "JPY", "LI": "CHF", "CH": "CHF",
    "AU": "AUD", "BR": "BRL", "ZA": "ZAR", "GH": "GHS", "KE": "KES",
    "NG": "NGN", "AE": "AED", "TH": "THB", "MY": "MYR", "SG": "SGD",
}


@pytest.fixture(autouse=True)
def _fresh_caches():
    niches.reload()
    markets.reload()
    yield
    niches.reload()
    markets.reload()


# ---------- Market pack data ----------

def test_every_market_pack_carries_an_iso_currency():
    all_m = markets.all_markets()
    assert len(all_m) == 46
    for m in all_m:
        cur = m.get("currency")
        assert isinstance(cur, str) and len(cur) == 3 and cur.isalpha(), m["code"]
    for code, expected in EXPECTED_CURRENCIES.items():
        assert markets.get_market(code)["currency"] == expected, code


def test_public_market_list_exposes_currency():
    listing = asyncio.run(list_market_packs())
    assert len(listing) == 46
    for m in listing:
        assert m["currency"], m["code"]
    mx = next(m for m in listing if m["code"] == "MX")
    assert mx["currency"] == "MXN" and mx["colors"]["accent"] == "#006847"


def test_resolve_payload_carries_market_currency():
    body = asyncio.run(resolve(trade="plumbing", market="MX"))
    assert body["currency"] == "MXN"
    assert body["market"]["currency"] == "MXN"
    assert body["accent"] == "#006847"  # market accent wins
    assert body["languages"] == ["es"]  # languages from the market
    assert body["trade"]["id"] == "plumbing"  # content from the trade
    ca = asyncio.run(resolve(trade="hvac", market="CA"))
    assert ca["currency"] == "CAD"


# ---------- Signup: tenant stores market_code, currency seeded ----------

def _storage(monkeypatch):
    users = SimpleNamespace(
        find_one=AsyncMock(return_value=None), insert_one=AsyncMock())
    tenants = SimpleNamespace(insert_one=AsyncMock())
    monkeypatch.setattr(auth.db, "users", users)
    monkeypatch.setattr(auth.db, "tenants", tenants)
    monkeypatch.setattr(auth, "seed_coa", AsyncMock())
    monkeypatch.setattr(auth, "hash_password", lambda value: "hashed-for-test")
    return users, tenants


def _created_tenant(tenants):
    assert tenants.insert_one.await_count == 1
    return tenants.insert_one.await_args.args[0]


def test_signup_with_market_stores_code_and_seeds_currency(monkeypatch):
    _, tenants = _storage(monkeypatch)
    asyncio.run(auth.create_workspace(
        "mx@example.com", "secure-password-1", "Owner", "Taller MX",
        market_code="mx"))
    tenant = _created_tenant(tenants)
    assert tenant["market_code"] == "MX"  # normalized
    assert tenant["settings"]["base_currency"] == "MXN"


def test_signup_with_unsupported_currency_market_keeps_usd_books(monkeypatch):
    # GHS is a real market currency but not one of the 20 the
    # multi-currency system supports — the face carries it, the books
    # keep the USD default rather than an unusable base currency.
    _, tenants = _storage(monkeypatch)
    asyncio.run(auth.create_workspace(
        "gh@example.com", "secure-password-1", "Owner", "Accra Shop",
        market_code="GH"))
    tenant = _created_tenant(tenants)
    assert tenant["market_code"] == "GH"
    assert markets.get_market("GH")["currency"] == "GHS"
    assert not C.is_supported("GHS")
    assert tenant["settings"]["base_currency"] == "USD"


def test_signup_without_market_is_unchanged(monkeypatch):
    _, tenants = _storage(monkeypatch)
    asyncio.run(auth.create_workspace(
        "plain@example.com", "secure-password-1", "Owner", "Plain Shop"))
    tenant = _created_tenant(tenants)
    assert tenant["market_code"] is None
    assert tenant["settings"]["base_currency"] == "USD"


def test_signup_with_unknown_market_is_400_and_creates_nothing(monkeypatch):
    users, tenants = _storage(monkeypatch)
    with pytest.raises(HTTPException) as err:
        asyncio.run(auth.create_workspace(
            "bad@example.com", "secure-password-1", "Owner", "Bad Shop",
            market_code="XX"))
    assert err.value.status_code == 400
    tenants.insert_one.assert_not_awaited()
    users.insert_one.assert_not_awaited()


def test_register_body_accepts_market_code():
    body = auth.RegisterBody(email="a@example.com", password="a-valid-password",
                             name="A", shop_name="Shop", market_code="mx")
    assert body.market_code == "mx"
    assert auth.RegisterBody(email="a@example.com",
                               password="a-valid-password",
                               name="A").market_code is None


# ---------- /edition composition ----------

def _edition_for(monkeypatch, tenant):
    tenants = SimpleNamespace(find_one=AsyncMock(return_value=tenant))
    monkeypatch.setattr(edition.db, "tenants", tenants)
    request = SimpleNamespace(headers={})
    return asyncio.run(edition.edition_profile(
        request, user={"tenant_id": (tenant or {}).get("id", "t1")}))


def test_edition_without_market_is_the_trade_only_shape(monkeypatch):
    payload = _edition_for(monkeypatch,
                           {"id": "t1", "name": "Shop", "niche_id": "plumbing",
                            "settings": {}})
    assert "market" not in payload
    assert payload["colors"] == niches.get_pack("plumbing")["colors"]


def test_edition_with_market_composes_the_skin(monkeypatch):
    payload = _edition_for(monkeypatch,
                           {"id": "t1", "name": "Taller", "niche_id": "plumbing",
                            "market_code": "MX", "settings": {}})
    assert payload["colors"]["accent"] == "#006847"  # market accent wins
    assert payload["terms"] == niches.get_pack("plumbing")["terms"]  # trade content
    market = payload["market"]
    assert market["code"] == "MX" and market["name"] == "Mexico"
    assert market["flag"] == "🇲🇽"
    assert market["accent"] == "#006847"
    assert market["languages"] == ["es"]
    assert market["currency"] == "MXN"


def test_edition_with_stale_market_code_degrades_to_trade_only(monkeypatch):
    payload = _edition_for(monkeypatch,
                           {"id": "t1", "name": "Shop", "niche_id": "hvac",
                            "market_code": "XX", "settings": {}})
    assert "market" not in payload
    assert payload["colors"] == niches.get_pack("hvac")["colors"]


# ---------- PATCH /edition/market ----------

def _switch(monkeypatch, code):
    tenants = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(edition.db, "tenants", tenants)
    result = asyncio.run(edition.switch_market(
        edition.MarketBody(market_code=code), user={"tenant_id": "t1"}))
    return result, tenants


def test_switch_market_sets_a_valid_market(monkeypatch):
    result, tenants = _switch(monkeypatch, "mx")
    assert result["ok"] and result["market_code"] == "MX"
    assert result["market"]["currency"] == "MXN"
    tenants.update_one.assert_awaited_once_with(
        {"id": "t1"}, {"$set": {"market_code": "MX"}})


def test_switch_market_clears_on_empty(monkeypatch):
    result, tenants = _switch(monkeypatch, None)
    assert result["market_code"] is None and result["market"] is None
    tenants.update_one.assert_awaited_once_with(
        {"id": "t1"}, {"$set": {"market_code": None}})


def test_switch_market_rejects_unknown(monkeypatch):
    tenants = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(edition.db, "tenants", tenants)
    with pytest.raises(HTTPException) as err:
        asyncio.run(edition.switch_market(
            edition.MarketBody(market_code="XX"), user={"tenant_id": "t1"}))
    assert err.value.status_code == 400
    tenants.update_one.assert_not_awaited()
