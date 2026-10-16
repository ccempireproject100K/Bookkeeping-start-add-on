"""Niche packs 20->33 + 46 market faces + trade x market skin resolver.

No live MongoDB / server needed: exercises the real YAML loaders, the
pure resolver composition, and the public router on a bare FastAPI app.

Run from the backend directory with:
    python -m pytest tests/test_niche_market_expansion.py
"""
import asyncio

import pytest
from fastapi import HTTPException

from lib import niches, markets
from lib.market_packs import compose_skin, resolve_market_pack
from routers.market_packs import list_market_packs, resolve

EXPECTED_TRADES = {
    "hvac", "plumbing", "electrical", "roofing", "appliance_repair",
    "garage_door", "pest_control", "landscaping", "pool_service",
    "painting", "handyman", "solar", "cleaning", "tree_service",
    "fencing", "restoration", "auto_repair", "barbershop",
    "real_estate", "freelancer",
    # the 13 added by this expansion
    "locksmith", "septic_well", "kitchen_bath", "windows_doors",
    "flooring", "foundation_repair", "whole_home_generators",
    "smart_home", "junk_removal", "decks_outdoor", "irrigation",
    "siding_gutters", "home_inspection",
}

ORIGINAL_20 = EXPECTED_TRADES - {
    "locksmith", "septic_well", "kitchen_bath", "windows_doors",
    "flooring", "foundation_repair", "whole_home_generators",
    "smart_home", "junk_removal", "decks_outdoor", "irrigation",
    "siding_gutters", "home_inspection",
}

EXPECTED_MARKETS = {
    "AU", "AT", "BE", "BR", "BG", "CA", "CY", "CZ", "DK", "EE", "FI",
    "FR", "DE", "GH", "GI", "HK", "HU", "IE", "IT", "JP", "KE", "LV",
    "LI", "LT", "LU", "MY", "MT", "MX", "NL", "NZ", "NG", "NO", "PL",
    "PT", "RO", "SG", "SK", "SI", "ZA", "ES", "SE", "CH", "TH", "AE",
    "GB", "US",
}


@pytest.fixture(autouse=True)
def _fresh_caches():
    niches.reload()
    markets.reload()
    yield
    niches.reload()
    markets.reload()


def test_all_33_trade_packs_load_and_validate():
    packs = niches.all_packs()
    assert len(packs) == 33
    assert {p["id"] for p in packs} == EXPECTED_TRADES
    assert ORIGINAL_20 <= {p["id"] for p in packs}  # original ids stable
    for p in packs:
        assert p["colors"]["accent"]  # skin fallback color present
        assert p["revenue_accounts"] and p["trade_expense_accounts"]
        for acct in p["revenue_accounts"] + p["trade_expense_accounts"]:
            assert isinstance(acct["code"], str) and acct["name"]
        assert p["services"] and p["checklist"] and p["kpis"]


def test_all_46_markets_load_and_validate():
    all_m = markets.all_markets()
    assert len(all_m) == 46
    assert {m["code"] for m in all_m} == EXPECTED_MARKETS
    for m in all_m:
        assert m["flag"] and m["colors"]["accent"] and m["languages"]


def test_get_market_is_case_insensitive_and_strict():
    assert markets.get_market("de")["name"] == "Germany"
    assert markets.get_market("DE")["languages"] == ["de"]
    assert markets.get_market("XX") is None
    assert markets.get_market(None) is None


def test_resolver_market_color_wins_content_from_trade():
    r = resolve_market_pack("hvac", "DE")
    assert r is not None
    de = markets.get_market("DE")
    hvac = niches.get_pack("hvac")
    assert r["colors"]["accent"] == de["colors"]["accent"] == "#ffcc00"
    assert r["accent"] == "#ffcc00"
    assert r["languages"] == ["de"]  # languages from the market
    assert r["flag"] == "🇩🇪"
    # content from the trade
    assert r["revenue_accounts"] == hvac["revenue_accounts"]
    assert r["services"] == hvac["services"]
    assert r["terms"] == hvac["terms"]
    assert r["trade"]["id"] == "hvac" and r["market"]["code"] == "DE"


def test_resolver_trade_color_is_fallback():
    pack = {"id": "t", "trade": "T", "product_name": "P", "tagline": "x",
            "terms": {}, "colors": {"accent": "#111111"},
            "revenue_accounts": [], "services": []}
    market = {"code": "DE", "name": "Germany", "flag": "🇩🇪",
              "colors": {}, "languages": ["de"]}
    r = compose_skin(pack, market)
    assert r["accent"] == "#111111"  # trade color survives when market sets none
    # a market color for a *different* key must not clobber the accent fallback
    market2 = dict(market, colors={"primary": "#222222"})
    r2 = compose_skin(pack, market2)
    assert r2["colors"] == {"accent": "#111111", "primary": "#222222"}


def test_resolver_unknown_trade_or_market_returns_none():
    assert resolve_market_pack("nope", "DE") is None
    assert resolve_market_pack("hvac", "XX") is None
    assert resolve_market_pack(None, "DE") is None
    assert resolve_market_pack("hvac", None) is None
    # bogus trade must NOT silently fall back to the default niche
    assert resolve_market_pack("barbershops", "US") is None


def test_endpoints_list_and_resolve():
    listing = asyncio.run(list_market_packs())
    assert len(listing) == 46
    body = asyncio.run(resolve(trade="locksmith", market="us"))
    assert body["trade_id"] == "locksmith" and body["market_code"] == "US"
    assert body["languages"] == ["en", "es"]
    assert body["revenue_accounts"]  # trade content present


def test_endpoints_unknown_returns_404():
    with pytest.raises(HTTPException) as e1:
        asyncio.run(resolve(trade="nope", market="US"))
    assert e1.value.status_code == 404
    with pytest.raises(HTTPException) as e2:
        asyncio.run(resolve(trade="hvac", market="XX"))
    assert e2.value.status_code == 404
