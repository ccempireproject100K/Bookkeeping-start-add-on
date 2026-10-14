"""Accountant-grade reports off the double-entry journal: chart of accounts, trial balance,
balance sheet, income statement, general ledger, A/R aging, inventory valuation, sales summary.
Plus manual journal entries and reversals. Every report reconciles to the underlying journal."""

import csv
import io
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Response

from lib.audit import audit
from lib.auth import Principal, require
from lib.bookkeeping import (ACCOUNT_NAME, ACCOUNT_TYPE, CHART, ensure_accounts, post_entry, reverse_entry, _c, _f)
from lib.dates import today_iso
from lib.db import db
from lib.repo import Scoped
from models.bookkeeping import (Account, ARAging, AgingRow, BalanceSheet, BalanceSheetLine, GeneralLedger, GLRow,
                                IncomeStatement, IncomeStatementLine, InventoryValuation, InvValuationRow,
                                JournalEntryView, JournalLineView, ManualEntryIn, ReverseEntryIn, SalesSummary,
                                TrialBalance, TrialBalanceRow)

router = APIRouter(prefix="/books")


async def _entries(p: Principal, start: str = "", end: str = "", account: str = "") -> list[dict]:
    f: dict = {"tenant_id": p.tenant_id}
    if start or end:
        f["date"] = {**({"$gte": start} if start else {}), **({"$lte": end} if end else {})}
    if account:
        f["lines.account"] = account
    return await db.journal_entries.find(f, {"_id": 0}).sort([("date", 1), ("created_at", 1)]).to_list(200000)


def _acct_meta(code: str) -> tuple[str, str]:
    return ACCOUNT_NAME.get(code, code), ACCOUNT_TYPE.get(code, "expense")


