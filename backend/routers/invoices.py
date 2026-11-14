"""Invoices (accounts receivable): issue, partial payments, overpayments → customer
credits, credit application, and refunds. Each transition posts a balanced entry."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P
from lib import fx as FX

router = APIRouter(prefix="/invoices", tags=["invoices"])


def coll(user):
    return Scoped(db.invoices, user["tenant_id"])


def credits(user):
    return Scoped(db.customer_credits, user["tenant_id"])


class InvLine(BaseModel):
    kind: str = "service"
    name: str
    revenue_code: str = "4020"
    amount: float


class InvoiceBody(BaseModel):
    client_name: str
    customer_id: Optional[str] = None
    lines: List[InvLine]
    tax: float = 0
    tax_jurisdiction_id: Optional[str] = None
    due_date: Optional[str] = None
    terms: Optional[str] = "Net 30"
    # Multi-currency (#16): document currency + optional user-typed rate.
    # Line amounts and tax are denominated in `currency`; the ledger posts
    # home currency converted off the stamp taken at issue time.
    currency: Optional[str] = None
    fx_rate: Optional[float] = None


def _enrich(inv):
    paid = inv.get("paid_cents", inv["total_cents"] if inv.get("status") == "paid" else 0)
    inv["paid_cents"] = paid
    inv["balance_cents"] = inv["total_cents"] - paid
    inv["balance"] = cents_to_dollars(inv["balance_cents"])
    inv["total"] = cents_to_dollars(inv["total_cents"])
    # Multi-currency: amounts in the document's own (transaction) currency.
    # For home-currency documents these are exactly the figures above.
    txn_total = inv.get("txn_total_cents", inv["total_cents"])
    txn_paid = inv.get("txn_paid_cents", paid)
    inv["txn_currency"] = inv.get("txn_currency") or inv.get("home_currency") or "USD"
    inv["txn_total_cents"] = txn_total
    inv["txn_paid_cents"] = txn_paid
    inv["txn_balance_cents"] = txn_total - txn_paid
    inv["txn_total"] = cents_to_dollars(txn_total)
    inv["txn_balance"] = cents_to_dollars(txn_total - txn_paid)
    inv["is_foreign"] = inv["txn_currency"] != (inv.get("home_currency") or inv["txn_currency"])
    return inv


@router.get("")
async def list_invoices(user: dict = Depends(get_current_user)):
    home = await FX.home_currency(db, user["tenant_id"])
    rows = await coll(user).find_list(sort=[("created_at", -1)], limit=500)
    for i in rows:
        i.setdefault("home_currency", home)
    return [_enrich(i) for i in rows]


@router.post("")
async def issue_invoice(body: InvoiceBody, user: dict = Depends(require("ops:manage"))):
    # Amounts arrive denominated in the document (transaction) currency.
    rev_lines_txn = []
    subtotal_txn = 0
    for ln in body.lines:
        amt = to_cents(ln.amount)
        subtotal_txn += amt
        rev_lines_txn.append({"revenue_code": ln.revenue_code, "amount_cents": amt})
    tax_txn = to_cents(body.tax)
    gross_txn = subtotal_txn + tax_txn
    jid = body.tax_jurisdiction_id
    if jid:
        jur = await db.tax_jurisdictions.find_one(
            {"id": jid, "tenant_id": user["tenant_id"], "active": True})
        if not jur:
            raise HTTPException(status_code=422, detail="Unknown or inactive tax jurisdiction")
    # Stamp the rate NOW; the stamp governs this document forever.
    home = await FX.home_currency(db, user["tenant_id"])
    stamp = await FX.resolve_stamp(db, user["tenant_id"], home, body.currency,
                                   on_date=today_str(), manual_rate=body.fx_rate)
    rate = stamp["fx_rate"]
    # The ledger posts HOME currency, converted off the stamp. Converting
    # line-by-line and summing keeps the entry balanced to the cent.
    rev_lines = [{"revenue_code": rl["revenue_code"],
                  "amount_cents": FX.convert_cents(rl["amount_cents"], rate)}
                 for rl in rev_lines_txn]
    tax_cents = FX.convert_cents(tax_txn, rate)
    gross = sum(rl["amount_cents"] for rl in rev_lines) + tax_cents
    inv_id = new_id()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, P.build_invoice_issue(
        revenue_lines=rev_lines, tax_cents=tax_cents, cogs_cents=0,
        gross_cents=gross, entry_date=today_str(), source_ref=inv_id,
        memo=f"Invoice — {body.client_name}",
    ), status="posted")
    doc = {
        "id": inv_id, "client_name": body.client_name, "customer_id": body.customer_id,
        "lines": [l.model_dump() for l in body.lines],
        "subtotal_cents": sum(rl["amount_cents"] for rl in rev_lines),
        "tax_cents": tax_cents, "total_cents": gross,
        "tax_jurisdiction_id": jid,
        "status": "issued", "paid_cents": 0, "payments": [], "due_date": body.due_date,
        "terms": body.terms, "issue_entry": entry["number"], "created_at": now_iso(),
        # Multi-currency stamp (#16). Home-currency documents stamp rate 1
        # in the home currency, and every figure above is then byte-for-byte
        # what it was before this lane existed.
        "home_currency": home,
        "txn_currency": stamp["txn_currency"], "fx_rate": stamp["fx_rate"],
        "fx_rate_date": stamp["fx_rate_date"], "fx_source": stamp["fx_source"],
        "fx_stale": stamp["fx_stale"],
        "txn_subtotal_cents": subtotal_txn, "txn_tax_cents": tax_txn,
        "txn_total_cents": gross_txn, "txn_paid_cents": 0,
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return _enrich(doc)


class PayBody(BaseModel):
    payment_method: str = "bank"
    amount: Optional[float] = None       # None = full remaining balance (document currency)
    credit_id: Optional[str] = None      # apply a customer credit (home-currency invoices only in this wave)
    pay_date: Optional[str] = None
    fx_rate: Optional[float] = None      # payment-day rate for foreign invoices (manual override)


@router.post("/{invoice_id}/pay")
async def pay_invoice(invoice_id: str, body: PayBody, user: dict = Depends(require("ops:manage"))):
    inv = await coll(user).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    home = inv.get("home_currency") or await FX.home_currency(db, user["tenant_id"])
    txn_currency = inv.get("txn_currency") or home
    foreign = txn_currency != home
    stamp_rate = inv.get("fx_rate", "1")

    # Document-currency (txn) bookkeeping: the customer owes and pays in the
    # invoice's own currency. Home (GL) figures derive from the two rates.
    total_txn = inv.get("txn_total_cents", inv["total_cents"])
    paid_txn = inv.get("txn_paid_cents", inv.get("paid_cents", 0))
    balance_txn = total_txn - paid_txn
    if balance_txn <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")

    received_txn = to_cents(body.amount) if body.amount is not None else balance_txn
    if received_txn <= 0:
        raise HTTPException(status_code=422, detail="Invalid payment amount")

    if foreign and body.credit_id:
        raise HTTPException(
            status_code=422,
            detail="Customer credits can't be applied to foreign-currency invoices yet — settle the credit on a home-currency invoice")

    # Payment-day rate (home invoices: always the rate-1 home stamp).
    pay_stamp = await FX.resolve_payment_rate(
        db, user["tenant_id"], home, inv,
        pay_date=body.pay_date or today_str(), manual_rate=body.fx_rate)
    pay_rate = pay_stamp["fx_rate"]

    credit_applied = 0
    if body.credit_id:
        c = await credits(user).find_one({"id": body.credit_id})
        if not c:
            raise HTTPException(status_code=404, detail="Customer credit not found")
        credit_applied = min(c.get("remaining_cents", 0), balance_txn)

    cash = P._cash_account(body.payment_method)
    cash_received_txn = received_txn
    applied_txn = min(cash_received_txn + credit_applied, balance_txn)
    overpay_txn = (cash_received_txn + credit_applied) - applied_txn

    # ---- Home-currency GL figures ----
    # Cash is worth what it converts to at the PAYMENT-day rate; AR clears
    # at the invoice STAMP rate (that's what was booked). The difference is
    # realized FX gain/loss. For home invoices both rates are 1 and every
    # figure below reduces to the pre-#16 math exactly.
    cash_home = FX.convert_cents(cash_received_txn, pay_rate)
    ar_cleared_home = FX.convert_cents(applied_txn, stamp_rate)
    overpay_home = FX.convert_cents(overpay_txn, pay_rate) if overpay_txn > 0 else 0
    fx_diff = cash_home + credit_applied - ar_cleared_home - overpay_home

    jl = []
    if cash_home > 0:
        jl.append(P.dr(cash, cash_home, "Invoice payment"))
    if credit_applied > 0:
        jl.append(P.dr("2210", credit_applied, "Customer credit applied"))
    jl.append(P.cr("1200", ar_cleared_home, "AR cleared"))
    if overpay_home > 0:
        jl.append(P.cr("2210", overpay_home, "Overpayment → customer credit"))
    if fx_diff > 0:
        jl.append(P.cr(FX.FX_GAIN_ACCOUNT, fx_diff,
                       f"FX gain — stamped {stamp_rate}, paid {pay_rate}"))
    elif fx_diff < 0:
        jl.append(P.dr(FX.FX_LOSS_ACCOUNT, -fx_diff,
                       f"FX loss — stamped {stamp_rate}, paid {pay_rate}"))

    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": body.pay_date or today_str(), "source": "invoice_paid", "source_ref": invoice_id,
        "memo": f"Payment — {inv['client_name']}", "lines": jl,
    }, status="posted")

    if credit_applied > 0:
        await credits(user).update_one({"id": body.credit_id}, {"$inc": {"remaining_cents": -credit_applied}})
    if overpay_home > 0:
        oc_id = new_id()
        await credits(user).insert_one({
            "id": oc_id, "client_name": inv["client_name"], "customer_id": inv.get("customer_id"),
            "amount_cents": overpay_home, "remaining_cents": overpay_home, "source": "overpayment",
            "memo": f"Overpayment on {inv.get('issue_entry')}", "entry_number": entry["number"],
            "created_at": now_iso()})

    new_paid_txn = paid_txn + applied_txn
    new_paid_home = inv.get("paid_cents", 0) + ar_cleared_home
    status = "paid" if new_paid_txn >= total_txn else "partial"
    pmt = {"date": body.pay_date or today_str(), "amount_cents": applied_txn,
           "home_amount_cents": ar_cleared_home,
           # Cash that landed on this invoice (home cents). For home-currency
           # invoices this is exactly the legacy received-minus-overpay figure.
           "cash_cents": (cash_home - overpay_home) if foreign else (cash_received_txn - overpay_txn),
           "credit_cents": credit_applied,
           "method": body.payment_method, "entry": entry["number"],
           # The two rates, shown on the payment record (owner's rule).
           "fx_rate_invoice": str(stamp_rate), "fx_rate_payment": str(pay_rate),
           "fx_rate_payment_source": pay_stamp["fx_source"],
           "fx_gain_loss_cents": fx_diff}
    await coll(user).update_one({"id": invoice_id},
                                {"$set": {"status": status, "paid_cents": new_paid_home,
                                          "txn_paid_cents": new_paid_txn},
                                 "$push": {"payments": pmt}})
    return {"status": status, "paid_cents": new_paid_home,
            "balance_cents": inv["total_cents"] - new_paid_home,
            "txn_paid_cents": new_paid_txn, "txn_balance_cents": total_txn - new_paid_txn,
            "entry_number": entry["number"], "credit_applied_cents": credit_applied,
            "overpayment_cents": overpay_home,
            "fx_gain_loss_cents": fx_diff,
            "fx_rate_invoice": str(stamp_rate), "fx_rate_payment": str(pay_rate)}


# ---------------- Customer credits / credit notes ----------------
class CreditNoteBody(BaseModel):
    client_name: str
    customer_id: Optional[str] = None
    amount: float
    revenue_code: str = "4020"
    memo: str = ""


@router.get("/customer-credits")
async def list_customer_credits(user: dict = Depends(require("ledger:read"))):
    return await credits(user).find_list(sort=[("created_at", -1)], limit=300)


@router.post("/customer-credits")
async def issue_credit_note(body: CreditNoteBody, user: dict = Depends(require("ops:manage"))):
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    cid = new_id()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "credit_note", "source_ref": cid,
        "memo": f"Credit note — {body.client_name}: {body.memo}".strip(),
        "lines": [P.dr(body.revenue_code, amount, "Revenue reduced"),
                  P.cr("2210", amount, "Customer credit issued")],
    }, status="posted")
    doc = {"id": cid, "client_name": body.client_name, "customer_id": body.customer_id,
           "amount_cents": amount, "remaining_cents": amount, "source": "credit_note",
           "memo": body.memo, "entry_number": entry["number"], "created_at": now_iso()}
    await credits(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


class RefundBody(BaseModel):
    payment_method: str = "bank"


@router.post("/customer-credits/{credit_id}/refund")
async def refund_credit(credit_id: str, body: RefundBody, user: dict = Depends(require("ops:manage"))):
    c = await credits(user).find_one({"id": credit_id})
    if not c:
        raise HTTPException(status_code=404, detail="Credit not found")
    amount = c.get("remaining_cents", 0)
    if amount <= 0:
        raise HTTPException(status_code=409, detail="No remaining credit to refund")
    cash = P._cash_account(body.payment_method)
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "refund", "source_ref": credit_id,
        "memo": f"Refund — {c['client_name']}",
        "lines": [P.dr("2210", amount, "Customer credit refunded"), P.cr(cash, amount, "Refund paid")],
    }, status="posted")
    await credits(user).update_one({"id": credit_id}, {"$set": {"remaining_cents": 0, "status": "refunded"}})
    return {"refunded_cents": amount, "entry_number": entry["number"]}
