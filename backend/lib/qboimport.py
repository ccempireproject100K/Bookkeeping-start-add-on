"""QuickBooks Online export mapper engine (Top-20 gap #12 — move-in).

Pure functions, no DB: parse QBO's documented CSV export shapes, detect
which export a file is, map rows into Books shapes, and produce a dry-run
report (counts, unmapped rows with reasons, an adjustable account map, and
a trial-balance check). The router (routers/migration.py) owns persistence.

Export shapes handled (QBO export-to-CSV headers, variants accepted):
  - Chart of Accounts:  Account, Type, Detail Type, Description, Balance
  - Customer list:      Customer, Company, Email, Phone, ..., Open Balance
  - Vendor list:        Vendor, Company, Email, Phone, ..., Open Balance
  - Products/Services:  Product/Service, SKU, Type, Sales Price, Income Account
  - Transactions:       Date, Transaction Type, Num, Name, Memo/Description,
                        Account, Split, Debit, Credit, Balance
                        (Transaction List by Account / General Ledger exports)

Money is integer cents everywhere (lib.base.to_cents). Every mapped record
carries a stable source fingerprint so re-running the same exports into the
same tenant never duplicates.
"""
import csv
import hashlib
import io
import json
from datetime import datetime

from lib.base import to_cents

# --------------------------------------------------------------------------
# CSV plumbing
# --------------------------------------------------------------------------

def _norm_header(h):
    return (h or "").strip().lower()


def parse_csv(text):
    """Return (headers_normalized, rows) where rows are dicts keyed by the
    ORIGINAL header names stripped, plus a normalized lookup per row."""
    if text is None:
        return [], []
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    raw = [r for r in reader if any((c or "").strip() for c in r)]
    if not raw:
        return [], []
    headers = [(h or "").strip() for h in raw[0]]
    rows = []
    for r in raw[1:]:
        row = {}
        for i, h in enumerate(headers):
            row[h] = (r[i] if i < len(r) else "").strip()
        rows.append(row)
    return headers, rows


def _get(row, *names):
    """Case-insensitive multi-name cell lookup."""
    lowered = {k.strip().lower(): v for k, v in row.items()}
    for n in names:
        v = lowered.get(n.strip().lower())
        if v not in (None, ""):
            return v
    return ""


