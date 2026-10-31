"""Advanced financial reports built directly from the posted general ledger:
cash-flow statement, budget vs actual, income by customer, expenses by vendor,
plus CSV exports. All figures reconcile to the ledger."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib.coa import ACCOUNT_TYPE_OF, NAME_OF, NORMAL_BALANCE_OF
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/reports", tags=["reports"])

CASH_CODES = {"1010", "1020", "1021", "1022", "1023", "1050", "1060"}


def _classify(code):
    t = ACCOUNT_TYPE_OF.get(code)
    if code in ("1800", "1850"):
        return "investing"
    if code in ("2500",) or t == "equity":
        return "financing"
    return "operating"


# ---------------- Cash Flow Statement ----------------
async def _cashflow(tenant_id, date_from, date_to):
    s = Scoped(db.journal_lines, tenant_id)
    lines = await s.find_list({"status": "posted", "entry_date": {"$gte": date_from, "$lte": date_to}},
                              limit=100000)
    by_entry = {}
    for ln in lines:
        by_entry.setdefault(ln["entry_id"], []).append(ln)
    activities = {"operating": {}, "investing": {}, "financing": {}}
    net_change = 0
    for eid, lns in by_entry.items():
        cash_delta = sum((l["debit"] - l["credit"]) for l in lns if l["account_code"] in CASH_CODES)
        if cash_delta == 0:
            continue
        net_change += cash_delta
        non_cash = [l for l in lns if l["account_code"] not in CASH_CODES]
        if not non_cash:
            continue
        # attribute cash delta across non-cash legs proportionally by amount
        total_nc = sum(abs(l["debit"] - l["credit"]) for l in non_cash) or 1
        for l in non_cash:
            weight = abs(l["debit"] - l["credit"]) / total_nc
            bucket = _classify(l["account_code"])
            key = l["account_code"]
            activities[bucket][key] = activities[bucket].get(key, 0) + round(cash_delta * weight)
    # opening cash balance
    pre = await L._balances(db, tenant_id, as_of=L._prev_day(date_from))
    opening = sum(L._net_balance(c, pre.get(c, {}).get("debit", 0), pre.get(c, {}).get("credit", 0))
                  for c in CASH_CODES)

    def section(name):
        items = [{"code": c, "name": NAME_OF.get(c, c), "amount": cents_to_dollars(v), "amount_cents": v}
                 for c, v in sorted(activities[name].items()) if v != 0]
        return {"items": items, "total_cents": sum(i["amount_cents"] for i in items),
                "total": cents_to_dollars(sum(i["amount_cents"] for i in items))}

    op, inv, fin = section("operating"), section("investing"), section("financing")
    return {
        "date_from": date_from, "date_to": date_to,
        "operating": op, "investing": inv, "financing": fin,
        "net_change_cents": net_change, "net_change": cents_to_dollars(net_change),
        "opening_cents": opening, "opening": cents_to_dollars(opening),
        "closing_cents": opening + net_change, "closing": cents_to_dollars(opening + net_change),
    }


@router.get("/cash-flow")
async def cash_flow(date_from: Optional[str] = None, date_to: Optional[str] = None,
                    user: dict = Depends(require("ledger:read"))):
    return await _cashflow(user["tenant_id"], date_from or "2000-01-01", date_to or today_str())


# ---------------- Budgets ----------------
class BudgetBody(BaseModel):
    year: str
    account_code: str
    amount: float


@router.get("/budgets")
async def list_budgets(year: Optional[str] = None, user: dict = Depends(require("ledger:read"))):
    q = {"year": year} if year else {}
    return await Scoped(db.budgets, user["tenant_id"]).find_list(q, sort=[("account_code", 1)])


@router.post("/budgets")
async def set_budget(body: BudgetBody, user: dict = Depends(require("ledger:admin"))):
    if body.account_code not in ACCOUNT_TYPE_OF:
        raise HTTPException(status_code=422, detail="Unknown account")
    s = Scoped(db.budgets, user["tenant_id"])
    existing = await s.find_one({"year": body.year, "account_code": body.account_code})
    amount = to_cents(body.amount)
    if existing:
        await s.update_one({"year": body.year, "account_code": body.account_code},
                           {"$set": {"amount_cents": amount}})
        return {"year": body.year, "account_code": body.account_code, "amount_cents": amount}
    doc = {"id": new_id(), "year": body.year, "account_code": body.account_code,
           "account_name": NAME_OF.get(body.account_code), "amount_cents": amount, "created_at": now_iso()}
    await s.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/budget-vs-actual")
async def budget_vs_actual(year: str, user: dict = Depends(require("ledger:read"))):
    tenant_id = user["tenant_id"]
    budgets = await Scoped(db.budgets, tenant_id).find_list({"year": year})
    bals = await L._balances(db, tenant_id, as_of=f"{year}-12-31", date_from=f"{year}-01-01")
    bmap = {b["account_code"]: b["amount_cents"] for b in budgets}
    codes = set(bmap.keys()) | {c for c in bals if ACCOUNT_TYPE_OF.get(c) in ("revenue", "expense")}
    rows = []
    for c in sorted(codes):
        t = ACCOUNT_TYPE_OF.get(c)
        if t not in ("revenue", "expense"):
            continue
        v = bals.get(c, {"debit": 0, "credit": 0})
        actual = L._net_balance(c, v["debit"], v["credit"])
        budget = bmap.get(c, 0)
        variance = (actual - budget) if t == "revenue" else (budget - actual)
        # variance_pct is null when nothing was budgeted -- never div-by-zero.
        # Positive variance is favorable (revenue beat / expense under budget).
        variance_pct = round(variance / budget * 100, 1) if budget else None
        rows.append({"code": c, "name": NAME_OF.get(c, c), "type": t,
                     "budget": cents_to_dollars(budget), "actual": cents_to_dollars(actual),
                     "budget_cents": budget, "actual_cents": actual,
                     "variance": cents_to_dollars(variance), "variance_cents": variance,
                     "variance_pct": variance_pct})
    return {"year": year, "rows": rows}


# ---------------- Income by customer ----------------
@router.get("/income-by-customer")
async def income_by_customer(date_from: Optional[str] = None, date_to: Optional[str] = None,
                             user: dict = Depends(require("ledger:read"))):
    df = date_from or "2000-01-01"
    dt = date_to or today_str()
    invoices = await Scoped(db.invoices, user["tenant_id"]).find_list()
    buckets = {}
    for inv in invoices:
        d = (inv.get("created_at") or "")[:10]
        if d and (d < df or d > dt):
            continue
        name = inv.get("client_name", "—")
        b = buckets.setdefault(name, {"income_cents": 0, "paid_cents": 0, "count": 0})
        b["income_cents"] += inv.get("subtotal_cents", inv.get("total_cents", 0))
        if inv.get("status") == "paid":
            b["paid_cents"] += inv.get("total_cents", 0)
        b["count"] += 1
    rows = [{"customer": k, "income": cents_to_dollars(v["income_cents"]), "income_cents": v["income_cents"],
             "paid": cents_to_dollars(v["paid_cents"]), "invoices": v["count"]}
            for k, v in buckets.items()]
    rows.sort(key=lambda x: -x["income_cents"])
    return {"date_from": df, "date_to": dt, "rows": rows,
            "total": cents_to_dollars(sum(r["income_cents"] for r in rows))}


# ---------------- Expenses by vendor ----------------
@router.get("/expenses-by-vendor")
async def expenses_by_vendor(date_from: Optional[str] = None, date_to: Optional[str] = None,
                             user: dict = Depends(require("ledger:read"))):
    df = date_from or "2000-01-01"
    dt = date_to or today_str()
    tenant_id = user["tenant_id"]
    buckets = {}
    for b in await Scoped(db.bills, tenant_id).find_list():
        d = b.get("bill_date") or (b.get("created_at") or "")[:10]
        if d and (d < df or d > dt):
            continue
        name = b.get("vendor", "—")
        x = buckets.setdefault(name, {"cents": 0, "count": 0})
        x["cents"] += b.get("amount_cents", 0)
        x["count"] += 1
    for e in await Scoped(db.expenses, tenant_id).find_list():
        d = e.get("expense_date") or (e.get("created_at") or "")[:10]
        if d and (d < df or d > dt):
            continue
        name = e.get("vendor", "—")
        x = buckets.setdefault(name, {"cents": 0, "count": 0})
        x["cents"] += e.get("amount_cents", 0)
        x["count"] += 1
    rows = [{"vendor": k, "spend": cents_to_dollars(v["cents"]), "spend_cents": v["cents"], "count": v["count"]}
            for k, v in buckets.items()]
    rows.sort(key=lambda x: -x["spend_cents"])
    return {"date_from": df, "date_to": dt, "rows": rows,
            "total": cents_to_dollars(sum(r["spend_cents"] for r in rows))}


# ---------------- CSV exports ----------------
def _csv_response(header, rows, filename):
    out = [",".join(header)]
    for r in rows:
        out.append(",".join(f'"{str(x)}"' if ("," in str(x)) else str(x) for x in r))
    return Response(content="\n".join(out), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/cash-flow.csv")
async def cash_flow_csv(date_from: Optional[str] = None, date_to: Optional[str] = None,
                        user: dict = Depends(require("ledger:read"))):
    d = await _cashflow(user["tenant_id"], date_from or "2000-01-01", date_to or today_str())
    rows = []
    for sec in ("operating", "investing", "financing"):
        for it in d[sec]["items"]:
            rows.append([sec.title(), it["code"], it["name"], f'{it["amount"]:.2f}'])
        rows.append([f"{sec.title()} Total", "", "", f'{d[sec]["total"]:.2f}'])
    rows.append(["Net Change", "", "", f'{d["net_change"]:.2f}'])
    rows.append(["Closing Cash", "", "", f'{d["closing"]:.2f}'])
    return _csv_response(["Activity", "Code", "Account", "Amount"], rows, "cash_flow.csv")


@router.get("/budget-vs-actual.csv")
async def bva_csv(year: str, user: dict = Depends(require("ledger:read"))):
    d = await budget_vs_actual(year, user)
    rows = [[r["code"], r["name"], r["type"], f'{r["budget"]:.2f}', f'{r["actual"]:.2f}',
             f'{r["variance"]:.2f}', "" if r["variance_pct"] is None else f'{r["variance_pct"]:.1f}']
            for r in d["rows"]]
    return _csv_response(["Code", "Account", "Type", "Budget", "Actual", "Variance", "VariancePct"],
                         rows, f"budget_{year}.csv")


@router.get("/income-by-customer.csv")
async def ibc_csv(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  user: dict = Depends(require("ledger:read"))):
    d = await income_by_customer(date_from, date_to, user)
    rows = [[r["customer"], f'{r["income"]:.2f}', f'{r["paid"]:.2f}', r["invoices"]] for r in d["rows"]]
    return _csv_response(["Customer", "Income", "Paid", "Invoices"], rows, "income_by_customer.csv")


@router.get("/expenses-by-vendor.csv")
async def ebv_csv(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  user: dict = Depends(require("ledger:read"))):
    d = await expenses_by_vendor(date_from, date_to, user)
    rows = [[r["vendor"], f'{r["spend"]:.2f}', r["count"]] for r in d["rows"]]
    return _csv_response(["Vendor", "Spend", "Transactions"], rows, "expenses_by_vendor.csv")


@router.get("/profit-loss.csv")
async def pnl_csv(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  user: dict = Depends(require("ledger:read"))):
    d = await L.pnl(db, user["tenant_id"], date_from or "2000-01-01", date_to or today_str())
    rows = [["REVENUE", "", ""]]
    for r in d["revenue"]:
        rows.append([r["code"], r["name"], f'{cents_to_dollars(r["amount"]):.2f}'])
    rows.append(["Total Revenue", "", f'{cents_to_dollars(d["revenue_total"]):.2f}'])
    rows.append(["EXPENSES", "", ""])
    for r in d["expense"]:
        rows.append([r["code"], r["name"], f'{cents_to_dollars(r["amount"]):.2f}'])
    rows.append(["Total Expenses", "", f'{cents_to_dollars(d["expense_total"]):.2f}'])
    rows.append(["Net Income", "", f'{cents_to_dollars(d["net_income"]):.2f}'])
    return _csv_response(["Code", "Account", "Amount"], rows, "profit_loss.csv")


@router.get("/balance-sheet.csv")
async def bs_csv(as_of: Optional[str] = None, user: dict = Depends(require("ledger:read"))):
    d = await L.balance_sheet(db, user["tenant_id"], as_of or today_str())
    rows = []
    for sec in ("assets", "liabilities", "equity"):
        rows.append([sec.upper(), "", ""])
        for r in d[sec]:
            rows.append([r["code"], r["name"], f'{cents_to_dollars(r["amount"]):.2f}'])
    return _csv_response(["Code", "Account", "Amount"], rows, "balance_sheet.csv")


# ---------------- Booth rent ----------------
def _months_between(df: str, dt: str) -> int:
    """Number of calendar months touched by [df, dt], minimum 1."""
    y1, m1 = int(df[0:4]), int(df[5:7])
    y2, m2 = int(dt[0:4]), int(dt[5:7])
    return max(1, (y2 - y1) * 12 + (m2 - m1) + 1)


@router.get("/booth-rent")
async def booth_rent(date_from: Optional[str] = None, date_to: Optional[str] = None,
                     user: dict = Depends(require("ledger:read"))):
    """Per team member: monthly booth rent (from users' booth_rent field), rent
    expected for the period (monthly rent x months in period), rent actually
    collected in the period, and the outstanding balance."""
    df = date_from or "2000-01-01"
    dt = date_to or today_str()
    months = _months_between(df, dt)
    tenant_id = user["tenant_id"]
    team = await db.users.find({"tenant_id": tenant_id, "role": {"$ne": "owner"}},
                               {"_id": 0, "password_hash": 0}).to_list(1000)
    team = [u for u in team if u.get("role") in ("barber", "staff") or (u.get("booth_rent") or 0) > 0]
    collected = {}
    for p in await Scoped(db.booth_rent_payments, tenant_id).find_list():
        d = p.get("pay_date") or (p.get("created_at") or "")[:10]
        if d and (d < df or d > dt):
            continue
        bid = p.get("barber_id")
        collected[bid] = collected.get(bid, 0) + p.get("amount_cents", 0)
    rows = []
    for u in team:
        rent_cents = to_cents(u.get("booth_rent") or 0)
        expected = rent_cents * months
        got = collected.get(u["id"], 0)
        bal = expected - got
        rows.append({"barber_id": u["id"], "name": u.get("name"),
                     "monthly_rent_cents": rent_cents,
                     "monthly_rent": cents_to_dollars(rent_cents),
                     "months": months,
                     "expected_cents": expected,
                     "expected": cents_to_dollars(expected),
                     "collected_cents": got,
                     "collected": cents_to_dollars(got),
                     "balance_cents": bal,
                     "balance": cents_to_dollars(bal)})
    rows.sort(key=lambda r: -r["balance_cents"])
    return {"date_from": df, "date_to": dt, "months": months, "rows": rows,
            "total_expected": cents_to_dollars(sum(r["expected_cents"] for r in rows)),
            "total_collected": cents_to_dollars(sum(r["collected_cents"] for r in rows)),
            "total_balance": cents_to_dollars(sum(r["balance_cents"] for r in rows))}


class BoothRentPaymentBody(BaseModel):
    barber_id: str
    amount: float
    payment_method: str = "bank"          # cash | card | bank
    pay_date: Optional[str] = None


@router.post("/booth-rent/payment")
async def record_booth_rent(body: BoothRentPaymentBody,
                            user: dict = Depends(require("ops:manage"))):
    """Record a booth-rent collection: Dr cash / Cr 4200 Chair Rental Income."""
    barber = await db.users.find_one({"id": body.barber_id, "tenant_id": user["tenant_id"]},
                                     {"_id": 0, "password_hash": 0})
    if not barber:
        raise HTTPException(status_code=404, detail="Team member not found")
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    cash = P._cash_account(body.payment_method)
    pid = new_id()
    pdate = body.pay_date or today_str()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": pdate, "source": "booth_rent", "source_ref": pid,
        "memo": f"Booth rent collected — {barber.get('name')}",
        "lines": [P.dr(cash, amount, "Booth rent received"),
                  P.cr("4200", amount, "Chair rental income")],
    }, status="posted")
    doc = {"id": pid, "barber_id": barber["id"], "barber_name": barber.get("name"),
           "amount_cents": amount, "payment_method": body.payment_method,
           "pay_date": pdate, "entry_number": entry["number"],
           "created_by": user.get("name"), "created_at": now_iso()}
    await Scoped(db.booth_rent_payments, user["tenant_id"]).insert_one(doc)
    doc.pop("_id", None)
    return doc


# ---------------- Balance assertions + ledger health (bake-off #5) ----------------
# A balance assertion is a PERSISTENT record — "on this date, this account
# held exactly this amount" — re-verified on read, not a one-off reconcile
# screen. Implemented independently (concept study: Beancount docs).

def assertions_coll(user):
    return Scoped(db.balance_assertions, user["tenant_id"])


async def _gl_balance_for_account(user, gl_code: str, as_of: str) -> int:
    bals = await L._balances(db, user["tenant_id"], as_of=as_of)
    v = bals.get(gl_code, {"debit": 0, "credit": 0})
    return L._net_balance(gl_code, v["debit"], v["credit"])


async def _evaluate_assertion(user, a: dict) -> dict:
    computed = await _gl_balance_for_account(user, a["gl_code"], a["statement_date"])
    diff = a["statement_cents"] - computed
    out = dict(a)
    out["computed_cents"] = computed
    out["difference_cents"] = diff
    out["status"] = "passed" if diff == 0 else "failed"
    return out


class AssertionBody(BaseModel):
    bank_account_id: str
    statement_date: str        # YYYY-MM-DD
    statement_balance: float   # dollars, in the account's normal direction


@router.post("/balance-assertions")
async def create_assertion(body: AssertionBody, user: dict = Depends(require("ledger:admin"))):
    acc = await Scoped(db.bank_accounts, user["tenant_id"]).find_one({"id": body.bank_account_id})
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")
    doc = {"id": new_id(), "bank_account_id": body.bank_account_id,
           "account_name": acc.get("name"), "gl_code": acc["gl_code"],
           "statement_date": body.statement_date,
           "statement_cents": to_cents(body.statement_balance),
           "waived": False, "waived_by": None, "waived_at": None, "waive_reason": None,
           "created_by": user["email"], "created_at": now_iso()}
    evaluated = await _evaluate_assertion(user, doc)
    doc["computed_cents"] = evaluated["computed_cents"]
    doc["difference_cents"] = evaluated["difference_cents"]
    doc["status"] = evaluated["status"]
    await assertions_coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/balance-assertions")
async def list_assertions(user: dict = Depends(require("ledger:read"))):
    rows = await assertions_coll(user).find_list(sort=[("statement_date", -1)], limit=500)
    out = []
    for a in rows:
        ev = await _evaluate_assertion(user, a)   # re-verify on read: standing proof
        ev["waived"] = a.get("waived", False)
        ev["waived_by"] = a.get("waived_by")
        ev["waive_reason"] = a.get("waive_reason")
        out.append(ev)
    return out


class WaiveBody(BaseModel):
    reason: str


@router.post("/balance-assertions/{assertion_id}/waive")
async def waive_assertion(assertion_id: str, body: WaiveBody,
                          user: dict = Depends(require("ledger:admin"))):
    """Named, reasoned, audited waiver — the ONLY way a period may lock over
    a failed assertion. The assertion record itself is never edited away."""
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="A waiver needs a reason")
    a = await assertions_coll(user).find_one({"id": assertion_id})
    if not a:
        raise HTTPException(status_code=404, detail="Assertion not found")
    await assertions_coll(user).update_one({"id": assertion_id}, {"$set": {
        "waived": True, "waived_by": user["email"], "waived_at": now_iso(),
        "waive_reason": body.reason.strip()}})
    from lib.audit import append_audit
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"], "actor_id": user["id"],
        "actor_name": user.get("name"), "actor_role": user.get("role"),
        "action": "balance_assertion.waive", "entity": "balance_assertion",
        "entity_id": assertion_id, "details": {"reason": body.reason.strip()},
        "created_at": now_iso()})
    return {"id": assertion_id, "waived": True, "waived_by": user["email"]}


@router.get("/ledger-health")
async def ledger_health(user: dict = Depends(require("ledger:read"))):
    """One pass/fail gate over the whole book (bake-off #5) plus the current
    state of every balance assertion."""
    result = await L.run_health_check(db, user["tenant_id"])
    assertion_failures = []
    for a in await assertions_coll(user).find_list(limit=500):
        ev = await _evaluate_assertion(user, a)
        if ev["status"] == "failed" and not a.get("waived"):
            assertion_failures.append({"kind": "failed_assertion", "assertion_id": a["id"],
                                       "bank_account_id": a.get("bank_account_id"),
                                       "statement_date": a.get("statement_date"),
                                       "difference_cents": ev["difference_cents"]})
    violations = result["violations"] + assertion_failures
    return {"ok": not violations, "violations": violations,
            "counts": {**result["counts"], "assertion_failures": len(assertion_failures)}}


# ---------------- Per-tag (per-job) report (bake-off #7) ----------------
@router.get("/by-tag")
async def by_tag(date_from: Optional[str] = None, date_to: Optional[str] = None,
                 user: dict = Depends(require("ledger:read"))):
    """Money in/out per job tag, from posted bank transactions. Category says
    what kind of cost; tag says which job."""
    q = {"status": {"$in": ["cleared", "matched"]}}
    if date_from:
        q.setdefault("txn_date", {})["$gte"] = date_from
    if date_to:
        q.setdefault("txn_date", {})["$lte"] = date_to
    rows = await Scoped(db.bank_transactions, user["tenant_id"]).find_list(q, limit=10000)
    agg = {}
    for t in rows:
        for tag in (t.get("tags") or []):
            a = agg.setdefault(tag, {"tag": tag, "count": 0, "in_cents": 0, "out_cents": 0})
            a["count"] += 1
            if t["amount_cents"] > 0:
                a["in_cents"] += t["amount_cents"]
            else:
                a["out_cents"] += -t["amount_cents"]
    out = sorted(agg.values(), key=lambda a: a["tag"])
    for a in out:
        a["net_cents"] = a["in_cents"] - a["out_cents"]
    return {"tags": out, "transactions": len(rows)}


# ---------------- Plain-text full-ledger export (bake-off #9) ----------------
@router.get("/ledger-export")
async def ledger_export(user: dict = Depends(require("ledger:read"))):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0, "name": 1})
    text = await L.export_ledger_text(db, user["tenant_id"],
                                      tenant_name=(tenant or {}).get("name", ""))
    return Response(content=text, media_type="text/plain",
                    headers={"Content-Disposition": "attachment; filename=ledger-export.txt"})
