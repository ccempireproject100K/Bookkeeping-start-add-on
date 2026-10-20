"""Chart of Accounts seed: shared base + niche-pack trade lines.

The base covers assets, liabilities, equity, and generic expenses/revenue
adjustments. Each niche pack contributes its own revenue accounts and
trade-specific expense accounts (see backend/niches/*.yaml), so a new
workspace opens with a chart of accounts that speaks its trade.
"""
from lib.base import Scoped, new_id, now_iso

# (code, name, type, normal_balance, is_system)
# Shared by every niche. Niche packs add revenue + trade expense lines.
BASE_COA = [
    ("1010", "Cash — Register", "asset", "debit"),
    ("1020", "Cash — Bank Checking", "asset", "debit"),
    ("1021", "Cash — Bank Savings", "asset", "debit"),
    ("1022", "Cash — Bank #2", "asset", "debit"),
    ("1023", "Cash — Bank #3", "asset", "debit"),
    ("1050", "Stripe Clearing", "asset", "debit"),
    ("1060", "Undeposited Funds", "asset", "debit"),
    ("1200", "Accounts Receivable", "asset", "debit"),
    ("1300", "Inventory — Retail", "asset", "debit"),
    ("1310", "Inventory — Supply", "asset", "debit"),
    ("1500", "Prepaid Expenses", "asset", "debit"),
    ("1800", "Furniture & Equipment", "asset", "debit"),
    ("1850", "Accumulated Depreciation", "asset", "credit"),  # contra-asset
    ("2010", "Accounts Payable", "liability", "credit"),
    ("2100", "Sales Tax Payable", "liability", "credit"),
    ("2120", "Payroll Liabilities", "liability", "credit"),
    ("2200", "Unearned Revenue — Prepaid Packages", "liability", "credit"),
    ("2210", "Customer Credits", "liability", "credit"),
    ("2300", "Tips / Commissions Payable", "liability", "credit"),
    ("2320", "Wages Payable", "liability", "credit"),
    ("2330", "Customer Deposits", "liability", "credit"),
    ("2050", "Credit Card #1", "liability", "credit"),
    ("2051", "Credit Card #2", "liability", "credit"),
    ("2052", "Credit Card #3", "liability", "credit"),
    ("2500", "Loan Payable", "liability", "credit"),
    ("3010", "Owner's Equity", "equity", "credit"),
    ("3020", "Owner's Draw", "equity", "debit"),           # contra-equity
    ("3030", "Owner's Contribution", "equity", "credit"),
    ("3100", "Retained Earnings", "equity", "credit"),
    ("4900", "Discounts & Promos", "revenue", "debit"),    # contra-revenue
    ("4090", "Late Fee Income", "revenue", "credit"),
    ("5000", "Job Cost — Materials", "expense", "debit"),
    ("5005", "Job Cost — Labor", "expense", "debit"),
    ("5006", "Job Cost — Equipment", "expense", "debit"),
    ("5007", "Job Cost — Subcontractors", "expense", "debit"),
    ("5008", "Job Cost — Other", "expense", "debit"),
    ("5010", "COGS — Retail", "expense", "debit"),
    ("5020", "COGS — Supply Used", "expense", "debit"),
    ("5300", "Shop Rent", "expense", "debit"),
    ("5400", "Utilities", "expense", "debit"),
    ("5500", "Merchant Fees — Card", "expense", "debit"),
    ("5590", "Cash Over / Short", "expense", "debit"),
    ("5600", "Shrinkage / Count Correction", "expense", "debit"),
    ("5700", "Supplies & Office", "expense", "debit"),
    ("5800", "Marketing", "expense", "debit"),
    ("5900", "Depreciation Expense", "expense", "debit"),
    ("5950", "Interest Expense", "expense", "debit"),
    ("5990", "Other / Miscellaneous", "expense", "debit"),
    ("6110", "Wages & Salaries", "expense", "debit"),
]

# Backwards-compatible alias for anything still importing COA directly.
def full_coa(niche_id: str | None = None):
    """Base chart + the niche pack's revenue and trade-expense lines."""
    from lib.niches import get_pack
    pack = get_pack(niche_id)
    rows = list(BASE_COA)
    for a in pack.get("revenue_accounts", []):
        rows.append((a["code"], a["name"], "revenue", "credit"))
    for a in pack.get("trade_expense_accounts", []):
        rows.append((a["code"], a["name"], "expense", "debit"))
    return rows


