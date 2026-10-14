"""Chart of Accounts seed (barbershop-specific) + fiscal period helpers."""
from lib.base import Scoped, new_id, now_iso

# (code, name, type, normal_balance, is_system)
# normal_balance overrides for contra accounts are explicit.
COA = [
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
    ("4000", "Service Revenue — Haircuts", "revenue", "credit"),
    ("4010", "Service Revenue — Beard", "revenue", "credit"),
    ("4020", "Service Revenue — Other", "revenue", "credit"),
    ("4100", "Product Revenue — Retail", "revenue", "credit"),
    ("4200", "Chair Rental Income", "revenue", "credit"),
    ("4300", "Contract Revenue — HVAC", "revenue", "credit"),
    ("4900", "Discounts & Promos", "revenue", "debit"),    # contra-revenue
    ("5000", "Job Cost — Materials", "expense", "debit"),
    ("5005", "Job Cost — Labor", "expense", "debit"),
    ("5006", "Job Cost — Equipment", "expense", "debit"),
    ("5007", "Job Cost — Subcontractors", "expense", "debit"),
    ("5008", "Job Cost — Other", "expense", "debit"),
    ("5010", "COGS — Retail", "expense", "debit"),
    ("5020", "COGS — Supply Used", "expense", "debit"),
    ("5100", "Barber Commissions", "expense", "debit"),
    ("5200", "Chair Rent — Paid Out", "expense", "debit"),
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
]

ACCOUNT_TYPE_OF = {c[0]: c[2] for c in COA}
NORMAL_BALANCE_OF = {c[0]: c[3] for c in COA}
NAME_OF = {c[0]: c[1] for c in COA}

TYPE_ORDER = {"asset": 0, "liability": 1, "equity": 2, "revenue": 3, "expense": 4}


async def seed_coa(accounts: Scoped):
    """Idempotent: creates any missing system accounts for this tenant."""
    existing = {a["code"] for a in await accounts.find_list()}
    created = 0
    for code, name, atype, normal in COA:
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