def fingerprint(kind, payload):
    canon = json.dumps({"kind": kind, "p": payload}, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# Detection — say plainly which export each file is (or that it isn't one)
# --------------------------------------------------------------------------

def detect_kind(headers):
    hs = {_norm_header(h) for h in headers}
    if not hs:
        return None, "file is empty or has no header row"
    # Transactions first: a GL / transaction-list export has dates + both sides.
    if ({"date"} & hs or {"transaction date"} & hs) and "debit" in hs and "credit" in hs:
        return "transactions", None
    if "product/service" in hs or "products/services" in hs:
        return "items", None
    if "account" in hs and ("type" in hs or "detail type" in hs):
        return "accounts", None
    if "customer" in hs:
        return "customers", None
    if "vendor" in hs:
        return "vendors", None
    return None, ("not a recognized QuickBooks export — expected a Chart of "
                  "Accounts, Customer, Vendor, Products/Services, or "
                  "Transaction/General-Ledger export")


# --------------------------------------------------------------------------
# Mappers
# --------------------------------------------------------------------------

QBO_TYPE_MAP = {
    "bank": "asset",
    "accounts receivable (a/r)": "asset", "accounts receivable": "asset",
    "other current asset": "asset", "fixed asset": "asset", "other asset": "asset",
    "accounts payable (a/p)": "liability", "accounts payable": "liability",
    "credit card": "liability", "other current liability": "liability",
    "long term liabilities": "liability", "long-term liabilities": "liability",
    "equity": "equity",
    "income": "revenue", "other income": "revenue",
    "cost of goods sold": "expense", "expenses": "expense",
    "expense": "expense", "other expense": "expense",
}

_CODE_BASE = {"asset": 1000, "liability": 2000, "equity": 3000,
              "revenue": 4000, "expense": 5000}

_STOP_TOKENS = {"a", "an", "the", "of", "cash", "account", "accounts"}


def _tokens(name):
    raw = "".join(ch if ch.isalnum() else " " for ch in (name or "").lower()).split()
    return {t for t in raw if t not in _STOP_TOKENS and len(t) > 1}


def _closest_name_match(qbo_name, existing_accounts, want_type):
    """Best token-overlap match among existing accounts of the same type.
    'Accounts Receivable (A/R)' -> 'Accounts Receivable'; 'Checking' ->
    'Cash — Bank Checking'. Returns None when nothing is genuinely close."""
    qt = _tokens(qbo_name)
    if not qt:
        return None
    best, best_score = None, 0.0
    for a in existing_accounts:
        if a.get("type") != want_type:
            continue
        at = _tokens(a.get("name"))
        if not at:
            continue
        score = len(qt & at) / max(1, min(len(qt), len(at)))
        if score > best_score:
            best, best_score = a, score
    return best if best_score >= 0.5 else None


def map_accounts(rows):
    out, problems = [], []
    for i, r in enumerate(rows):
        name = _get(r, "Account", "Account Name")
        qtype = _get(r, "Type")
        if not name:
            problems.append({"kind": "accounts", "row": i + 2,
                             "reason": "row has no account name", "data": r})
            continue
        btype = QBO_TYPE_MAP.get(qtype.strip().lower())
        if not btype:
            problems.append({"kind": "accounts", "row": i + 2,
                             "reason": f"unknown QuickBooks account type '{qtype}'",
                             "data": r})
            continue
        rec = {
            "qbo_name": name, "type": btype, "qbo_type": qtype,
            "detail_type": _get(r, "Detail Type"),
            "description": _get(r, "Description"),
        }
        rec["fingerprint"] = fingerprint("account", rec)
        out.append(rec)
    return out, problems


def _map_people(rows, kind, name_headers):
    out, problems = [], []
    for i, r in enumerate(rows):
        name = _get(r, *name_headers) or _get(r, "Company")
        if not name:
            problems.append({"kind": kind, "row": i + 2,
                             "reason": f"row has no {kind[:-1]} name", "data": r})
            continue
        rec = {
            "name": name,
            "company": _get(r, "Company"),
            "email": _get(r, "Email", "E-mail"),
            "phone": _get(r, "Phone", "Mobile"),
            "address": _get(r, "Billing Address", "Address", "Street"),
        }
        rec["fingerprint"] = fingerprint(kind, rec)
        out.append(rec)
    return out, problems


def map_customers(rows):
    return _map_people(rows, "customers", ("Customer", "Customer Name", "Display Name"))


def map_vendors(rows):
    return _map_people(rows, "vendors", ("Vendor", "Vendor Name", "Display Name"))


def map_items(rows):
    out, problems = [], []
    for i, r in enumerate(rows):
        name = _get(r, "Product/Service", "Products/Services", "Item", "Name")
        if not name:
            problems.append({"kind": "items", "row": i + 2,
                             "reason": "row has no product/service name", "data": r})
            continue
        price_raw = _get(r, "Sales Price", "Price", "Sales price/rate")
        rec = {
            "name": name,
            "sku": _get(r, "SKU"),
            "qbo_type": _get(r, "Type") or "Service",
            "price_cents": to_cents(price_raw) if price_raw else 0,
            "income_account_name": _get(r, "Income Account"),
        }
        rec["fingerprint"] = fingerprint("item", rec)
        out.append(rec)
    return out, problems


def _parse_date(raw):
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _amt(row, *names):
    v = _get(row, *names)
    if v in ("", None):
        return 0
    return to_cents(str(v).replace(",", "").replace("$", ""))


def map_transactions(rows):
    """Group line rows into journal entries by (date, type, num)."""
    groups, problems = {}, []
    for i, r in enumerate(rows):
        date = _parse_date(_get(r, "Date", "Transaction Date"))
        acct = _get(r, "Account", "Account Name")
        if not date:
            problems.append({"kind": "transactions", "row": i + 2,
                             "reason": f"unreadable date '{_get(r, 'Date', 'Transaction Date')}'",
                             "data": r})
            continue
        if not acct:
            problems.append({"kind": "transactions", "row": i + 2,
                             "reason": "line has no account — cannot be mapped",
                             "data": r})
            continue
        debit, credit = _amt(r, "Debit"), _amt(r, "Credit")
        if debit == 0 and credit == 0:
            problems.append({"kind": "transactions", "row": i + 2,
                             "reason": "line has no debit or credit amount",
                             "data": r})
            continue
        key = (date, _get(r, "Transaction Type", "Type"), _get(r, "Num", "No.", "Ref No."))
        g = groups.setdefault(key, {
            "entry_date": date, "txn_type": key[1], "num": key[2],
            "name": _get(r, "Name", "Payee", "Customer", "Vendor"),
            "memo": _get(r, "Memo/Description", "Memo", "Description"),
            "lines": [],
        })
        g["lines"].append({
            "account_name": acct, "debit": debit, "credit": credit,
            "memo": _get(r, "Memo/Description", "Memo", "Description"),
        })
    entries = []
    for g in groups.values():
        g["fingerprint"] = fingerprint("entry", {
            "d": g["entry_date"], "t": g["txn_type"], "n": g["num"],
            "lines": sorted((l["account_name"], l["debit"], l["credit"]) for l in g["lines"]),
        })
        entries.append(g)
    entries.sort(key=lambda e: (e["entry_date"], e["num"]))
    return entries, problems


MAPPERS = {"accounts": map_accounts, "customers": map_customers,
           "vendors": map_vendors, "items": map_items,
           "transactions": map_transactions}


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

def build_account_map(account_recs, existing_accounts, code_universe=None):
    """Default map per imported account:
      - same-named existing Books account -> 'map' onto it;
      - else 'create' with an unused code drawn from the Books chart
        universe (the ledger only posts to codes in that universe, so a
        suggested code must come from it);
      - if the universe has no free code of that type left, 'map' onto an
        existing account of the same type and SAY SO in the row's note —
        never silently.
    `code_universe`: {code: type} of every code the ledger accepts."""
    by_name = {(a.get("name") or "").strip().lower(): a for a in existing_accounts}
    used = {a.get("code") for a in existing_accounts}
    free_by_type = {}
    for code, typ in sorted((code_universe or {}).items()):
        if code not in used:
            free_by_type.setdefault(typ, []).append(code)
    counters = dict(_CODE_BASE)
    out = []
    for rec in account_recs:
        hit = by_name.get(rec["qbo_name"].strip().lower())
        if hit:
            out.append({"qbo_name": rec["qbo_name"], "action": "map",
                        "books_code": hit["code"], "books_name": hit["name"],
                        "type": rec["type"], "note": "Name matches an existing Books account"})
            continue
        free = free_by_type.get(rec["type"]) or []
        if free:
            code = free.pop(0)
            used.add(code)
            out.append({"qbo_name": rec["qbo_name"], "action": "create",
                        "books_code": code, "books_name": rec["qbo_name"],
                        "type": rec["type"], "note": "New Books account"})
            continue
        similar = _closest_name_match(rec["qbo_name"], existing_accounts, rec["type"])
        if similar:
            out.append({"qbo_name": rec["qbo_name"], "action": "map",
                        "books_code": similar["code"], "books_name": similar["name"],
                        "type": rec["type"],
                        "note": ("No unused code in the Books chart for a new "
                                 f"{rec['type']} account — mapped to the closest "
                                 "name match; re-point it in this table before "
                                 "applying if you prefer another")})
            continue
        same_type = next((a for a in existing_accounts if a.get("type") == rec["type"]), None)
        if same_type:
            out.append({"qbo_name": rec["qbo_name"], "action": "map",
                        "books_code": same_type["code"], "books_name": same_type["name"],
                        "type": rec["type"],
                        "note": ("No unused code in the Books chart for a new "
                                 f"{rec['type']} account — mapped here; re-point "
                                 "it in this table before applying if you prefer another")})
            continue
        code = counters[rec["type"]]
        while str(code) in used:
            code += 10
        counters[rec["type"]] = code + 10
        used.add(str(code))
        out.append({"qbo_name": rec["qbo_name"], "action": "create",
                    "books_code": str(code), "books_name": rec["qbo_name"],
                    "type": rec["type"], "note": "New Books account"})
    return out


def dry_run(files, existing_accounts=None, code_universe=None):
    """files: [{filename, content}]. Returns the full preview report plus
    the mapped records (records are persisted with the run by the router)."""
    existing_accounts = existing_accounts or []
    report_files, records, problems = [], {}, []
    counts = {"accounts": 0, "customers": 0, "vendors": 0, "items": 0,
              "transaction_entries": 0, "transaction_lines": 0}
    for f in files:
        headers, rows = parse_csv(f.get("content") or "")
        kind, why = detect_kind(headers)
        if kind is None:
            report_files.append({"filename": f.get("filename"), "kind": None,
                                 "rows": len(rows), "reason": why})
            continue
        mapped, probs = MAPPERS[kind](rows)
        problems.extend(probs)
        records.setdefault(kind, []).extend(mapped)
        report_files.append({"filename": f.get("filename"), "kind": kind,
                             "rows": len(rows), "mapped": len(mapped)})
    for k in ("accounts", "customers", "vendors", "items"):
        counts[k] = len(records.get(k, []))
    counts["transaction_entries"] = len(records.get("transactions", []))
    counts["transaction_lines"] = sum(len(e["lines"]) for e in records.get("transactions", []))

    account_map = build_account_map(records.get("accounts", []), existing_accounts,
                                    code_universe=code_universe)
    mapped_names = {m["qbo_name"].strip().lower() for m in account_map}
    existing_names = {(a.get("name") or "").strip().lower() for a in existing_accounts}

    # Transaction lines naming an account that is neither imported nor an
    # existing Books account are unmapped until the user maps them.
    for e in records.get("transactions", []):
        for l in e["lines"]:
            nm = l["account_name"].strip().lower()
            if nm not in mapped_names and nm not in existing_names:
                problems.append({
                    "kind": "transactions", "row": None,
                    "reason": (f"account '{l['account_name']}' is not in the "
                               "imported chart of accounts and matches no "
                               "existing Books account — map it before applying"),
                    "data": {"entry": f"{e['entry_date']} #{e['num']}", "account": l["account_name"]},
                })

    total_debit = sum(l["debit"] for e in records.get("transactions", []) for l in e["lines"])
    total_credit = sum(l["credit"] for e in records.get("transactions", []) for l in e["lines"])
    unbalanced = []
    for e in records.get("transactions", []):
        d = sum(l["debit"] for l in e["lines"])
        c = sum(l["credit"] for l in e["lines"])
        if d != c:
            unbalanced.append({"entry": f"{e['entry_date']} {e['txn_type']} #{e['num']}",
                               "debit_cents": d, "credit_cents": c})
    report = {
        "files": report_files,
        "counts": counts,
        "account_map": account_map,
        "unmapped": problems,
        "trial_balance": {
            "total_debit_cents": total_debit, "total_credit_cents": total_credit,
            "difference_cents": total_debit - total_credit,
            "balanced": total_debit == total_credit and not unbalanced,
            "unbalanced_groups": unbalanced,
        },
    }
    return report, records


# --------------------------------------------------------------------------
# "Where did my reports go?" — QBO report -> where it lives in Books
# --------------------------------------------------------------------------

REPORTS_MAP = [
    {"qbo": "Profit and Loss", "books": "Profit & Loss (Reports → Profit & Loss)",
     "note": "Same rows, same math — it reads straight off your posted journal."},
    {"qbo": "Balance Sheet", "books": "Balance Sheet (Reports → Balance Sheet)",
     "note": "Assets, liabilities and equity as of any date you pick."},
    {"qbo": "Trial Balance", "books": "Trial Balance (Reports → Trial Balance)",
     "note": "Every account with its debit and credit totals."},
    {"qbo": "General Ledger", "books": "General Ledger (Reports → General Ledger)",
     "note": "Pick an account and see every line that touched it."},
    {"qbo": "A/R Aging Summary", "books": "Aging (Reports → Aging, Receivables tab)",
     "note": "Who owes you, bucketed 0–30 / 31–60 / 61–90 / 90+ days."},
    {"qbo": "A/P Aging Summary", "books": "Aging (Reports → Aging, Payables tab)",
     "note": "What you owe vendors, in the same age buckets."},
    {"qbo": "Expenses by Vendor", "books": "Business Reports → Expenses by Vendor",
     "note": "Your spend per vendor for any period, exportable to CSV."},
    {"qbo": "Income by Customer", "books": "Business Reports → Income by Customer",
     "note": "Revenue per customer — the mirror of expenses by vendor."},
    {"qbo": "Statement of Cash Flows", "books": "Cash Flow (Reports → Cash Flow)",
     "note": "Operating / investing / financing, built from the same ledger."},
    {"qbo": "Audit Log", "books": "Audit Log (Settings → Audit Log)",
     "note": "Who did what, when — entries here are reversed, never edited away."},
    {"qbo": "1099 Summary", "books": "Tax → Contractors / CPA Package",
     "note": "Contractor totals with the $600 flags, plus a one-click CPA zip."},
]


# --------------------------------------------------------------------------
# Bundled PRACTICE export set — synthetic, balanced, clearly fake
# --------------------------------------------------------------------------

SAMPLE_FILES = [
    {"filename": "practice-chart-of-accounts.csv", "content":
     "Account,Type,Detail Type,Description,Balance\n"
     "Checking,Bank,Checking,Main operating account,2450.00\n"
     "Accounts Receivable (A/R),Accounts receivable (A/R),Accounts Receivable,Money customers owe,\n"
     "Accounts Payable (A/P),Accounts payable (A/P),Accounts Payable,Money owed to vendors,\n"
     "Owner Equity,Equity,Owner's Equity,,\n"
     "Service Income,Income,Service Income,Money from jobs,\n"
     "Job Materials,Expenses,Supplies & Materials,Materials used on jobs,\n"},
    {"filename": "practice-customers.csv", "content":
     "Customer,Company,Email,Phone,Billing Address,Open Balance\n"
     "Practice Customer One,Practice Homes LLC,practice1@example.com,555-0101,12 Practice Lane,550.00\n"
     "Practice Customer Two,,practice2@example.com,555-0102,34 Sample Street,0.00\n"},
    {"filename": "practice-vendors.csv", "content":
     "Vendor,Company,Email,Phone,Billing Address,Open Balance\n"
     "Practice Supply Co,Practice Supply Co,orders@practice.example,555-0201,9 Depot Road,120.00\n"},
    {"filename": "practice-products-services.csv", "content":
     "Product/Service,SKU,Type,Sales Price,Income Account\n"
     "Practice Tune-Up,TU-1,Service,275.00,Service Income\n"
     "Practice Filter,FIL-1,Non-inventory,25.00,Service Income\n"},
    {"filename": "practice-transactions.csv", "content":
     "Date,Transaction Type,Num,Name,Memo/Description,Account,Split,Debit,Credit,Balance\n"
     "01/05/2026,Invoice,1001,Practice Customer One,Practice tune-up job,Accounts Receivable (A/R),,550.00,,550.00\n"
     "01/05/2026,Invoice,1001,Practice Customer One,Practice tune-up job,Service Income,,,550.00,550.00\n"
     "01/07/2026,Bill,E-2001,Practice Supply Co,Practice materials,Job Materials,,120.00,,120.00\n"
     "01/07/2026,Bill,E-2001,Practice Supply Co,Practice materials,Accounts Payable (A/P),,,120.00,120.00\n"
     "01/12/2026,Payment,,Practice Customer One,Payment on invoice 1001,Checking,,550.00,,550.00\n"
     "01/12/2026,Payment,,Practice Customer One,Payment on invoice 1001,Accounts Receivable (A/R),,,550.00,550.00\n"},
]
