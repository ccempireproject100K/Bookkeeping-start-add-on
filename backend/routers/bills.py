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
    # Multi-currency (#16): document currency + optional user-typed rate.
    # Amounts are denominated in `currency`; the ledger posts home currency
    # converted off the stamp taken when the bill is created.
    currency: Optional[str] = None
    fx_rate: Optional[float] = None


def _enrich(b):
    b["paid_cents"] = b.get("paid_cents", b["amount_cents"] if b.get("status") == "paid" else 0)
    b["balance_cents"] = b["amount_cents"] - b["paid_cents"]
    b["balance"] = cents_to_dollars(b["balance_cents"])
    b["amount"] = cents_to_dollars(b["amount_cents"])
    # Multi-currency: figures in the bill's own (transaction) currency.
    txn_amount = b.get("txn_amount_cents", b["amount_cents"])
    txn_paid = b.get("txn_paid_cents", b["paid_cents"])
    b["txn_currency"] = b.get("txn_currency") or b.get("home_currency") or "USD"
    b["txn_amount_cents"] = txn_amount
    b["txn_paid_cents"] = txn_paid
    b["txn_balance_cents"] = txn_amount - txn_paid
    b["txn_amount"] = cents_to_dollars(txn_amount)
    b["txn_balance"] = cents_to_dollars(txn_amount - txn_paid)
    b["is_foreign"] = b["txn_currency"] != (b.get("home_currency") or b["txn_currency"])
    return b


@router.get("")
async def list_bills(user: dict = Depends(get_current_user)):
    home = await FX.home_currency(db, user["tenant_id"])
    rows = await coll(user).find_list(sort=[("created_at", -1)], limit=500)
    for b in rows:
        b.setdefault("home_currency", home)
    return [_enrich(b) for b in rows]


