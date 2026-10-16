"""Market faces + trade x market skin resolver (public, like /niches).

GET /market-packs            -> all 46 market faces (lean subset)
GET /market-packs/resolve?trade=<niche_id>&market=<country_code>
    -> composed skin: market colors override trade colors (trade is the
       fallback), languages from the market, content (COA, services,
       checklist, copilot notes, KPIs, terms) from the trade.
       Unknown trade or market -> 404.
"""
from fastapi import APIRouter, HTTPException

from lib.market_packs import resolve_market_pack
from lib.markets import all_markets, public_market

router = APIRouter(prefix="/market-packs", tags=["market-packs"])


@router.get("")
async def list_market_packs():
    """All market faces — powers a signup market picker."""
    return [public_market(m) for m in all_markets()]


@router.get("/resolve")
async def resolve(trade: str, market: str):
    resolved = resolve_market_pack(trade, market)
    if resolved is None:
        raise HTTPException(404, "Unknown trade or market")
    return resolved
