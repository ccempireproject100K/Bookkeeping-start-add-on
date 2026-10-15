"""Vendor bills (accounts payable sub-ledger). Supports partial payments, recurring
bills and vendor-credit application. Each cash movement posts to the GL."""
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
