"""Sales-tax calculation engine — "manual-plus" (v1).

Design (parity audit 2026-10-20, deep dive D2): QuickBooks' bar is
auto-CALCULATE + liability/return PREPARATION. QBO does not file, and by
owner ruling neither do we: this module calculates and prepares; it
never submits, files, or remits anything.

The engine is deliberately tenant-data-driven: rates come from the
tenant's own configured jurisdictions (stacked state/county/city/
district components), taxability from a small seeded category matrix,
sourcing from a per-state config table. A commercial engine (Avalara /
TaxJar / Stripe Tax) can slot in later behind the same snapshot shape:
`calculate_tax()` returns everything a caller must persist so a return
can be reproduced from the snapshot, never recomputed live.

Money is integer cents; rates are percent floats (6.25 = 6.25%);
per-line, per-component rounding is half-up via Decimal — the house
money rule from the income-tax engine (PR #147).
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

ENGINE_NAME = "manual-plus"
ENGINE_VERSION = "1"

CPA_NOTE = ("Taxability categories are a seeded starting matrix, not tax "
            "advice — confirm with your CPA or state DOR before relying on "
            "them for a filing.")

# ---------------- Tax categories & taxability matrix ----------------
# Categories a line can carry. Keep the list short and honest (deep dive
# §1.4): categories map to taxability per state; the seed below encodes
# only widely-true rules and is labelled for CPA confirmation in every
# surface that shows it.
TAX_CATEGORIES = [
    "general_goods",    # tangible personal property, default retail
    "parts_materials",  # parts/materials sold or installed
    "labor_service",    # labour-only services
    "repair_service",   # repair/maintenance services
    "digital_goods",    # software, downloads, digital products
    "food_grocery",     # unprepared food / groceries
    "clothing",         # clothing & footwear
    "exempt_always",    # never taxable (e.g. prescription items in most states)
]

# Default taxability when a state has no override. Conservative for a
# seller means: goods are taxable; most services and groceries are not
# (the majority rule across sales-tax states).
CATEGORY_DEFAULTS = {
    "general_goods": True,
    "parts_materials": True,
    "labor_service": False,
    "repair_service": False,
    "digital_goods": True,
    "food_grocery": False,
    "clothing": True,
    "exempt_always": False,
}

# Per-state overrides — only rules that are broad and well documented.
# HI (GET), NM (GRT) and SD tax services far more broadly than the
# default. Everything else rides the defaults until a CPA-reviewed seed
# update lands; overrides are data, versioned with the file.
STATE_CATEGORY_OVERRIDES = {
    "HI": {"labor_service": True, "repair_service": True},
    "NM": {"labor_service": True, "repair_service": True},
    "SD": {"labor_service": True, "repair_service": True},
}


def category_taxable(state: str, category: str) -> bool:
    """Seed-matrix taxability for (state, category). Unknown categories
    fall back to the general-goods rule rather than silently exempting."""
    cat = category if category in CATEGORY_DEFAULTS else "general_goods"
    overrides = STATE_CATEGORY_OVERRIDES.get((state or "").upper(), {})
    if cat in overrides:
        return bool(overrides[cat])
    return bool(CATEGORY_DEFAULTS[cat])


# ---------------- Sourcing ----------------
# States where local sales tax is (largely) origin-based for
# intrastate sales — the seller's place of business, not the delivery
# address. All other sales-tax states default to destination. A tenant
# jurisdiction may override per-jurisdiction.
ORIGIN_STATES = {"AZ", "IL", "MS", "MO", "NM", "OH", "PA", "TN", "TX", "UT", "VA"}


def sourcing_for_state(state: str) -> str:
    return "origin" if (state or "").upper() in ORIGIN_STATES else "destination"


# ---------------- Nexus threshold config (versioned data) ----------------
# Economic-nexus thresholds are moving law — this table is config with
# a verified-as-of date, exactly like the income-tax engine's state
# tables, and the monitor SURFACES warnings only: it never presents a
# nexus determination as fact (deep dive §1.3).
NEXUS_CONFIG_VERSION = "2026.1"
NEXUS_VERIFIED_AS_OF = "2026-10-20"
NEXUS_SOURCE = ("State DOR economic-nexus pages as surveyed 2026-10-20 "
                "(parity audit D2 §1.3). Re-verify quarterly.")

# Modal rule: $100k of sales into the state, no transaction-count test
# (the count test is being repealed state by state; where one survives
# and is modelled, it is listed in the overrides).
_NEXUS_DEFAULT = {
    "sales_threshold_cents": 100_000 * 100,
    "transaction_threshold": None,
    "transaction_test_mode": "or",   # sales OR transactions (where a test exists)
    "basis": "gross sales into the state, current or prior calendar year",
}
NEXUS_STATE_OVERRIDES = {
    "CA": {"sales_threshold_cents": 500_000 * 100, "transaction_threshold": None},
    "TX": {"sales_threshold_cents": 500_000 * 100, "transaction_threshold": None},
    # NY requires BOTH prongs: > $500k AND > 100 transactions.
    "NY": {"sales_threshold_cents": 500_000 * 100, "transaction_threshold": 100,
           "transaction_test_mode": "and"},
}


def nexus_threshold_for(state: str) -> dict:
    cfg = dict(_NEXUS_DEFAULT)
    cfg.update(NEXUS_STATE_OVERRIDES.get((state or "").upper(), {}))
    return cfg


# ---------------- Rate history ----------------
def combined_rate_pct(components) -> float:
    return round(sum(float(c.get("rate_pct", 0)) for c in (components or [])), 4)


def resolve_components_as_of(jurisdiction: dict, on_date: str):
    """Pick the component set in force on `on_date` from a jurisdiction's
    rate history. Legacy jurisdictions (no history) synthesise a single
    'combined' component from their flat rate_pct. Returns
    (components, effective_from)."""
    history = jurisdiction.get("rate_history") or []
    if history:
        ordered = sorted(history, key=lambda h: h.get("effective_from", ""))
        chosen = None
        for entry in ordered:
            if entry.get("effective_from", "") <= on_date:
                chosen = entry
            else:
                break
        if chosen is None:
            chosen = ordered[0]
        return (chosen.get("components") or [], chosen.get("effective_from"))
    components = jurisdiction.get("components")
    if not components:
        components = [{
            "level": "combined",
            "name": jurisdiction.get("name", jurisdiction.get("state", "")),
            "rate_pct": jurisdiction.get("rate_pct", 0),
        }]
    return components, (jurisdiction.get("created_at") or "")[:10] or "0001-01-01"


# ---------------- Calculation ----------------
def _line_component_tax(base_cents: int, rate_pct: float) -> int:
    """Half-up cents for one component on one line."""
    if base_cents <= 0 or not rate_pct:
        return 0
    raw = (Decimal(base_cents) * Decimal(str(rate_pct))) / Decimal(100)
    return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_tax(*, lines, components, state, customer_exempt=False):
    """Calculate stacked-component tax for a set of lines.

    lines: [{amount_cents, tax_category, taxable (None = use matrix)}]
    components: [{level, name, rate_pct}] — stacked additively.
    Returns per-line detail + totals; the caller persists the whole
    result as the invoice's calculation snapshot.
    """
    out_lines = []
    subtotal = taxable_sales = nontaxable_sales = exempt_sales = total_tax = 0
    for i, ln in enumerate(lines):
        amount = int(ln.get("amount_cents", 0) or 0)
        category = ln.get("tax_category") or "general_goods"
        subtotal += amount
        if customer_exempt:
            taxable, reason = False, "customer_exempt"
        elif ln.get("taxable") is not None:
            taxable, reason = bool(ln["taxable"]), "line_override"
        else:
            taxable = category_taxable(state, category)
            reason = "matrix"
        base = amount if taxable else 0
        comp_detail = []
        line_tax = 0
        if taxable:
            for comp in components or []:
                t = _line_component_tax(base, float(comp.get("rate_pct", 0)))
                line_tax += t
                comp_detail.append({
                    "level": comp.get("level", "combined"),
                    "name": comp.get("name", ""),
                    "rate_pct": comp.get("rate_pct", 0),
                    "tax_cents": t,
                })
            taxable_sales += amount
        elif customer_exempt:
            exempt_sales += amount
        else:
            nontaxable_sales += amount
        total_tax += line_tax
        out_lines.append({
            "index": i,
            "name": ln.get("name", ""),
            "tax_category": category,
            "taxable": taxable,
            "taxability_reason": reason,
            "amount_cents": amount,
            "taxable_base_cents": base,
            "tax_cents": line_tax,
            "components": comp_detail,
        })
    return {
        "lines": out_lines,
        "subtotal_cents": subtotal,
        "taxable_sales_cents": taxable_sales,
        "nontaxable_sales_cents": nontaxable_sales,
        "exempt_sales_cents": exempt_sales,
        "tax_cents": total_tax,
        "combined_rate_pct": combined_rate_pct(components),
    }


# ---------------- Filing periods ----------------
def period_bounds(year: int, month: int = None, quarter: int = None):
    """(start_date, end_date_exclusive, period_key) for a filing period.
    Dates are ISO strings; string comparison is chronological."""
    if month:
        start = date(year, month, 1)
        end = date(year + (1 if month == 12 else 0), (month % 12) + 1, 1)
        return start.isoformat(), end.isoformat(), f"{year:04d}-{month:02d}"
    if quarter:
        first_month = 3 * (quarter - 1) + 1
        start = date(year, first_month, 1)
        end_month = first_month + 3
        end = date(year + (1 if end_month > 12 else 0),
                   ((end_month - 1) % 12) + 1, 1)
        return start.isoformat(), end.isoformat(), f"{year:04d}-Q{quarter}"
    return f"{year:04d}-01-01", f"{year + 1:04d}-01-01", f"{year:04d}"


def period_key_for_date(date_str: str, frequency: str) -> str:
    """The filing-period key a date belongs to under monthly / quarterly /
    annual filing."""
    y, m = int(date_str[:4]), int(date_str[5:7])
    if frequency == "quarterly":
        return f"{y:04d}-Q{(m - 1) // 3 + 1}"
    if frequency == "annual":
        return f"{y:04d}"
    return f"{y:04d}-{m:02d}"
