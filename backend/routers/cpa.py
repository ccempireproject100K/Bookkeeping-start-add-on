"""One-click CPA package: everything a tax preparer needs, in one ZIP.

Includes P&L, balance sheet, trial balance, general ledger detail, chart of
accounts, expenses, invoices, mileage log, and the 1099 contractor summary —
plus a README that tells the preparer what's inside.
"""
import csv
import io
import zipfile
from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, today_str, cents_to_dollars
from lib import ledger as L
from lib.coa import NAME_OF

router = APIRouter(prefix="/cpa", tags=["cpa"])


def _csv_str(header, rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _money(cents) -> str:
    return f"{cents_to_dollars(cents or 0):.2f}"


@router.get("/package.zip")
async def cpa_package(year: str = Query("2026"),
                      user: dict = Depends(require("ledger:read"))):
    tenant_id = user["tenant_id"]
    start, end = f"{year}-01-01", f"{year}-12-31"
    tenant = await db.tenants.find_one({"id": tenant_id}, {"_id": 0}) or {}
    files = {}

    # P&L
    p = await L.pnl(db, tenant_id, start, end)
    rows = [["REVENUE", "", ""]]
    rows += [[r["code"], r["name"], _money(r["amount"])] for r in p["revenue"]]
    rows += [["Total Revenue", "", _money(p["revenue_total"])], ["EXPENSES", "", ""]]
    rows += [[r["code"], r["name"], _money(r["amount"])] for r in p["expense"]]
    rows += [["Total Expenses", "", _money(p["expense_total"])],
             ["Net Income", "", _money(p["net_income"])]]
    files["profit_and_loss.csv"] = _csv_str(["Code", "Account", "Amount"], rows)

    # Balance sheet
    bs = await L.balance_sheet(db, tenant_id, end)
    rows = []
    for sec in ("assets", "liabilities", "equity"):
        rows.append([sec.upper(), "", ""])
        rows += [[r["code"], r["name"], _money(r["amount"])] for r in bs[sec]]
    files["balance_sheet.csv"] = _csv_str(["Code", "Account", "Amount"], rows)

    # Trial balance
    tb = await L.trial_balance(db, tenant_id, end)
    rows = [[r["code"], r["name"], _money(r["debit"]), _money(r["credit"])]
            for r in tb]
    files["trial_balance.csv"] = _csv_str(["Code", "Account", "Debit", "Credit"], rows)

    # General ledger detail
    entries = await Scoped(db.journal_entries, tenant_id).find_list(
        {"status": "posted", "entry_date": {"$gte": start, "$lte": end}},
        sort=[("entry_date", 1)], limit=10000)
    rows = []
    for e in entries:
        for ln in e.get("lines", []):
            rows.append([e.get("entry_date"), e.get("number"), e.get("memo", ""),
                         ln.get("account_code"), NAME_OF.get(ln.get("account_code"), ""),
                         _money(ln.get("debit_cents")), _money(ln.get("credit_cents"))])
    files["general_ledger.csv"] = _csv_str(
        ["Date", "Entry#", "Memo", "Code", "Account", "Debit", "Credit"], rows)

    # Chart of accounts
    accts = await Scoped(db.accounts, tenant_id).find_list(sort=[("code", 1)], limit=2000)
    files["chart_of_accounts.csv"] = _csv_str(
        ["Code", "Name", "Type"],
        [[a.get("code"), a.get("name"), a.get("type")] for a in accts])

    # Expenses
    exps = await Scoped(db.expenses, tenant_id).find_list(
        {"expense_date": {"$gte": start, "$lte": end}}, sort=[("expense_date", 1)], limit=5000)
    files["expenses.csv"] = _csv_str(
        ["Date", "Vendor", "Category", "Amount", "Payment", "Description"],
        [[e.get("expense_date"), e.get("vendor"), e.get("category_name"),
          _money(e.get("amount_cents")), e.get("payment_method"),
          e.get("description")] for e in exps])

    # Invoices
    invs = await Scoped(db.invoices, tenant_id).find_list(
        {"issue_date": {"$gte": start, "$lte": end}}, sort=[("issue_date", 1)], limit=5000)
    files["invoices.csv"] = _csv_str(
        ["Issue Date", "Client", "Total", "Paid", "Balance", "Status"],
        [[i.get("issue_date"), i.get("client_name"), _money(i.get("total_cents")),
          _money(i.get("paid_cents")),
          _money(i.get("total_cents", 0) - i.get("paid_cents", 0)),
          i.get("status")] for i in invs])

    # Mileage log
    trips = await Scoped(db.mileage_trips, tenant_id).find_list(
        {"trip_date": {"$gte": start, "$lte": end}}, sort=[("trip_date", 1)], limit=5000)
    files["mileage_log.csv"] = _csv_str(
        ["Date", "Origin", "Destination", "Purpose", "Miles", "Rate", "Deductible"],
        [[t.get("trip_date"), t.get("origin"), t.get("destination"), t.get("purpose"),
          t.get("miles"), t.get("rate"), t.get("deductible")] for t in trips])

    # 1099 contractors
    subs = await Scoped(db.vendors, tenant_id).find_list({"is_1099": True}, sort=[("name", 1)], limit=1000)
    pay_totals = {}
    pays = await Scoped(db.contractor_payments, tenant_id).find_list(
        {"pay_date": {"$gte": start, "$lte": end}}, limit=10000)
    for py in pays:
        pay_totals[py.get("vendor_id")] = pay_totals.get(py.get("vendor_id"), 0) + (py.get("amount_cents") or 0)
    files["contractors_1099.csv"] = _csv_str(
        ["Name", "Tax ID on file", "W-9 on file", f"{year} Paid", "Needs 1099 (>= $600)"],
        [[s.get("name"), "yes" if (s.get("tax_id") or s.get("tax_id_enc")
                                   or s.get("tax_id_last4")) else "no",
          "yes" if (s.get("compliance") or {}).get("w9_received") else "no",
          _money(pay_totals.get(s["id"], 0)),
          "yes" if pay_totals.get(s["id"], 0) >= 60000 else "no"] for s in subs])

    # README
    files["README.txt"] = (
        f"CPA TAX PACKAGE — {year}\n"
        f"Workspace: {tenant.get('name', tenant_id)}\n"
        f"Generated: {today_str()} (all amounts in USD)\n\n"
        "CONTENTS\n"
        "- profit_and_loss.csv — revenue & expenses for the year\n"
        "- balance_sheet.csv — as of Dec 31\n"
        "- trial_balance.csv — as of Dec 31 (debits = credits check)\n"
        "- general_ledger.csv — every posted journal line, chronological\n"
        "- chart_of_accounts.csv — account codes and names\n"
        "- expenses.csv — vendor expenses with categories\n"
        "- invoices.csv — issued invoices with paid/balance\n"
        "- mileage_log.csv — business miles with IRS rates applied\n"
        "- contractors_1099.csv — 1099-eligible vendors, W-9 status, amounts paid\n\n"
        "Questions about any figure: every line traces to a journal entry number\n"
        "in general_ledger.csv.\n")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    buf.seek(0)
    await audit(user, "cpa.package", "package", year, {"files": len(files)})
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=cpa-package-{year}.zip"})


@router.get("/checklist")
async def cpa_checklist(year: str = Query("2026"),
                        user: dict = Depends(require("ledger:read"))):
    """What's ready and what's missing before handing to the preparer."""
    tenant_id = user["tenant_id"]
    start, end = f"{year}-01-01", f"{year}-12-31"
    items = []

    unposted = await Scoped(db.journal_entries, tenant_id).find_list(
        {"status": "draft", "entry_date": {"$gte": start, "$lte": end}}, limit=5)
    items.append({"label": "All journal entries posted",
                  "ok": not unposted, "detail": f"{len(unposted)} drafts" if unposted else "clean"})

    tb = await L.trial_balance(db, tenant_id, end)
    dr = sum(r["debit"] for r in tb)
    cr = sum(r["credit"] for r in tb)
    items.append({"label": "Trial balance nets to zero",
                  "ok": dr == cr, "detail": f"dr {dr} / cr {cr}"})

    subs = await Scoped(db.vendors, tenant_id).find_list({"is_1099": True}, limit=1000)
    missing_w9 = [s for s in subs if not (s.get("compliance") or {}).get("w9_received")]
    items.append({"label": "W-9s collected for all 1099 subs",
                  "ok": not missing_w9,
                  "detail": f"{len(missing_w9)} missing" if missing_w9 else f"{len(subs)} on file"})

    unrec = await Scoped(db.bank_transactions, tenant_id).find_list(
        {"reconciled": {"$ne": True}}, limit=1000)
    items.append({"label": "Bank feed reconciled",
                  "ok": len(unrec) == 0, "detail": f"{len(unrec)} unreconciled" if unrec else "clean"})

    return {"year": year, "ready": all(i["ok"] for i in items), "items": items}
