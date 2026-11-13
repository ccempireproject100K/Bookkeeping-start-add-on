"""Coverage statement — what share of our contractors actually opted in.

Owner's words (2026-11-11): "we need to be able to tell people what
percentage of our contractors we actually got data from."

This builds a PRESENTABLE coverage report per scope: one row per
trade x metro cell, plus rollups by trade, by metro, and overall. Each
cell carries presented count, opted-in count, and the opt-in rate %. The
report states its consent method on its face:

    "active opt-in, unchecked by default"

That sentence is the product: a pool a buyer can trust because every
member in it actively said yes.

PRIVACY — counts and rates ONLY:

- No tenant names, no tenant ids, no per-business rows anywhere in the
  payload. The report is built purely from aggregates.
- A cell whose presented count is below the reporting floor (the
  existing suppression floor, ``MIN_CELL_BUSINESSES`` in
  consent/suppression.py — the numbers there are unchanged) is shown as
  "below reporting floor" instead of an exact count, for the presented
  count, the opted-in count, and the rate alike.
- Account-deleted tenants are excluded, matching cohort reporting, so
  the statement describes the pool as it stands now.
- Rollups (by trade, by metro, overall) aggregate REPORTABLE cells
  only, and the payload says so. Summing a below-floor cell into a
  published total would let a reader difference it back out
  (total minus the published cells), which is the differencing leak the
  risk review exists to stop.
- "Opted in" means a CURRENT opt-in under the scope's clause version in
  force, exactly as in consent/cohorts.py.
"""

from __future__ import annotations

from collections import defaultdict

from .clause import CONSENT_SCOPES, SCOPE_MARKETING_IMPROVEMENT
from .store import ConsentStore, STATE_ACCOUNT_DELETED, STATE_OPTED_IN
from .suppression import MIN_CELL_BUSINESSES

CONSENT_METHOD_STATEMENT = "active opt-in, unchecked by default"
BELOW_FLOOR_LABEL = "below reporting floor"


def _cell(presented: int, opted_in: int) -> dict:
    """One coverage cell, floor-suppressed when below the floor."""
    if presented < MIN_CELL_BUSINESSES:
        return {
            "presented": BELOW_FLOOR_LABEL,
            "opted_in": BELOW_FLOOR_LABEL,
            "opt_in_rate_pct": BELOW_FLOOR_LABEL,
            "suppressed": True,
        }
    return {
        "presented": presented,
        "opted_in": opted_in,
        "opt_in_rate_pct": round(100.0 * opted_in / presented, 1) if presented else 0.0,
        "suppressed": False,
    }


def coverage_report(store: ConsentStore,
                    scope: str = SCOPE_MARKETING_IMPROVEMENT) -> dict:
    """Aggregate-only coverage payload for one scope."""
    if scope not in CONSENT_SCOPES:
        raise ValueError(f"unknown consent scope {scope!r}")
    current_version = store.current_clause_version(scope)
    tenants = {r.tenant_id: r for r in store.all_records()}

    cells: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for rec in store.all_scope_records(scope):
        if rec.state == STATE_ACCOUNT_DELETED:
            continue
        tenant = tenants.get(rec.tenant_id)
        if tenant is None:
            continue
        key = (tenant.trade, tenant.metro)
        cells[key][0] += 1
        if rec.state == STATE_OPTED_IN and rec.clause_version == current_version:
            cells[key][1] += 1

    rows = []
    for (trade, metro), (presented, opted_in) in sorted(cells.items()):
        row = {"trade": trade, "metro": metro}
        row.update(_cell(presented, opted_in))
        rows.append(row)

    # Rollups aggregate reportable cells only (see module docstring):
    # a below-floor cell summed into a total could be differenced back
    # out. The payload states how many cells were excluded, as a count
    # of cells — never a count of businesses.
    reportable = {
        key: v for key, v in cells.items() if v[0] >= MIN_CELL_BUSINESSES
    }
    excluded_cells = len(cells) - len(reportable)

    def rollup(index: int, label: str) -> list[dict]:
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for (trade, metro), (presented, opted_in) in reportable.items():
            key = (trade, metro)[index]
            agg[key][0] += presented
            agg[key][1] += opted_in
        out = []
        for key, (presented, opted_in) in sorted(agg.items()):
            entry = {label: key}
            entry.update(_cell(presented, opted_in))
            out.append(entry)
        return out

    total_presented = sum(v[0] for v in reportable.values())
    total_opted_in = sum(v[1] for v in reportable.values())
    overall = _cell(total_presented, total_opted_in)

    return {
        "scope": scope,
        "consent_method": CONSENT_METHOD_STATEMENT,
        "reporting_floor": MIN_CELL_BUSINESSES,
        "floor_label": BELOW_FLOOR_LABEL,
        "rollup_basis": "reportable cells only; below-floor cells are excluded from rollups",
        "excluded_below_floor_cells": excluded_cells,
        "rows": rows,
        "rollups": {
            "by_trade": rollup(0, "trade"),
            "by_metro": rollup(1, "metro"),
            "overall": overall,
        },
    }


def coverage_reports_by_scope(store: ConsentStore) -> dict:
    return {scope: coverage_report(store, scope=scope) for scope in CONSENT_SCOPES}
