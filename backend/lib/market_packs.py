"""Trade x market skin resolver (mirrors Momo's resolver semantics).

Composition rules:
  * colors:    market colors win; trade colors are the fallback for any
               key the market does not set.
  * languages: always from the market (AI receptionist languages).
  * content:   always from the trade pack — chart of accounts, services,
               checklist, copilot notes, KPIs, terminology, branding.
  * flag/name: from the market.

resolve_market_pack() returns None when the trade id or market code is
unknown (and does NOT fall back to a default trade the way get_pack
does — a skin resolution for a bogus trade is a caller error), so the
router can answer a clean 404.
"""
from lib.markets import get_market, public_market
from lib.niches import all_packs, public_pack


def compose_skin(pack: dict, market: dict) -> dict:
    """Pure composition of one validated trade pack + one market pack."""
    trade_colors = dict(pack.get("colors") or {})
    market_colors = {k: v for k, v in (market.get("colors") or {}).items() if v}
    colors = {**trade_colors, **market_colors}
    return {
        "trade_id": pack["id"],
        "market_code": market["code"],
        "trade": public_pack(pack),
        "market": public_market(market),
        # merged skin: market accent overrides the trade accent
        "colors": colors,
        "accent": colors.get("accent"),
        "flag": market["flag"],
        # languages come from the market
        "languages": list(market.get("languages") or []),
        # content comes from the trade
        "terms": pack.get("terms", {}),
        "revenue_accounts": pack.get("revenue_accounts", []),
        "trade_expense_accounts": pack.get("trade_expense_accounts", []),
        "services": pack.get("services", []),
        "checklist": pack.get("checklist", []),
        "copilot_notes": pack.get("copilot_notes", ""),
        "kpis": pack.get("kpis", []),
    }


def resolve_market_pack(trade_id: str | None, market_code: str | None) -> dict | None:
    """Resolved skin for (trade, market), or None if either is unknown."""
    if not trade_id or not market_code:
        return None
    market = get_market(market_code)
    if market is None:
        return None
    pack = next((p for p in all_packs() if p["id"] == str(trade_id).strip()), None)
    if pack is None:
        return None
    return compose_skin(pack, market)