# Backwards-compatible alias for anything still importing COA directly.
def __getattr__(name):
    if name == "COA":
        return full_coa("barbershop")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _account_type_map():
    """Type lookup covering the base chart AND every niche pack's lines,
    so pack revenue codes (e.g. an HVAC pack's 4000) validate everywhere
    the legacy chart did."""
    from lib.niches import all_packs
    m = {c[0]: c[2] for c in BASE_COA}
    for p in all_packs():
        for a in p.get("revenue_accounts", []):
            m.setdefault(a["code"], "revenue")
        for a in p.get("trade_expense_accounts", []):
            m.setdefault(a["code"], "expense")
    return m


def _normal_balance_map():
    """Normal-balance lookup covering the base chart AND every niche
    pack's lines — the same universe as _account_type_map(). Pack
    revenue codes are credit-normal and pack trade-expense codes are
    debit-normal (exactly as full_coa seeds them); building this map
    from BASE_COA alone left every pack code out, so report math in
    lib/ledger.py defaulted pack revenue to debit-normal and negated
    it on the P&L and balance sheet. Base entries are inserted first
    and win on any code collision."""
    from lib.niches import all_packs
    m = {c[0]: c[3] for c in BASE_COA}
    for p in all_packs():
        for a in p.get("revenue_accounts", []):
            m.setdefault(a["code"], "credit")
        for a in p.get("trade_expense_accounts", []):
            m.setdefault(a["code"], "debit")
    return m


ACCOUNT_TYPE_OF = _account_type_map()
NORMAL_BALANCE_OF = _normal_balance_map()
NAME_OF = {c[0]: c[1] for c in BASE_COA}

# Foreign-exchange accounts (multi-currency Phase 3). NOT part of BASE_COA:
# they are created per workspace on first use via ensure_fx_accounts(),
# so existing charts are untouched until a workspace actually needs them.
FX_GAIN_CODE = "4080"
FX_GAIN_NAME = "Foreign Exchange Gain"
FX_LOSS_CODE = "5960"
FX_LOSS_NAME = "Foreign Exchange Loss"
FX_ACCOUNTS = [
    (FX_GAIN_CODE, FX_GAIN_NAME, "revenue", "credit"),
    (FX_LOSS_CODE, FX_LOSS_NAME, "expense", "debit"),
]

for _code, _name, _type, _normal in FX_ACCOUNTS:
    ACCOUNT_TYPE_OF.setdefault(_code, _type)
    NORMAL_BALANCE_OF.setdefault(_code, _normal)
    NAME_OF.setdefault(_code, _name)
del _code, _name, _type, _normal

TYPE_ORDER = {"asset": 0, "liability": 1, "equity": 2, "revenue": 3, "expense": 4}


async def seed_coa(accounts: Scoped, niche_id: str | None = None):
    """Idempotent: creates any missing system accounts for this tenant.

    The chart is the shared base plus the niche pack's revenue and
    trade-expense lines, so a new workspace opens speaking its trade.
    """
    rows = full_coa(niche_id)
    existing = {a["code"] for a in await accounts.find_list()}
    created = 0
    for code, name, atype, normal in rows:
        if code in existing:
            continue
        await accounts.insert_one({
            "id": new_id(),
            "code": code,
            "name": name,
            "type": atype,
            "normal_balance": normal,
            "parent_code": None,
            "is_system": True,
            "archived": False,
            "description": None,
            "created_at": now_iso(),
        })
        created += 1
    return created


async def ensure_fx_accounts(accounts: Scoped):
    """Idempotent: create the FX gain/loss accounts for this tenant on first use.

    Workspaces that never touch foreign currency never get these accounts,
    keeping existing charts byte-identical.
    """
    existing = {a["code"] for a in await accounts.find_list()}
    created = []
    for code, name, atype, normal in FX_ACCOUNTS:
        if code in existing:
            continue
        await accounts.insert_one({
            "id": new_id(),
            "code": code,
            "name": name,
            "type": atype,
            "normal_balance": normal,
            "parent_code": None,
            "is_system": True,
            "archived": False,
            "description": None,
            "created_at": now_iso(),
        })
        created.append(code)
    return created