def _csv(header: list[str], rows: list[list], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------- Chart of accounts ----------
@router.get("/accounts", response_model=list[Account])
async def list_accounts(p: Principal = Depends(require("report:read"))):
    await ensure_accounts(p)
    docs = await db.accounts.find({"tenant_id": p.tenant_id}, {"_id": 0}).sort("code", 1).to_list(500)
    return [Account(**d) for d in docs]


# ---------- Journal ----------
@router.get("/journal", response_model=list[JournalEntryView])
async def list_journal(start: str = "", end: str = "", account: str = "", limit: int = 500,
                       p: Principal = Depends(require("report:read"))):
    docs = await _entries(p, start, end, account)
    docs = list(reversed(docs))[:min(limit, 5000)]
    return [_view(d) for d in docs]


def _view(d: dict) -> JournalEntryView:
    lines = [JournalLineView(account=ln["account"], account_name=ln["account_name"],
                             debit=_f(ln["debit_cents"]), credit=_f(ln["credit_cents"]), memo=ln.get("memo", ""))
             for ln in d["lines"]]
    return JournalEntryView(id=d["id"], date=d["date"], memo=d["memo"], source=d["source"], ref=d["ref"], lines=lines,
                            total=_f(d["total_cents"]), reversal_of=d.get("reversal_of"), reversed_by=d.get("reversed_by"),
                            created_by_name=d.get("created_by_name"), created_at=d["created_at"])


@router.post("/journal", response_model=JournalEntryView)
async def manual_entry(body: ManualEntryIn, p: Principal = Depends(require("txn:adjust"))):
    await ensure_accounts(p)
    valid = {c for c, _, _, _ in CHART} | {d["code"] for d in await db.accounts.find({"tenant_id": p.tenant_id}, {"_id": 0, "code": 1}).to_list(500)}
    td = tc = 0
    signed: list[tuple[str, float]] = []
    for ln in body.lines:
        if ln.account not in valid:
            raise HTTPException(400, f"Unknown account {ln.account}")
        if (ln.debit > 0) == (ln.credit > 0):
            raise HTTPException(400, "Each line needs exactly one of debit or credit")
        td += _c(ln.debit)
        tc += _c(ln.credit)
        signed.append((ln.account, ln.debit - ln.credit))
    if td != tc:
        raise HTTPException(400, f"Debits ({_f(td)}) must equal credits ({_f(tc)})")
    import uuid
    ref = f"manual:{uuid.uuid4().hex}"
    entry = await post_entry(p, body.date, body.memo, signed, source="manual", ref=ref)
    await audit(p, "journal.create", "journal_entry", entry["id"], {"memo": body.memo, "total": _f(td)})
    return _view(entry)


@router.post("/journal/{entry_id}/reverse", response_model=JournalEntryView)
async def reverse_journal(entry_id: str, body: ReverseEntryIn, p: Principal = Depends(require("txn:adjust"))):
    entry = await reverse_entry(p, entry_id, body.reason.strip())
    await audit(p, "journal.reverse", "journal_entry", entry_id, {"reversal_id": entry["id"], "reason": body.reason})
    return _view(entry)


# ---------- Trial balance ----------
@router.get("/trial-balance", response_model=TrialBalance)
async def trial_balance(as_of: str = "", format: str = "json", p: Principal = Depends(require("report:read"))):
    as_of = as_of or today_iso(p.settings.get("timezone"))
    bal: dict[str, int] = defaultdict(int)  # net cents, +=debit
    for e in await _entries(p, end=as_of):
        for ln in e["lines"]:
            bal[ln["account"]] += ln["debit_cents"] - ln["credit_cents"]
    rows, td, tc = [], 0, 0
    for code in sorted(bal):
        net = bal[code]
        if net == 0:
            continue
        name, typ = _acct_meta(code)
        debit = _f(net) if net > 0 else 0.0
        credit = _f(-net) if net < 0 else 0.0
        td += max(net, 0)
        tc += max(-net, 0)
        rows.append(TrialBalanceRow(code=code, name=name, type=typ, debit=debit, credit=credit))
    if format == "csv":
        return _csv(["code", "account", "type", "debit", "credit"],
                    [[r.code, r.name, r.type, f"{r.debit:.2f}", f"{r.credit:.2f}"] for r in rows], "trial_balance.csv")
    return TrialBalance(as_of=as_of, rows=rows, total_debit=_f(td), total_credit=_f(tc), balanced=td == tc)


# ---------- Balance sheet ----------
@router.get("/balance-sheet", response_model=BalanceSheet)
async def balance_sheet(as_of: str = "", format: str = "json", p: Principal = Depends(require("report:read"))):
    as_of = as_of or today_iso(p.settings.get("timezone"))
    bal: dict[str, int] = defaultdict(int)
    for e in await _entries(p, end=as_of):
        for ln in e["lines"]:
            bal[ln["account"]] += ln["debit_cents"] - ln["credit_cents"]
    assets, liabs, equity = [], [], []
    ta = tl = te = ni = 0
    for code in sorted(bal):
        net = bal[code]
        if net == 0:
            continue
        name, typ = _acct_meta(code)
        if typ == "asset":
            assets.append(BalanceSheetLine(code=code, name=name, balance=_f(net)))
            ta += net
        elif typ == "liability":
            liabs.append(BalanceSheetLine(code=code, name=name, balance=_f(-net)))
            tl += -net
        elif typ == "equity":
            equity.append(BalanceSheetLine(code=code, name=name, balance=_f(-net)))
            te += -net
        elif typ == "income":
            ni += -net  # income credit-normal increases net income
        elif typ == "expense":
            ni -= net   # expense debit-normal decreases net income
    equity.append(BalanceSheetLine(code="3950", name="Current Earnings", balance=_f(ni)))
    te += ni
    if format == "csv":
        out = [["ASSETS", ""]] + [[r.name, f"{r.balance:.2f}"] for r in assets] + [["Total assets", f"{_f(ta):.2f}"], ["", ""],
            ["LIABILITIES", ""]] + [[r.name, f"{r.balance:.2f}"] for r in liabs] + [["Total liabilities", f"{_f(tl):.2f}"], ["", ""],
            ["EQUITY", ""]] + [[r.name, f"{r.balance:.2f}"] for r in equity] + [["Total equity", f"{_f(te):.2f}"]]
        return _csv(["line", "balance"], out, "balance_sheet.csv")
    return BalanceSheet(as_of=as_of, assets=assets, liabilities=liabs, equity=equity, total_assets=_f(ta),
                        total_liabilities=_f(tl), total_equity=_f(te), net_income=_f(ni), balanced=ta == tl + te)


# ---------- Income statement (accrual, from journal) ----------
@router.get("/income-statement", response_model=IncomeStatement)
async def income_statement(start: str = "", end: str = "", format: str = "json", p: Principal = Depends(require("report:read"))):
    today = today_iso(p.settings.get("timezone"))
    start, end = start or today[:8] + "01", end or today
    bal: dict[str, int] = defaultdict(int)
    for e in await _entries(p, start=start, end=end):
        for ln in e["lines"]:
            bal[ln["account"]] += ln["debit_cents"] - ln["credit_cents"]
    income, expenses, ti, te = [], [], 0, 0
    for code in sorted(bal):
        _, typ = _acct_meta(code)
        name = ACCOUNT_NAME.get(code, code)
        if typ == "income":
            amt = -bal[code]
            if amt:
                income.append(IncomeStatementLine(code=code, name=name, amount=_f(amt)))
                ti += amt
        elif typ == "expense":
            amt = bal[code]
            if amt:
                expenses.append(IncomeStatementLine(code=code, name=name, amount=_f(amt)))
                te += amt
    if format == "csv":
        out = [["INCOME", ""]] + [[r.name, f"{r.amount:.2f}"] for r in income] + [["Total income", f"{_f(ti):.2f}"], ["", ""],
            ["EXPENSES", ""]] + [[r.name, f"{r.amount:.2f}"] for r in expenses] + [["Total expenses", f"{_f(te):.2f}"], ["Net income", f"{_f(ti - te):.2f}"]]
        return _csv(["line", "amount"], out, "income_statement.csv")
    return IncomeStatement(start=start, end=end, income=income, expenses=expenses, total_income=_f(ti),
                           total_expenses=_f(te), net_income=_f(ti - te))


# ---------- General ledger ----------
@router.get("/general-ledger", response_model=GeneralLedger)
async def general_ledger(account: str, start: str = "", end: str = "", format: str = "json",
                         p: Principal = Depends(require("report:read"))):
    name, typ = _acct_meta(account)
    opening = 0
    if start:
        for e in await _entries(p, end=_prev(start), account=account):
            for ln in e["lines"]:
                if ln["account"] == account:
                    opening += ln["debit_cents"] - ln["credit_cents"]
    rows, running = [], opening
    for e in await _entries(p, start=start, end=end, account=account):
        for ln in e["lines"]:
            if ln["account"] != account:
                continue
            running += ln["debit_cents"] - ln["credit_cents"]
            rows.append(GLRow(date=e["date"], entry_id=e["id"], ref=e["ref"], memo=e["memo"],
                              debit=_f(ln["debit_cents"]), credit=_f(ln["credit_cents"]), balance=_f(running)))
    if format == "csv":
        return _csv(["date", "ref", "memo", "debit", "credit", "balance"],
                    [[r.date, r.ref, r.memo, f"{r.debit:.2f}", f"{r.credit:.2f}", f"{r.balance:.2f}"] for r in rows],
                    f"gl_{account}.csv")
    return GeneralLedger(account=account, account_name=name, start=start, end=end, opening_balance=_f(opening),
                         rows=rows, closing_balance=_f(running))


def _prev(d: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(d) - timedelta(days=1)).isoformat()


# ---------- A/R aging ----------
@router.get("/ar-aging", response_model=ARAging)
async def ar_aging(as_of: str = "", format: str = "json", p: Principal = Depends(require("report:read"))):
    from datetime import date
    as_of = as_of or today_iso(p.settings.get("timezone"))
    invs = await Scoped("invoices", p).find({"status": {"$in": ["sent", "draft", "paid"]}})
    rows: list[AgingRow] = []
    buckets = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    total = 0.0
    for i in invs:
        bd = i.get("balance_due", i["total"] if i["status"] != "paid" and i["status"] != "void" else 0)
        bd = round(bd, 2)
        if bd <= 0:
            continue
        ref_date = i.get("due_date") or i["date"]
        try:
            days = (date.fromisoformat(as_of) - date.fromisoformat(ref_date)).days
        except ValueError:
            days = 0
        bucket = "current" if days <= 0 else "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"
        buckets[bucket] += bd
        total += bd
        rows.append(AgingRow(invoice_id=i["id"], number=i["number"], client_name=i["client_name"], date=i["date"],
                             due_date=i.get("due_date"), total=round(i["total"], 2), balance_due=bd,
                             days_overdue=max(days, 0), bucket=bucket))
    rows.sort(key=lambda r: -r.days_overdue)
    ar_bal = 0
    for e in await _entries(p, end=as_of, account="1100"):
        for ln in e["lines"]:
            if ln["account"] == "1100":
                ar_bal += ln["debit_cents"] - ln["credit_cents"]
    if format == "csv":
        return _csv(["number", "client", "date", "due_date", "total", "balance_due", "days_overdue", "bucket"],
                    [[r.number, r.client_name, r.date, r.due_date or "", f"{r.total:.2f}", f"{r.balance_due:.2f}", r.days_overdue, r.bucket] for r in rows],
                    "ar_aging.csv")
    return ARAging(as_of=as_of, rows=rows, buckets={k: round(v, 2) for k, v in buckets.items()},
                   total_outstanding=round(total, 2), ar_account_balance=_f(ar_bal),
                   reconciled=abs(round(total, 2) - _f(ar_bal)) < 0.01)


# ---------- Inventory valuation ----------
@router.get("/inventory-valuation", response_model=InventoryValuation)
async def inventory_valuation(format: str = "json", p: Principal = Depends(require("report:read"))):
    as_of = today_iso(p.settings.get("timezone"))
    prods = await Scoped("products", p).find({"active": True})
    rows, total_cents = [], 0
    for d in prods:
        qoh = max(d["quantity_on_hand"], 0)
        val_cents = _c(qoh * d["unit_cost"])
        if qoh == 0 and val_cents == 0:
            continue
        total_cents += val_cents
        rows.append(InvValuationRow(product_id=d["id"], name=d["name"], quantity_on_hand=d["quantity_on_hand"],
                                    unit_cost=round(d["unit_cost"], 4), value=_f(val_cents)))
    rows.sort(key=lambda r: -r.value)
    inv_bal = 0
    for e in await _entries(p, end=as_of, account="1200"):
        for ln in e["lines"]:
            if ln["account"] == "1200":
                inv_bal += ln["debit_cents"] - ln["credit_cents"]
    if format == "csv":
        return _csv(["product", "quantity_on_hand", "unit_cost", "value"],
                    [[r.name, r.quantity_on_hand, f"{r.unit_cost:.4f}", f"{r.value:.2f}"] for r in rows], "inventory_valuation.csv")
    return InventoryValuation(as_of=as_of, rows=rows, total_value=_f(total_cents), inventory_account_balance=_f(inv_bal),
                              reconciled=abs(total_cents - inv_bal) <= 1)


# ---------- Sales / tips / tax summary ----------
@router.get("/sales-summary", response_model=SalesSummary)
async def sales_summary(start: str = "", end: str = "", p: Principal = Depends(require("report:read"))):
    today = today_iso(p.settings.get("timezone"))
    start, end = start or today[:8] + "01", end or today
    bal: dict[str, int] = defaultdict(int)
    for e in await _entries(p, start=start, end=end):
        for ln in e["lines"]:
            bal[ln["account"]] += ln["debit_cents"] - ln["credit_cents"]
    svc = -bal["4000"]
    prod = -bal["4010"] - bal["4090"]
    disc = bal["4900"] + bal["4910"]
    tax = -bal["2100"]
    tips = -bal["2200"]
    fees = bal["6200"]
    cogs = bal["5000"] + bal["5010"]
    net_sales = svc + prod - disc
    return SalesSummary(start=start, end=end, service_revenue=_f(svc), product_revenue=_f(prod), discounts=_f(disc),
                        net_sales=_f(net_sales), tax_collected=_f(tax), tips_collected=_f(tips),
                        processing_fees=_f(fees), cogs=_f(cogs), gross_profit=_f(net_sales - cogs))
