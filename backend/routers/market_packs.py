"""Market faces + trade x market skin resolver (public, like /niches).

GET /market-packs            -> all 46 market faces (lean subset)
GET /market-packs/resolve?trade=<niche_id>&market=<country_code>
    -> composed skin: market colors override trade colors (trade is the
       fallback), languages from the market, content (COA, services,
       checklist, copilot notes, KPIs, terms) from the trade.
       Unknown trade or market -> 404.
GET /market-packs/detect     -> the market-face default ruling
    (2026-11-01): echoes this request's detected country from the
    CF-IPCountry header (set by Cloudflare at the edge when the host
    path transits it; absent on direct-to-Render traffic) and the
    market it maps to. No header / unpacked country -> the US market
    with source "default" — never another market in its place.
"""
from fastapi import APIRouter, HTTPException, Request

from lib.market_packs import resolve_market_pack
from lib.markets import (DEFAULT_MARKET_CODE, all_markets, get_market,
                         market_for_country, public_market)

router = APIRouter(prefix="/market-packs", tags=["market-packs"])


@router.get("")
async def list_market_packs():
    """All market faces — powers a signup market picker."""
    return [public_market(m) for m in all_markets()]


@router.get("/detect")
async def detect_market(request: Request):
    """Detected market for THIS request's edge country, if any.

    The browser cannot read CF-IPCountry itself (it is a request
    header the edge adds), so the public pages ask here when the
    visitor has no stored choice. Response: {country, market_code,
    source, market} — source is "detected" when the header named a
    packed country, else "default" with the US market.
    """
    country = request.headers.get("cf-ipcountry")
    code = market_for_country(country)
    if code is not None:
        market = get_market(code)
        return {"country": code, "market_code": code, "source": "detected",
                "market": public_market(market)}
    return {"country": None, "market_code": DEFAULT_MARKET_CODE,
            "source": "default",
            "market": public_market(get_market(DEFAULT_MARKET_CODE))}


@router.get("/resolve")
async def resolve(trade: str, market: str):
    resolved = resolve_market_pack(trade, market)
    if resolved is None:
        raise HTTPException(404, "Unknown trade or market")
    return resolved
