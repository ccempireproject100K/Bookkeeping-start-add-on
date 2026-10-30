"""Vendor bills (accounts payable sub-ledger). Supports partial payments, recurring
bills and vendor-credit application. Each cash movement posts to the GL.

Bake-off #2 (recurring automation) adds recurring bill TEMPLATES below:
schedule definitions (same frequency/end-rule model as recurring invoices)
that materialize real bills through create_bill — run manually via
POST /bills/templates/run-due or hourly by the scheduler."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, timedelta

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P
from lib import fx as FX
from lib import fx_posting as FXP
from lib import currencies as C
from lib.currencies import normalize as normalize_currency

router = APIRouter(prefix="/bills", tags=["bills"])


def coll(user):
    return Scoped(db.bills, user["tenant_id"])


def credits(user):
    return Scoped(db.vendor_credits, user["tenant_id"])


class BillLine(BaseModel):
    expense_code: str = "5700"
    description: str = ""
    amount: float


class BillBody(BaseModel):
    vendor: str
    vendor_id: Optional[str] = None
    amount: float = 0
    expense_code: str = "5700"
    lines: Optional[List[BillLine]] = None
    bill_date: Optional[str] = None
    due_date: Optional[str] = None
    recurring: bool = False
    frequency: Optional[str] = None     # monthly | weekly | quarterly
    currency: str = "USD"               # ISO 4217; validated on create


def _enrich(b):
    b["paid_cents"] = b.get("paid_cents", b["amount_cents"] if b.get("status") == "paid" else 0)
    b["balance_cents"] = b["amount_cents"] - b["paid_cents"]
    b["balance"] = cents_to_dollars(b["balance_cents"])
    b["amount"] = cents_to_dollars(b["amount_cents"])
    return b


@router.get("")
async def list_bills(user: dict = Depends(get_current_user)):
    rows = await coll(user).find_list(sort=[("created_at", -1)], limit=500)
    return [_enrich(b) for b in rows]


@router.post("")
async def create_bill(body: BillBody, user: dict = Depends(require("ops:manage"))):
    try:
        currency = normalize_currency(body.currency)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unsupported currency: {body.currency!r}")
    bdate = body.bill_date or today_str()
    due = body.due_date or (date.fromisoformat(bdate) + timedelta(days=30)).isoformat()
    if body.lines:
        line_items = [{"expense_code": l.expense_code, "description": l.description,
                       "amount_cents": to_cents(l.amount)} for l in body.lines]
    else:
        line_items = [{"expense_code": body.expense_code, "description": body.vendor,
                       "amount_cents": to_cents(body.amount)}]
    amount = sum(li["amount_cents"] for li in line_items)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Bill amount must be positive")
    bill_id = new_id()
    jl = [P.dr(li["expense_code"], li["amount_cents"], li["description"]) for li in line_items]
    jl.append(P.cr("2010", amount, "Accounts payable"))
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": bdate, "source": "bill", "source_ref": bill_id,
        "memo": f"Bill — {body.vendor}", "lines": jl, "currency": currency,
    }, status="posted")
    doc = {
        "id": bill_id, "vendor": body.vendor, "vendor_id": body.vendor_id,
        "amount_cents": amount, "currency": currency,
        "expense_code": line_items[0]["expense_code"],
        "line_items": line_items, "bill_date": bdate, "due_date": due, "status": "open",
        "paid_cents": 0, "payments": [], "recurring": body.recurring, "frequency": body.frequency,
        "issue_entry": entry["number"], "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return _enrich(doc)


class PayBody(BaseModel):
    payment_method: str = "bank"
    amount: Optional[float] = None       # None = pay full remaining balance
    credit_id: Optional[str] = None      # apply a vendor credit
    pay_date: Optional[str] = None
    payment_currency: Optional[str] = None  # currency actually moved; defaults to the bill currency


@router.post("/{bill_id}/pay")
async def pay_bill(bill_id: str, body: PayBody, user: dict = Depends(require("ops:manage"))):
    bill = await coll(user).find_one({"id": bill_id})
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    paid = bill.get("paid_cents", 0)
    balance = bill["amount_cents"] - paid
    if balance <= 0:
        raise HTTPException(status_code=409, detail="Bill already fully paid")
    bill_currency = normalize_currency(bill.get("currency") or "USD")
    try:
        pay_currency = normalize_currency(body.payment_currency or bill_currency)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unsupported currency: {body.payment_currency!r}")
    pay_date = body.pay_date or today_str()

    target = to_cents(body.amount) if body.amount is not None else balance
    if pay_currency != bill_currency and body.amount is not None:
        # body.amount is denominated in pay_currency; price it in bill terms
        try:
            target = await FX.convert(
                C.to_minor_units(body.amount, pay_currency),
                pay_currency, bill_currency, pay_date, db=db)
        except FX.FXRateError:
            raise HTTPException(status_code=422, detail=(
                "No exchange rate available to convert this payment"))
        pay_minor_for_fx = C.to_minor_units(body.amount, pay_currency)
    elif pay_currency != bill_currency:
        # full balance: the pay-currency equivalent is derived at pay-date rate
        try:
            pay_minor_for_fx = await FX.convert(
                balance, bill_currency, pay_currency, pay_date, db=db)
        except FX.FXRateError:
            raise HTTPException(status_code=422, detail=(
                "No exchange rate available to price this payment's currency"))
    else:
        pay_minor_for_fx = target
    if target <= 0 or target > balance:
        raise HTTPException(status_code=422, detail="Invalid payment amount")

    credit_applied = 0
    if body.credit_id:
        cr = await credits(user).find_one({"id": body.credit_id})
        if not cr:
            raise HTTPException(status_code=404, detail="Vendor credit not found")
        credit_applied = min(cr.get("remaining_cents", 0), target)

    cash_portion = target - credit_applied
    cash = P._cash_account(body.payment_method)
    entry_number = None
    if cash_portion > 0:
        entry = await L.post_journal_entry(db, user["tenant_id"], user, {
            "entry_date": pay_date, "source": "ap_payment", "source_ref": bill_id,
            "memo": f"Pay bill — {bill['vendor']}",
            "lines": [P.dr("2010", cash_portion, "AP settled"), P.cr(cash, cash_portion, "Payment")],
            "currency": bill_currency,
        }, status="posted")
        entry_number = entry["number"]
        # Realized FX on the cash that settled AP (best-effort; never breaks
        # the payment). Vendor credits applied carry no FX.
        ap_cash_doc = cash_portion
        pay_for_ap = (int(round(pay_minor_for_fx * ap_cash_doc / target))
                      if pay_currency != bill_currency and target > 0
                      else ap_cash_doc)
        await FXP.post_realized_fx(
            db=db, tenant_id=user["tenant_id"], user=user, kind="bill",
            doc=bill, pay_currency=pay_currency,
            pay_amount_minor=pay_for_ap, applied_doc_minor=ap_cash_doc,
            pay_date=pay_date)

    if credit_applied > 0:
        await credits(user).update_one({"id": body.credit_id},
                                       {"$inc": {"remaining_cents": -credit_applied}})
        if cash_portion == 0:
            # Settled entirely by vendor credit: no cash moved, but reverse
            # any outstanding unrealized FX on the bill.
            await FXP.post_realized_fx(
                db=db, tenant_id=user["tenant_id"], user=user, kind="bill",
                doc=bill, pay_currency=bill_currency,
                pay_amount_minor=0, pay_date=pay_date)

    new_paid = paid + target
    status = "paid" if new_paid >= bill["amount_cents"] else "partial"
    pmt = {"date": pay_date, "amount_cents": target,
           "cash_cents": cash_portion, "credit_cents": credit_applied,
           "method": body.payment_method, "pay_currency": pay_currency,
           "entry": entry_number}
    await coll(user).update_one({"id": bill_id}, {"$set": {"status": status, "paid_cents": new_paid},
                                                  "$push": {"payments": pmt}})
    return {"status": status, "paid_cents": new_paid, "balance_cents": bill["amount_cents"] - new_paid,
            "entry_number": entry_number, "credit_applied_cents": credit_applied}


@router.post("/{bill_id}/generate-next")
async def generate_recurring(bill_id: str, user: dict = Depends(require("ops:manage"))):
    """Create the next occurrence of a recurring bill (manual trigger; no scheduler)."""
    bill = await coll(user).find_one({"id": bill_id})
    if not bill or not bill.get("recurring"):
        raise HTTPException(status_code=404, detail="Recurring bill not found")
    freq = bill.get("frequency") or "monthly"
    days = {"weekly": 7, "monthly": 30, "quarterly": 90}.get(freq, 30)
    bdate = (date.fromisoformat(bill["bill_date"]) + timedelta(days=days)).isoformat()
    body = BillBody(vendor=bill["vendor"], vendor_id=bill.get("vendor_id"),
                    expense_code=bill["expense_code"],
                    lines=[BillLine(expense_code=li["expense_code"], description=li.get("description", ""),
                                    amount=cents_to_dollars(li["amount_cents"])) for li in bill.get("line_items", [])] or None,
                    amount=cents_to_dollars(bill["amount_cents"]),
                    bill_date=bdate, recurring=True, frequency=freq)
    return await create_bill(body, user)


# ---------------- Recurring bill templates (bake-off #2) ----------------
from lib.audit import append_audit as _append_audit  # noqa: E402


def templates(user):
    return Scoped(db.recurring_bills, user["tenant_id"])


class BillTemplateBody(BaseModel):
    name: str
    vendor: str
    vendor_id: Optional[str] = None
    amount: float = 0
    expense_code: str = "5700"
    lines: Optional[List[BillLine]] = None
    currency: str = "USD"
    frequency: str = "monthly"          # daily|weekly|biweekly|monthly|quarterly|semiannual|annual|custom
    interval_days: Optional[int] = None
    next_run: Optional[str] = None      # YYYY-MM-DD; defaults to today
    due_offset_days: int = 30           # bill due = run date + offset
    active: bool = True
    paused: bool = False
    end_type: str = "never"             # never | after_count | on_date
    end_count: Optional[int] = None
    end_date: Optional[str] = None


def _bill_template_finished(t: dict, runs_made: int, next_run: str) -> bool:
    end_type = t.get("end_type") or "never"
    if end_type == "after_count" and t.get("end_count"):
        return runs_made >= int(t["end_count"])
    if end_type == "on_date" and t.get("end_date"):
        return next_run > t["end_date"]
    return False


@router.get("/templates")
async def list_bill_templates(user: dict = Depends(get_current_user)):
    return await templates(user).find_list(sort=[("name", 1)], limit=500)


@router.post("/templates")
async def create_bill_template(body: BillTemplateBody, user: dict = Depends(require("ops:manage"))):
    from routers.recurring_invoices import FREQUENCIES
    if body.frequency not in FREQUENCIES:
        raise HTTPException(status_code=422, detail=f"frequency must be one of {', '.join(FREQUENCIES)}")
    if body.frequency == "custom" and not (body.interval_days and body.interval_days >= 1):
        raise HTTPException(status_code=422, detail="interval_days (>= 1) is required for custom frequency")
    if body.end_type not in ("never", "after_count", "on_date"):
        raise HTTPException(status_code=422, detail="end_type must be never, after_count or on_date")
    if body.end_type == "after_count" and not (body.end_count and body.end_count >= 1):
        raise HTTPException(status_code=422, detail="end_count (>= 1) is required for end_type=after_count")
    if body.next_run:
        try:
            date.fromisoformat(body.next_run)
        except ValueError:
            raise HTTPException(status_code=422, detail="next_run must be YYYY-MM-DD")
    doc = {"id": new_id(), **body.model_dump(), "runs_made": 0, "status": "active",
           "next_run": body.next_run or today_str(), "created_at": now_iso()}
    await templates(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.patch("/templates/{tid}")
async def update_bill_template(tid: str, body: BillTemplateBody,
                               user: dict = Depends(require("ops:manage"))):
    t = await templates(user).find_one({"id": tid})
    if not t:
        raise HTTPException(status_code=404, detail="Bill template not found")
    await templates(user).update_one({"id": tid}, {"$set": body.model_dump()})
    return {"id": tid, **body.model_dump()}


@router.delete("/templates/{tid}")
async def delete_bill_template(tid: str, user: dict = Depends(require("ops:manage"))):
    res = await db.recurring_bills.delete_one({"id": tid, "tenant_id": user["tenant_id"]})
    if not res.deleted_count:
        raise HTTPException(status_code=404, detail="Bill template not found")
    return {"ok": True}


async def run_due_bills_for_user(user: dict) -> dict:
    """Materialize due recurring bills for the user's tenant. Same semantics
    as recurring invoices: paused skipped, end rules retire the template,
    missed occurrences catch up (capped per run)."""
    from routers.recurring_invoices import _advance
    today = today_str()
    made = []
    for t in await templates(user).find_list({"active": True, "next_run": {"$lte": today}}):
        if t.get("paused"):
            continue
        runs_made = int(t.get("runs_made") or 0)
        next_run = t["next_run"]
        if _bill_template_finished(t, runs_made, next_run):
            await templates(user).update_one({"id": t["id"]},
                                             {"$set": {"active": False, "status": "completed"}})
            continue
        made_for_template = 0
        while next_run <= today and made_for_template < 24:
            if _bill_template_finished(t, runs_made, next_run):
                break
            due = (date.fromisoformat(next_run) + timedelta(days=int(t.get("due_offset_days", 30)))).isoformat()
            body = BillBody(
                vendor=t["vendor"], vendor_id=t.get("vendor_id"),
                amount=t.get("amount", 0), expense_code=t.get("expense_code", "5700"),
                lines=[BillLine(**l) for l in t["lines"]] if t.get("lines") else None,
                bill_date=next_run, due_date=due, currency=t.get("currency", "USD"))
            try:
                bill = await create_bill(body, user)
            except Exception:
                break
            runs_made += 1
            made_for_template += 1
            made.append({"template_id": t["id"], "template": t["name"],
                         "bill_id": bill["id"], "run_date": next_run})
            next_run = _advance(next_run, t.get("frequency", "monthly"), t.get("interval_days"))
        finished = _bill_template_finished(t, runs_made, next_run)
        upd = {"next_run": next_run, "runs_made": runs_made, "last_run": today}
        if finished:
            upd.update({"active": False, "status": "completed"})
        await templates(user).update_one({"id": t["id"]}, {"$set": upd})
    return {"generated": len(made), "bills": made}


@router.post("/templates/run-due")
async def run_due_bills(user: dict = Depends(require("ops:manage"))):
    return await run_due_bills_for_user(user)


# ---------------- Expected-bills calendar (bake-off #12) ----------------
@router.get("/expected")
async def expected_bills(days: int = 60, user: dict = Depends(get_current_user)):
    """Recurring obligations as EXPECTATIONS, matched against the bank feed:
    which fixed bills are coming, which already hit the bank, and which are
    overdue-unpaid. Feeds the same picture the cash-flow forecast draws."""
    from routers.recurring_invoices import _advance
    today = today_str()
    horizon = (date.fromisoformat(today) + timedelta(days=max(1, days))).isoformat()
    txns = await Scoped(db.bank_transactions, user["tenant_id"]).find_list(
        {"amount_cents": {"$lt": 0}}, sort=[("txn_date", -1)], limit=5000)

    def _match(vendor: str, amount_cents: int, expected_date: str):
        token = (vendor or "").strip().lower()[:12]
        exp = date.fromisoformat(expected_date)
        for t in txns:
            if abs(t["amount_cents"]) != amount_cents:
                continue
            try:
                td = date.fromisoformat(t["txn_date"])
            except (ValueError, TypeError):
                continue
            if abs((td - exp).days) > 5:
                continue
            if token and token not in (t.get("description") or "").lower():
                continue
            return t["id"]
        return None

    expectations = []
    for t in await templates(user).find_list({"active": True}, limit=500):
        if t.get("status") == "completed":
            continue
        amount = sum(li["amount_cents"] for li in t.get("line_items", [])) \
            if t.get("line_items") else to_cents(t.get("amount", 0))
        nxt = t.get("next_run")
        guard = 0
        while nxt and nxt <= horizon and guard < 60:
            guard += 1
            if nxt >= (date.fromisoformat(today) - timedelta(days=30)).isoformat():
                mid = _match(t["vendor"], amount, nxt)
                expectations.append({
                    "source": "template", "template_id": t["id"],
                    "vendor": t["vendor"], "expected_date": nxt,
                    "amount_cents": amount,
                    "amount_min_cents": amount, "amount_max_cents": amount,
                    "status": "matched" if mid else ("overdue" if nxt < today else "expected"),
                    "matched_txn_id": mid})
            nxt = _advance(nxt, t.get("frequency", "monthly"), t.get("interval_days"))
    expectations.sort(key=lambda e: e["expected_date"])
    return {
        "as_of": today, "horizon": horizon,
        "expectations": expectations,
        "summary": {
            "expected": sum(1 for e in expectations if e["status"] == "expected"),
            "matched": sum(1 for e in expectations if e["status"] == "matched"),
            "overdue": sum(1 for e in expectations if e["status"] == "overdue"),
            "total_cents": sum(e["amount_cents"] for e in expectations),
        },
    }