@router.post("")
async def create_bill(body: BillBody, user: dict = Depends(require("ops:manage"))):
    bdate = body.bill_date or today_str()
    due = body.due_date or (date.fromisoformat(bdate) + timedelta(days=30)).isoformat()
    if body.lines:
        line_items_txn = [{"expense_code": l.expense_code, "description": l.description,
                           "amount_cents": to_cents(l.amount)} for l in body.lines]
    else:
        line_items_txn = [{"expense_code": body.expense_code, "description": body.vendor,
                           "amount_cents": to_cents(body.amount)}]
    amount_txn = sum(li["amount_cents"] for li in line_items_txn)
    if amount_txn <= 0:
        raise HTTPException(status_code=422, detail="Bill amount must be positive")
    # Stamp the rate NOW; the stamp governs this bill forever.
    home = await FX.home_currency(db, user["tenant_id"])
    stamp = await FX.resolve_stamp(db, user["tenant_id"], home, body.currency,
                                   on_date=bdate, manual_rate=body.fx_rate)
    rate = stamp["fx_rate"]
    # The ledger posts HOME currency, converted off the stamp, line by line
    # so the entry balances to the cent.
    line_items = [dict(li, amount_cents=FX.convert_cents(li["amount_cents"], rate))
                  for li in line_items_txn]
    amount = sum(li["amount_cents"] for li in line_items)
    bill_id = new_id()
    jl = [P.dr(li["expense_code"], li["amount_cents"], li["description"]) for li in line_items]
    jl.append(P.cr("2010", amount, "Accounts payable"))
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": bdate, "source": "bill", "source_ref": bill_id,
        "memo": f"Bill — {body.vendor}", "lines": jl,
    }, status="posted")
    doc = {
        "id": bill_id, "vendor": body.vendor, "vendor_id": body.vendor_id,
        "amount_cents": amount, "expense_code": line_items[0]["expense_code"],
        "line_items": line_items, "bill_date": bdate, "due_date": due, "status": "open",
        "paid_cents": 0, "payments": [], "recurring": body.recurring, "frequency": body.frequency,
        "issue_entry": entry["number"], "created_at": now_iso(),
        # Multi-currency stamp (#16). Home-currency bills stamp rate 1 and
        # every figure above is then exactly what it was before this lane.
        "home_currency": home,
        "txn_currency": stamp["txn_currency"], "fx_rate": stamp["fx_rate"],
        "fx_rate_date": stamp["fx_rate_date"], "fx_source": stamp["fx_source"],
        "fx_stale": stamp["fx_stale"],
        "txn_amount_cents": amount_txn, "txn_paid_cents": 0,
        "txn_line_items": line_items_txn,
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return _enrich(doc)


class PayBody(BaseModel):
    payment_method: str = "bank"
    amount: Optional[float] = None       # None = pay full remaining balance (document currency)
    credit_id: Optional[str] = None      # apply a vendor credit (home-currency bills only in this wave)
    pay_date: Optional[str] = None
    fx_rate: Optional[float] = None      # payment-day rate for foreign bills (manual override)


@router.post("/{bill_id}/pay")
async def pay_bill(bill_id: str, body: PayBody, user: dict = Depends(require("ops:manage"))):
    bill = await coll(user).find_one({"id": bill_id})
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    home = bill.get("home_currency") or await FX.home_currency(db, user["tenant_id"])
    txn_currency = bill.get("txn_currency") or home
    foreign = txn_currency != home
    stamp_rate = bill.get("fx_rate", "1")

    total_txn = bill.get("txn_amount_cents", bill["amount_cents"])
    paid_txn = bill.get("txn_paid_cents", bill.get("paid_cents", 0))
    balance_txn = total_txn - paid_txn
    if balance_txn <= 0:
        raise HTTPException(status_code=409, detail="Bill already fully paid")

    target_txn = to_cents(body.amount) if body.amount is not None else balance_txn
    if target_txn <= 0 or target_txn > balance_txn:
        raise HTTPException(status_code=422, detail="Invalid payment amount")

    if foreign and body.credit_id:
        raise HTTPException(
            status_code=422,
            detail="Vendor credits can't be applied to foreign-currency bills yet — settle the credit on a home-currency bill")

    pay_stamp = await FX.resolve_payment_rate(
        db, user["tenant_id"], home, bill,
        pay_date=body.pay_date or today_str(), manual_rate=body.fx_rate)
    pay_rate = pay_stamp["fx_rate"]

    credit_applied = 0
    if body.credit_id:
        cr = await credits(user).find_one({"id": body.credit_id})
        if not cr:
            raise HTTPException(status_code=404, detail="Vendor credit not found")
        credit_applied = min(cr.get("remaining_cents", 0), target_txn)

    # Home GL figures: AP clears at the bill's STAMP rate (what was booked);
    # cash leaves at the PAYMENT-day rate. Paying fewer home dollars than
    # booked is a realized gain; paying more is a realized loss. Home bills:
    # both rates are 1 and this is the pre-#16 math exactly.
    # A vendor credit already cleared its slice of AP in the GL when the
    # credit was created (vendors.py posts Dr 2010 there), so — exactly as
    # before this lane — only the CASH portion posts here.
    cash_portion_txn = target_txn - credit_applied
    ap_cleared_home = FX.convert_cents(target_txn, stamp_rate)
    ap_cash_home = FX.convert_cents(cash_portion_txn, stamp_rate)
    cash_home = FX.convert_cents(cash_portion_txn, pay_rate)
    fx_diff = ap_cash_home - cash_home

    cash = P._cash_account(body.payment_method)
    entry_number = None
    if cash_portion_txn > 0:
        jl = [P.dr("2010", ap_cash_home, "AP settled"),
              P.cr(cash, cash_home, "Payment")]
        if fx_diff > 0:
            jl.append(P.cr(FX.FX_GAIN_ACCOUNT, fx_diff,
                           f"FX gain — stamped {stamp_rate}, paid {pay_rate}"))
        elif fx_diff < 0:
            jl.append(P.dr(FX.FX_LOSS_ACCOUNT, -fx_diff,
                           f"FX loss — stamped {stamp_rate}, paid {pay_rate}"))
        entry = await L.post_journal_entry(db, user["tenant_id"], user, {
            "entry_date": body.pay_date or today_str(), "source": "ap_payment", "source_ref": bill_id,
            "memo": f"Pay bill — {bill['vendor']}", "lines": jl,
        }, status="posted")
        entry_number = entry["number"]

    if credit_applied > 0:
        await credits(user).update_one({"id": body.credit_id},
                                       {"$inc": {"remaining_cents": -credit_applied}})

    new_paid_txn = paid_txn + target_txn
    new_paid_home = bill.get("paid_cents", 0) + ap_cleared_home
    status = "paid" if new_paid_txn >= total_txn else "partial"
    pmt = {"date": (body.pay_date or today_str()), "amount_cents": target_txn,
           "home_amount_cents": ap_cleared_home,
           "cash_cents": cash_home if foreign else cash_portion_txn,
           "credit_cents": credit_applied,
           "method": body.payment_method, "entry": entry_number,
           "fx_rate_bill": str(stamp_rate), "fx_rate_payment": str(pay_rate),
           "fx_rate_payment_source": pay_stamp["fx_source"],
           "fx_gain_loss_cents": fx_diff}
    await coll(user).update_one({"id": bill_id},
                                {"$set": {"status": status, "paid_cents": new_paid_home,
                                          "txn_paid_cents": new_paid_txn},
                                 "$push": {"payments": pmt}})
    return {"status": status, "paid_cents": new_paid_home,
            "balance_cents": bill["amount_cents"] - new_paid_home,
            "txn_paid_cents": new_paid_txn, "txn_balance_cents": total_txn - new_paid_txn,
            "entry_number": entry_number, "credit_applied_cents": credit_applied,
            "fx_gain_loss_cents": fx_diff,
            "fx_rate_bill": str(stamp_rate), "fx_rate_payment": str(pay_rate)}


@router.post("/{bill_id}/generate-next")
async def generate_recurring(bill_id: str, user: dict = Depends(require("ops:manage"))):
    """Create the next occurrence of a recurring bill (manual trigger; no scheduler)."""
    bill = await coll(user).find_one({"id": bill_id})
    if not bill or not bill.get("recurring"):
        raise HTTPException(status_code=404, detail="Recurring bill not found")
    freq = bill.get("frequency") or "monthly"
    days = {"weekly": 7, "monthly": 30, "quarterly": 90}.get(freq, 30)
    bdate = (date.fromisoformat(bill["bill_date"]) + timedelta(days=days)).isoformat()
    # Regenerate in the bill's OWN currency at its own (txn) amounts when it
    # has them; the new bill takes a FRESH stamp at its own date — history
    # is never rewritten by a newer rate.
    src_lines = bill.get("txn_line_items") or bill.get("line_items", [])
    body = BillBody(vendor=bill["vendor"], vendor_id=bill.get("vendor_id"),
                    expense_code=bill["expense_code"],
                    lines=[BillLine(expense_code=li["expense_code"], description=li.get("description", ""),
                                    amount=cents_to_dollars(li["amount_cents"])) for li in src_lines] or None,
                    amount=cents_to_dollars(bill.get("txn_amount_cents", bill["amount_cents"])),
                    bill_date=bdate, recurring=True, frequency=freq,
                    currency=bill.get("txn_currency"))
    return await create_bill(body, user)
