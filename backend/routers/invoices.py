"""Invoices (accounts receivable): issue, partial payments, overpayments → customer
credits, credit application, and refunds. Each transition posts a balanced entry."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import idempotency as idem
from lib.integration_auth import actor_label, get_sync_actor
from lib import ledger as L
from lib import posting as P
from lib import fx as FX
from lib import fx_posting as FXP
from lib import currencies as C
from lib.currencies import normalize as normalize_currency

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
    currency: str = "USD"          # ISO 4217; validated on issue


def _enrich(inv):
    paid = inv.get("paid_cents", inv["total_cents"] if inv.get("status") == "paid" else 0)
    inv["paid_cents"] = paid
    inv["balance_cents"] = inv["total_cents"] - paid
    inv["balance"] = cents_to_dollars(inv["balance_cents"])
    inv["total"] = cents_to_dollars(inv["total_cents"])
    return inv


@router.get("")
async def list_invoices(user: dict = Depends(get_current_user)):
    rows = await coll(user).find_list(sort=[("created_at", -1)], limit=500)
    return [_enrich(i) for i in rows]


@router.post("")
async def issue_invoice(body: InvoiceBody,
                        user: dict = Depends(get_sync_actor),
                        request: Request = None):
    # Sync surface: user session (ops:manage) OR integration credential.
    # Idempotency-Key (sent by Momo) is enforced server-side — a retried
    # push replays the recorded response instead of issuing a duplicate
    # invoice + journal entry. See lib/idempotency.py.
    idem_key = request.headers.get("Idempotency-Key") if request else None
    sess = await idem.begin(
        tenant_id=user["tenant_id"], key=idem_key,
        endpoint="POST /invoices",
        req_hash=idem.request_hash(
            "POST", request.url.path if request else "/api/invoices",
            body.model_dump()),
        actor=actor_label(user))
    if sess.is_replay:
        return sess.replay_response()
    try:
        if idem_key:
            # Crash-window guard: a previous attempt with this key
            # issued the invoice but its response was never recorded.
            stamped = await coll(user).find_one({"sync_key": idem_key})
            if stamped:
                out = {k: v for k, v in stamped.items() if k != "sync_key"}
                return await sess.complete(_enrich(out),
                                           resource_id=stamped["id"])
        try:
            currency = normalize_currency(body.currency)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unsupported currency: {body.currency!r}")
        rev_lines = []
        subtotal = 0
        for ln in body.lines:
            amt = to_cents(ln.amount)
            subtotal += amt
            rev_lines.append({"revenue_code": ln.revenue_code, "amount_cents": amt})
        tax_cents = to_cents(body.tax)
        gross = subtotal + tax_cents
        jid = body.tax_jurisdiction_id
        if jid:
            jur = await db.tax_jurisdictions.find_one(
                {"id": jid, "tenant_id": user["tenant_id"], "active": True})
            if not jur:
                raise HTTPException(status_code=422, detail="Unknown or inactive tax jurisdiction")
        inv_id = new_id()
        entry = await L.post_journal_entry(db, user["tenant_id"], user, P.build_invoice_issue(
            revenue_lines=rev_lines, tax_cents=tax_cents, cogs_cents=0,
            gross_cents=gross, entry_date=today_str(), source_ref=inv_id,
            memo=f"Invoice — {body.client_name}", currency=currency,
        ), status="posted",
            idempotency_key=f"httpidem:{idem_key}" if idem_key else None)
        doc = {
            "id": inv_id, "client_name": body.client_name, "customer_id": body.customer_id,
            "lines": [l.model_dump() for l in body.lines],
            "subtotal_cents": subtotal, "tax_cents": tax_cents, "total_cents": gross,
            "currency": currency,
            "tax_jurisdiction_id": jid,
            "status": "issued", "paid_cents": 0, "payments": [], "due_date": body.due_date,
            "terms": body.terms, "issue_entry": entry["number"], "created_at": now_iso(),
            "issue_date": today_str(),  # accounting date; KPIs filter on this
        }
        if idem_key:
            doc["sync_key"] = idem_key
        await coll(user).insert_one(doc)
        doc.pop("_id", None)
        doc.pop("sync_key", None)
        return await sess.complete(_enrich(doc), resource_id=inv_id)
    except HTTPException as e:
        await sess.fail(e.status_code, str(e.detail))
        raise


class PayBody(BaseModel):
    payment_method: str = "bank"
    amount: Optional[float] = None       # None = full remaining balance
    credit_id: Optional[str] = None      # apply a customer credit
    pay_date: Optional[str] = None
    payment_currency: Optional[str] = None  # currency actually moved; defaults to the invoice currency


@router.post("/{invoice_id}/pay")
async def pay_invoice(invoice_id: str, body: PayBody,
                      user: dict = Depends(get_sync_actor),
                      request: Request = None):
    # Sync surface: user session (ops:manage) OR integration credential.
    # A retried payment push with the same Idempotency-Key replays the
    # recorded result instead of posting the payment twice.
    idem_key = request.headers.get("Idempotency-Key") if request else None
    sess = await idem.begin(
        tenant_id=user["tenant_id"], key=idem_key,
        endpoint="POST /invoices/pay",
        req_hash=idem.request_hash(
            "POST",
            request.url.path if request else f"/api/invoices/{invoice_id}/pay",
            body.model_dump()),
        actor=actor_label(user))
    if sess.is_replay:
        return sess.replay_response()
    try:
        return await _pay_invoice_inner(invoice_id, body, user,
                                        idem_key, sess)
    except HTTPException as e:
        await sess.fail(e.status_code, str(e.detail))
        raise


async def _pay_invoice_inner(invoice_id: str, body: PayBody, user: dict,
                             idem_key, sess):
    inv = await coll(user).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if idem_key:
        # Crash-window guard: this payment already landed (its stamp is
        # on the invoice) but the response was never recorded.
        for pmt in inv.get("payments", []):
            if pmt.get("sync_key") == idem_key and pmt.get("sync_result"):
                return await sess.complete(pmt["sync_result"],
                                           resource_id=invoice_id)
    paid = inv.get("paid_cents", 0)
    balance = inv["total_cents"] - paid
    if balance <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    inv_currency = normalize_currency(inv.get("currency") or "USD")
    try:
        pay_currency = normalize_currency(body.payment_currency or inv_currency)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unsupported currency: {body.payment_currency!r}")

    pay_date = body.pay_date or today_str()
    # body.amount is denominated in pay_currency; the JE stays in the invoice
    # currency (AR must clear in invoice terms). Same-currency path is
    # byte-identical to the pre-multi-currency behavior.
    if pay_currency == inv_currency:
        cash_received = to_cents(body.amount) if body.amount is not None else balance
        pay_minor_for_fx = cash_received
    else:
        if body.amount is not None:
            pay_minor_for_fx = C.to_minor_units(body.amount, pay_currency)
        else:
            try:
                pay_minor_for_fx = await FX.convert(
                    balance, inv_currency, pay_currency, pay_date, db=db)
            except FX.FXRateError:
                raise HTTPException(status_code=422, detail=(
                    "No exchange rate available to price this payment's currency"))
        try:
            cash_received = await FX.convert(
                pay_minor_for_fx, pay_currency, inv_currency, pay_date, db=db)
        except FX.FXRateError:
            raise HTTPException(status_code=422, detail=(
                "No exchange rate available to convert this payment"))
    if cash_received <= 0:
        raise HTTPException(status_code=422, detail="Invalid payment amount")

    credit_applied = 0
    if body.credit_id:
        c = await credits(user).find_one({"id": body.credit_id})
        if not c:
            raise HTTPException(status_code=404, detail="Customer credit not found")
        credit_applied = min(c.get("remaining_cents", 0), balance)

    cash = P._cash_account(body.payment_method)
    applied_to_ar = min(cash_received + credit_applied, balance)
    overpay = (cash_received + credit_applied) - applied_to_ar

    jl = []
    if cash_received > 0:
        jl.append(P.dr(cash, cash_received, "Invoice payment"))
    if credit_applied > 0:
        jl.append(P.dr("2210", credit_applied, "Customer credit applied"))
    jl.append(P.cr("1200", applied_to_ar, "AR cleared"))
    if overpay > 0:
        jl.append(P.cr("2210", overpay, "Overpayment → customer credit"))

    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": pay_date, "source": "invoice_paid", "source_ref": invoice_id,
        "memo": f"Payment — {inv['client_name']}", "lines": jl,
        "currency": inv_currency,
    }, status="posted",
        idempotency_key=f"httpidem:{idem_key}" if idem_key else None)

    # Realized FX on the cash portion that settled AR (best-effort; never
    # breaks the payment). Overpayments parked in 2210 carry no FX yet.
    ar_cash_doc = applied_to_ar - credit_applied
    if ar_cash_doc > 0 and cash_received > 0:
        pay_for_ar = (int(round(pay_minor_for_fx * ar_cash_doc / cash_received))
                      if pay_currency != inv_currency else ar_cash_doc)
        await FXP.post_realized_fx(
            db=db, tenant_id=user["tenant_id"], user=user, kind="invoice",
            doc=inv, pay_currency=pay_currency,
            pay_amount_minor=pay_for_ar, applied_doc_minor=ar_cash_doc,
            pay_date=pay_date)

    if credit_applied > 0:
        await credits(user).update_one({"id": body.credit_id}, {"$inc": {"remaining_cents": -credit_applied}})
    if overpay > 0:
        oc_id = new_id()
        await credits(user).insert_one({
            "id": oc_id, "client_name": inv["client_name"], "customer_id": inv.get("customer_id"),
            "amount_cents": overpay, "remaining_cents": overpay, "source": "overpayment",
            "memo": f"Overpayment on {inv.get('issue_entry')}", "entry_number": entry["number"],
            "created_at": now_iso()})

    new_paid = paid + applied_to_ar
    status = "paid" if new_paid >= inv["total_cents"] else "partial"
    pmt = {"date": pay_date, "amount_cents": applied_to_ar,
           "cash_cents": cash_received - overpay, "credit_cents": credit_applied,
           "method": body.payment_method, "pay_currency": pay_currency,
           "entry": entry["number"]}
    result = {"status": status, "paid_cents": new_paid,
              "balance_cents": inv["total_cents"] - new_paid,
              "entry_number": entry["number"],
              "credit_applied_cents": credit_applied,
              "overpayment_cents": overpay}
    if idem_key:
        # Stamp the payment with its sync key + full result so an
        # interrupted-then-retried push can be answered from the stamp.
        pmt["sync_key"] = idem_key
        pmt["sync_result"] = result
    await coll(user).update_one({"id": invoice_id}, {"$set": {"status": status, "paid_cents": new_paid},
                                                     "$push": {"payments": pmt}})
    return await sess.complete(result, resource_id=invoice_id)


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
