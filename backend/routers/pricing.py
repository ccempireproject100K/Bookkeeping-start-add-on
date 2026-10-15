"""Dynamic price guidance: what should you quote?

Learns from the shop's own estimate history — which prices won, which lost —
and combines that with the job's cost and a target margin to suggest a price.
Shows the expected win rate at that price and the range the shop usually
wins in.
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from db import db
from lib.auth import require
from lib.base import Scoped, cents_to_dollars

router = APIRouter(prefix="/pricing", tags=["pricing"])

WON = ("accepted", "converted")
LOST = ("declined",)
DECIDED = WON + LOST


def _money(cents) -> str:
    return f"${cents_to_dollars(cents or 0):,.2f}"


async def _decided_estimates(user) -> list:
    return await Scoped(db.estimates, user["tenant_id"]).find_list(
        {"status": {"$in": list(DECIDED)}}, limit=2000)


def _quartiles(values: list) -> list:
    s = sorted(values)
    n = len(s)
    if n < 4:
        return []
    return [s[n // 4], s[n // 2], s[3 * n // 4]]


@router.get("/history")
async def pricing_history(user: dict = Depends(require("ledger:read"))):
    ests = await _decided_estimates(user)
    totals = [e["total_cents"] for e in ests if e.get("total_cents")]
    won = [e for e in ests if e["status"] in WON]
    if not totals:
        return {"samples": 0}
    qs = _quartiles(totals)
    bands = []
    if qs:
        edges = [0] + qs + [max(totals) + 1]
        for i in range(4):
            lo, hi = edges[i], edges[i + 1]
            in_band = [e for e in ests if lo <= e.get("total_cents", 0) < hi]
            w = sum(1 for e in in_band if e["status"] in WON)
            bands.append({"low_cents": lo, "high_cents": hi,
                          "win_rate": round(w / len(in_band) * 100) if in_band else 0,
                          "samples": len(in_band)})
    won_totals = sorted(e["total_cents"] for e in won if e.get("total_cents"))
    return {
        "samples": len(ests),
        "win_rate_overall": round(len(won) / len(ests) * 100, 1),
        "median_won_cents": won_totals[len(won_totals) // 2] if won_totals else 0,
        "avg_won_cents": int(sum(won_totals) / len(won_totals)) if won_totals else 0,
        "bands": bands,
    }


class GuideBody(BaseModel):
    description: str = ""
    estimated_cost_cents: int = 0
    target_margin_pct: float = 40.0


@router.post("/guide")
async def price_guide(body: GuideBody, user: dict = Depends(require("ledger:read"))):
    ests = await _decided_estimates(user)
    hist = await pricing_history(user)

    # cost-plus anchor
    margin = max(5.0, min(body.target_margin_pct, 90.0))
    cost_based = int(round(body.estimated_cost_cents / (1 - margin / 100))) \
        if body.estimated_cost_cents else 0

    # win-rate-aware suggestion: price at the band boundary with the best win rate
    suggested = cost_based or hist.get("median_won_cents", 0)
    win_rate_at = hist.get("win_rate_overall", 0)
    band_note = ""
    if hist.get("bands"):
        best = max(hist["bands"], key=lambda b: (b["win_rate"], -b["samples"]))
        if best["samples"] >= 3:
            win_rate_at = best["win_rate"]
            band_note = (f"Jobs priced {_money(best['low_cents'])}–{_money(best['high_cents'])} "
                         f"won {best['win_rate']}% of the time ({best['samples']} quotes).")
            if not cost_based:
                suggested = (best["low_cents"] + best["high_cents"]) // 2

    low = int(suggested * 0.9)
    high = int(suggested * 1.15)
    confidence = "high" if hist.get("samples", 0) >= 20 else \
                 "medium" if hist.get("samples", 0) >= 8 else "low"

    notes = []
    if body.estimated_cost_cents:
        notes.append(f"Cost-plus at {margin:.0f}% margin: {_money(cost_based)}.")
    if band_note:
        notes.append(band_note)
    if hist.get("samples", 0) < 8:
        notes.append("Limited history — guidance improves as you quote more jobs.")
    if not body.estimated_cost_cents:
        notes.append("Add the job's estimated cost for a sharper, margin-based suggestion.")

    return {
        "suggested_cents": suggested,
        "range_low_cents": low, "range_high_cents": high,
        "win_rate_at_price": win_rate_at,
        "samples": hist.get("samples", 0),
        "confidence": confidence,
        "median_won_cents": hist.get("median_won_cents", 0),
        "notes": notes,
    }
