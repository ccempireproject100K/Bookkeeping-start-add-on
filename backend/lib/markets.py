"""Market faces: one lean YAML pack per Stripe-supported country.

Each file in backend/markets/ describes a market (46 total, the locked
list in proposed-45-markets.md): a theme accent, the AI receptionist
languages for that market, the country flag, and the market's ISO 4217
currency. Markets carry NO trade content — chart of accounts, services
and templates come from the trade (niche) pack. See lib/market_packs.py
for the trade x market resolver.

Adding market #47 = adding one YAML file. No code changes.
"""
import os

import yaml

PACKS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "markets")

_REQUIRED = ["code", "name", "flag", "colors", "languages", "currency"]

_cache: dict = {}


def _validate(market: dict, path: str) -> dict:
    for key in _REQUIRED:
        if not market.get(key):
            raise ValueError(f"market pack {path}: missing required key '{key}'")
    code = str(market["code"]).upper()
    if len(code) != 2 or not code.isalpha():
        raise ValueError(f"market pack {path}: code must be a 2-letter country code")
    market["code"] = code
    if not market["colors"].get("accent"):
        raise ValueError(f"market pack {path}: colors.accent is required")
    if not all(isinstance(x, str) and x for x in market["languages"]):
        raise ValueError(f"market pack {path}: languages must be non-empty strings")
    currency = str(market["currency"]).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError(f"market pack {path}: currency must be a 3-letter ISO 4217 code")
    market["currency"] = currency
    market.setdefault("extended_network", False)
    return market


def all_markets() -> list:
    """All markets, validated, sorted by name. Cached after first load."""
    global _cache
    if _cache:
        return sorted(_cache.values(), key=lambda m: m["name"])
    if not os.path.isdir(PACKS_DIR):
        raise RuntimeError(f"markets directory missing: {PACKS_DIR}")
    for fname in sorted(os.listdir(PACKS_DIR)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(PACKS_DIR, fname)
        with open(path, "r", encoding="utf-8") as fh:
            market = yaml.safe_load(fh)
        market = _validate(market, fname)
        if market["code"] in _cache:
            raise ValueError(f"duplicate market code '{market['code']}' in {fname}")
        _cache[market["code"]] = market
    return sorted(_cache.values(), key=lambda m: m["name"])


def get_market(code: str | None) -> dict | None:
    """Market for a country code (case-insensitive), or None if unknown.

    Unlike niches there is no default market: an unknown code is a
    caller error (the resolver turns it into a 404), never a fallback.
    """
    if not code:
        return None
    for m in all_markets():
        if m["code"] == str(code).strip().upper():
            return m
    return None


def public_market(market: dict) -> dict:
    """Safe subset for public endpoints."""
    return {
        "code": market["code"], "name": market["name"],
        "flag": market["flag"], "colors": market.get("colors", {}),
        "languages": market.get("languages", []),
        "currency": market.get("currency"),
        "extended_network": bool(market.get("extended_network", False)),
    }


def reload():
    """For tests."""
    global _cache
    _cache = {}
