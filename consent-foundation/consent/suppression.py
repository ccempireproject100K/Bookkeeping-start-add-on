"""Re-identification cell check — the executable half of the risk review.

Before any aggregated dataset leaves Mind At Ease, every cell (one
trade x geography x attribute-band combination) must pass this check.
Cells that fail are SUPPRESSED — removed from the dataset, never rounded
into safety and never shipped with a note.

⚠️ ALL THRESHOLDS IN THIS FILE ARE PROPOSED — they are the review's
recommendations and take effect for real releases only with the owner's
stamp. The justifications live in docs/REIDENTIFICATION-RISK-REVIEW.md.

Why this exists, in one line: in a small market, "anonymized" aggregates
can identify the only roofing shop in town.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- PROPOSED thresholds (awaiting the owner's stamp) --------------------
MIN_CELL_BUSINESSES = 10
"""k-anonymity floor: a cell may publish only if at least this many distinct
businesses contribute to it. Below the floor, a cell is a roster, not a
statistic."""

DOMINANCE_MAX_SHARE = 0.50
"""No single business may account for more than this share of a cell's
published value. A cell of 12 shops where one shop is 80% of revenue is
that shop's revenue with 11 shops of camouflage."""

SMALL_MARKET_TRADE_COUNT = 25
"""A trade x metro market with fewer total businesses than this (counted
across ALL businesses in the market, not just ours) is a small market:
cells from it get manual review even when they clear the numeric floors."""

SMALL_METRO_POPULATION = 50_000
"""Below this metro population, geography itself is a strong identifier and
cells are escalated to manual review by default."""

QUASI_IDENTIFIERS = ("trade", "metro", "revenue_band", "employee_band",
                     "years_in_business_band", "plan_id")
"""The combination of these fields is what an outsider who knows a target
business would match on. Every published combination must independently
satisfy MIN_CELL_BUSINESSES."""


@dataclass
class Cell:
    trade: str
    metro: str
    metro_population: int
    market_trade_businesses: int   # all businesses of this trade in the metro
    contributing_businesses: int   # businesses contributing to this cell
    largest_contributor_share: float  # 0..1 share of the cell's value
    attribute_bands: dict = field(default_factory=dict)  # e.g. {"revenue_band": "100k-250k"}


@dataclass
class CellVerdict:
    cell: Cell
    publishable: bool
    flags: list[str]


def evaluate_cell(cell: Cell) -> CellVerdict:
    """Return SUPPRESS / MANUAL-REVIEW flags for one aggregate cell.

    Any flag at all means the cell does not ship as-is: hard flags suppress
    it outright; review flags hold it for the named human sign-off in the
    review's checklist.
    """
    flags: list[str] = []

    if cell.contributing_businesses < MIN_CELL_BUSINESSES:
        flags.append(
            f"SUPPRESS: only {cell.contributing_businesses} contributing businesses "
            f"(< proposed minimum {MIN_CELL_BUSINESSES}) — cell is a roster, not a statistic"
        )
    if cell.contributing_businesses == 1:
        flags.append(
            "SUPPRESS: single-business cell — the aggregate IS that business's data"
        )
    if cell.largest_contributor_share > DOMINANCE_MAX_SHARE:
        flags.append(
            f"SUPPRESS: one business contributes {cell.largest_contributor_share:.0%} "
            f"of the cell value (> proposed dominance cap {DOMINANCE_MAX_SHARE:.0%})"
        )
    if cell.market_trade_businesses < SMALL_MARKET_TRADE_COUNT:
        flags.append(
            f"SMALL-MARKET: only {cell.market_trade_businesses} {cell.trade} businesses "
            f"in {cell.metro} (< proposed {SMALL_MARKET_TRADE_COUNT}) — could point back "
            "to a single business; manual review required"
        )
    if cell.metro_population < SMALL_METRO_POPULATION:
        flags.append(
            f"SMALL-METRO: {cell.metro} population {cell.metro_population:,} "
            f"(< proposed {SMALL_METRO_POPULATION:,}) — geography is a strong "
            "identifier; manual review required"
        )
    if cell.contributing_businesses < MIN_CELL_BUSINESSES and cell.attribute_bands:
        bands = ", ".join(f"{k}={v}" for k, v in sorted(cell.attribute_bands.items()))
        flags.append(
            f"QUASI-IDENTIFIER: combination ({bands}) narrows below the cell floor — "
            "an outsider who knows the target can match on this combination"
        )

    return CellVerdict(cell=cell, publishable=not flags, flags=flags)


def evaluate_dataset(cells: list[Cell]) -> dict:
    verdicts = [evaluate_cell(c) for c in cells]
    return {
        "cells_total": len(verdicts),
        "cells_publishable": sum(1 for v in verdicts if v.publishable),
        "cells_flagged": sum(1 for v in verdicts if not v.publishable),
        "verdicts": verdicts,
        "dataset_publishable": all(v.publishable for v in verdicts),
    }
