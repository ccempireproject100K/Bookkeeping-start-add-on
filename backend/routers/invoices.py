"""Invoices (accounts receivable): issue, partial payments, overpayments → customer
credits, credit application, and refunds. Each transition posts a balanced entry."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import idempotency as idem
from lib import developer as dev
from lib.integration_auth import actor_label, get_sync_actor
from lib import ledger as L
from lib import posting as P
from lib import sales_tax_engine as STE
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
    # Sales-tax v1: category drives the taxability matrix; `taxable`
    # (when set) is an explicit per-line override of the matrix.
    tax_category: str = "general_goods"
    taxable: Optional[bool] = None


class ShipToIn(BaseModel):
    street: str = ""
    city: str = ""
    state: str = ""
    zip5: str = ""
    zip4: Optional[str] = None


class InvoiceBody(BaseModel):
    client_name: str
    customer_id: Optional[str] = None
    lines: List[InvLine]
    tax: float = 0
    tax_jurisdiction_id: Optional[str] = None
    ship_to: Optional[ShipToIn] = None
    marketplace_facilitated: bool = False
    due_date: Optional[str] = None
    terms: Optional[str] = "Net 30"
    currency: str = "USD"          # ISO 4217; validated on issue


def _compute_invoice_tax(*, body, jur, customer, engine_lines, subtotal):
    """Sales-tax v1: the BACKEND is the calculator.

    With a jurisdiction resolved, the manual-plus engine computes tax
    from the jurisdiction's stacked rate components as of today and a
    full calculation snapshot is stored on the invoice — the caller's
    `tax` figure is demoted to a stored estimate, superseded by the
    snapshot. Without a jurisdiction, legacy behaviour stands
    (caller-supplied tax) and the snapshot says so ("manual").
    Marketplace-facilitated tax is calculated and recorded but never
    accrued to GL 2100 — the facilitator remits it, not us.
    Returns (gl_tax_cents, snapshot)."""
    estimate = to_cents(body.tax) if body.tax else None
    exemption = None
    if customer and customer.get("tax_exempt"):
        exemption = customer.get("exemption") or {"type": "exempt_customer"}
    if jur is not None:
        components, _eff = STE.resolve_components_as_of(jur, today_str())
        calc = STE.calculate_tax(
            lines=engine_lines, components=components, state=jur["state"],
            customer_exempt=bool(exemption))
        facilitator_tax = calc["tax_cents"] if body.marketplace_facilitated else 0
        gl_tax = 0 if (body.marketplace_facilitated or exemption) \
            else calc["tax_cents"]
        taxable_sales = calc["taxable_sales_cents"]
        facilitated_taxable = 0
        if body.marketplace_facilitated:
            # The facilitator owns this tax: the base leaves our
            # taxable sales and shows as facilitated instead.
            facilitated_taxable = taxable_sales
            taxable_sales = 0
        snapshot = {
            "engine": STE.ENGINE_NAME, "engine_version": STE.ENGINE_VERSION,
            "calculated_on": today_str(),
            "jurisdiction_id": jur["id"], "jurisdiction_name": jur.get("name"),
            "state": jur["state"],
            "sourcing": jur.get("sourcing") or STE.sourcing_for_state(jur["state"]),
            "components": components,
            "combined_rate_pct": calc["combined_rate_pct"],
            "lines": calc["lines"],
            "subtotal_cents": subtotal,
            "taxable_sales_cents": taxable_sales,
            "facilitated_taxable_cents": facilitated_taxable,
            "exempt_sales_cents": calc["exempt_sales_cents"],
            "nontaxable_sales_cents": calc["nontaxable_sales_cents"],
            "facilitated_sales_cents": subtotal if body.marketplace_facilitated else 0,
            "tax_cents": gl_tax,
            "facilitator_tax_cents": facilitator_tax,
            "client_estimate_cents": estimate,
            "exemption": exemption,
            "cpa_note": STE.CPA_NOTE,
        }
        return gl_tax, snapshot
    manual_tax = to_cents(body.tax)
    facilitator_tax = manual_tax if body.marketplace_facilitated else 0
    gl_tax = 0 if body.marketplace_facilitated else manual_tax
    snapshot = {
        "engine": "manual", "engine_version": STE.ENGINE_VERSION,
        "calculated_on": today_str(),
        "jurisdiction_id": None, "jurisdiction_name": None, "state": None,
        "sourcing": None, "components": [], "combined_rate_pct": None,
        "lines": [],
        "subtotal_cents": subtotal,
        "taxable_sales_cents": subtotal,
        "facilitated_taxable_cents": 0,
        "exempt_sales_cents": 0, "nontaxable_sales_cents": 0,
        "facilitated_sales_cents": subtotal if body.marketplace_facilitated else 0,
        "tax_cents": gl_tax,
        "facilitator_tax_cents": facilitator_tax,
        "client_estimate_cents": estimate,
        "exemption": exemption,
        "cpa_note": STE.CPA_NOTE,
    }
    return gl_tax, snapshot


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
        engine_lines = []
        subtotal = 0
        for ln in body.lines:
            amt = to_cents(ln.amount)
            subtotal += amt
            rev_lines.append({"revenue_code": ln.revenue_code, "amount_cents": amt})
            engine_lines.append({"name": ln.name, "amount_cents": amt,
                                 "tax_category": ln.tax_category,
                                 "taxable": ln.taxable})
        jid = body.tax_jurisdiction_id
        jur = None
        if jid:
            jur = await db.tax_jurisdictions.find_one(
                {"id": jid, "tenant_id": user["tenant_id"], "active": True})
            if not jur:
                raise HTTPException(status_code=422, detail="Unknown or inactive tax jurisdiction")
        # --- sales-tax resolution (v1): explicit jurisdiction wins;
        # otherwise a ship-to state with exactly one active jurisdiction
        # resolves automatically; otherwise the manual fallback stands.
        customer = None
        if body.customer_id:
            customer = await db.customers.find_one(
                {"id": body.customer_id, "tenant_id": user["tenant_id"]})
        ship = body.ship_to.model_dump() if body.ship_to else (
            (customer or {}).get("ship_to") or None)
        ship_state = ((ship or {}).get("state") or "").upper() or None
        if jur is None and ship_state:
            cands = await db.tax_jurisdictions.find(
                {"tenant_id": user["tenant_id"], "state": ship_state,
                 "active": True}, {"_id": 0}).to_list(10)
            if len(cands) == 1:
                jur = cands[0]
                jid = jur["id"]
        tax_cents, snapshot = _compute_invoice_tax(
            body=body, jur=jur, customer=customer,
            engine_lines=engine_lines, subtotal=subtotal)
        gross = subtotal + tax_cents
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
            "ship_to": ship,
            "marketplace_facilitated": body.marketplace_facilitated,
            "tax_snapshot": snapshot,
            "tax_reversed_cents": 0, "tax_adjustments": [],
            "status": "issued", "paid_cents": 0, "payments": [], "due_date": body.due_date,
            "terms": body.terms, "issue_entry": entry["number"], "created_at": now_iso(),
            "issue_date": today_str(),  # accounting date; KPIs filter on this
        }
        if idem_key:
            doc["sync_key"] = idem_key
        await coll(user).insert_one(doc)
        doc.pop("_id", None)
        doc.pop("sync_key", None)
        await dev.emit_event(user["tenant_id"], "invoice.created", {
            "invoice_id": inv_id, "client_name": doc.get("client_name"),
            "customer_id": doc.get("customer_id"),
            "total_cents": doc.get("total_cents"),
            "currency": doc.get("currency"), "status": doc.get("status")})
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
    await dev.emit_event(user["tenant_id"], "payment.received", {
        "invoice_id": invoice_id, "amount_cents": pmt["amount_cents"],
        "paid_cents": result["paid_cents"],
        "balance_cents": result["balance_cents"], "status": result["status"]})
    if result["status"] == "paid":
        await dev.emit_event(user["tenant_id"], "invoice.paid", {
            "invoice_id": invoice_id, "total_cents": inv["total_cents"],
            "paid_cents": result["paid_cents"]})
    return await sess.complete(result, resource_id=invoice_id)


# ---------------- Payment refunds (sync surface) ----------------
# Added by the financial-sync fix track (audit 2026-10-17): Momo
# processes Stripe refunds on its side, but until now nothing could
# reflect one here — the customer-credits refund below settles a
# *credit balance* (2210) for an owner in the UI; it is not an
# invoice-payment refund and is not reachable by an integration
# credential. This endpoint is the minimal sync-surface addition:
# same auth (get_sync_actor), same Idempotency-Key enforcement and
# crash-window stamping as /pay. See INTEGRATION_CONTRACT_ADDENDUM.md.

class InvoiceRefundBody(BaseModel):
    amount: float                    # dollars, in the invoice currency
    payment_method: str = "card"
    refund_date: Optional[str] = None
    # Sales-tax v1: dollars of sales tax to reverse with this refund,
    # when the refund accompanies a return of the sale itself. Settles
    # the GL 2100 side; pair with a credit note for the revenue side.
    tax_reversal: float = 0


@router.post("/{invoice_id}/refund")
async def refund_invoice_payment(invoice_id: str, body: InvoiceRefundBody,
                                 user: dict = Depends(get_sync_actor),
                                 request: Request = None):
    # Sync surface: user session (ops:manage) OR integration credential.
    # A retried refund push with the same Idempotency-Key replays the
    # recorded result instead of refunding twice.
    idem_key = request.headers.get("Idempotency-Key") if request else None
    sess = await idem.begin(
        tenant_id=user["tenant_id"], key=idem_key,
        endpoint="POST /invoices/refund",
        req_hash=idem.request_hash(
            "POST",
            request.url.path if request else f"/api/invoices/{invoice_id}/refund",
            body.model_dump()),
        actor=actor_label(user))
    if sess.is_replay:
        return sess.replay_response()
    try:
        return await _refund_invoice_inner(invoice_id, body, user,
                                           idem_key, sess)
    except HTTPException as e:
        await sess.fail(e.status_code, str(e.detail))
        raise


async def _refund_invoice_inner(invoice_id: str, body: InvoiceRefundBody,
                                user: dict, idem_key, sess):
    inv = await coll(user).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if idem_key:
        # Crash-window guard: this refund already landed (its stamp is
        # on the invoice) but the response was never recorded.
        for pmt in inv.get("payments", []):
            if pmt.get("sync_key") == idem_key and pmt.get("kind") == "refund" \
                    and pmt.get("sync_result"):
                return await sess.complete(pmt["sync_result"],
                                           resource_id=invoice_id)
    paid = inv.get("paid_cents", 0)
    refund_cents = to_cents(body.amount)
    if refund_cents <= 0:
        raise HTTPException(status_code=422, detail="Invalid refund amount")
    if refund_cents > paid:
        raise HTTPException(status_code=422, detail="Refund exceeds amount paid on this invoice")
    reversal_cents = to_cents(body.tax_reversal)
    if reversal_cents < 0:
        raise HTTPException(status_code=422, detail="Tax reversal cannot be negative")
    remaining_tax = int(inv.get("tax_cents", 0) or 0) - int(inv.get("tax_reversed_cents", 0) or 0)
    if reversal_cents > remaining_tax:
        raise HTTPException(status_code=422, detail="Tax reversal exceeds the sales tax remaining on this invoice")
    if reversal_cents > refund_cents:
        raise HTTPException(status_code=422, detail="Tax reversal cannot exceed the refund amount")
    inv_currency = normalize_currency(inv.get("currency") or "USD")
    refund_date = body.refund_date or today_str()
    cash = P._cash_account(body.payment_method)
    # Reverse the payment's AR settlement: cash leaves, the customer
    # owes the refunded amount again (balance reopens by exactly the
    # refund), revenue is untouched — the sale happened; the payment
    # was returned. Balanced by construction. When the refund also
    # unwinds the sale (tax_reversal > 0), the tax portion settles
    # against 2100 instead of reopening as receivable.
    jl = []
    ar_reopen = refund_cents - reversal_cents
    if ar_reopen > 0:
        jl.append(P.dr("1200", ar_reopen, "AR reopened — payment refunded"))
    if reversal_cents > 0:
        jl.append(P.dr("2100", reversal_cents, "Sales tax reversed — sale returned"))
    jl.append(P.cr(cash, refund_cents, "Refund paid out"))
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": refund_date, "source": "invoice_refund",
        "source_ref": invoice_id,
        "memo": f"Refund — {inv['client_name']}", "lines": jl,
        "currency": inv_currency,
    }, status="posted",
        idempotency_key=f"httpidem:{idem_key}" if idem_key else None)
    new_paid = paid - refund_cents
    status = "paid" if new_paid >= inv["total_cents"] else \
        ("partial" if new_paid > 0 else "issued")
    rec = {"date": refund_date, "amount_cents": -refund_cents,
           "cash_cents": -refund_cents, "credit_cents": 0,
           "method": body.payment_method, "pay_currency": inv_currency,
           "kind": "refund", "entry": entry["number"],
           "tax_reversal_cents": reversal_cents}
    result = {"status": status, "paid_cents": new_paid,
              "balance_cents": inv["total_cents"] - new_paid,
              "entry_number": entry["number"],
              "refund_cents": refund_cents,
              "tax_reversal_cents": reversal_cents}
    if idem_key:
        rec["sync_key"] = idem_key
        rec["sync_result"] = result
    update = {"$set": {"status": status, "paid_cents": new_paid},
              "$push": {"payments": rec}}
    if reversal_cents > 0:
        update["$set"]["tax_reversed_cents"] = \
            int(inv.get("tax_reversed_cents", 0) or 0) + reversal_cents
        update["$push"]["tax_adjustments"] = {
            "date": refund_date, "kind": "payment_refund",
            "cents": reversal_cents, "entry": entry["number"]}
    await coll(user).update_one({"id": invoice_id}, update)
    return await sess.complete(result, resource_id=invoice_id)


# ---------------- Customer credits / credit notes ----------------
class CreditNoteBody(BaseModel):
    client_name: str
    customer_id: Optional[str] = None
    amount: float
    revenue_code: str = "4020"
    memo: str = ""
    # Sales-tax v1: a credit note for a taxed sale (a return or
    # allowance) can reverse the sales tax with it. The reversal needs
    # its jurisdiction so the liability and worksheets can attribute
    # it; `invoice_id` links the note to its source document.
    invoice_id: Optional[str] = None
    tax_jurisdiction_id: Optional[str] = None
    sales_tax_reversal: float = 0
    taxable: bool = True


@router.get("/customer-credits")
async def list_customer_credits(user: dict = Depends(require("ledger:read"))):
    return await credits(user).find_list(sort=[("created_at", -1)], limit=300)


@router.post("/customer-credits")
async def issue_credit_note(body: CreditNoteBody, user: dict = Depends(require("ops:manage"))):
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    reversal = to_cents(body.sales_tax_reversal)
    if reversal < 0:
        raise HTTPException(status_code=422, detail="Sales-tax reversal cannot be negative")
    if reversal > 0 and not body.tax_jurisdiction_id:
        raise HTTPException(status_code=422, detail="tax_jurisdiction_id is required to reverse sales tax")
    if body.tax_jurisdiction_id:
        jur = await db.tax_jurisdictions.find_one(
            {"id": body.tax_jurisdiction_id, "tenant_id": user["tenant_id"]})
        if not jur:
            raise HTTPException(status_code=422, detail="Unknown tax jurisdiction")
    cid = new_id()
    lines = [P.dr(body.revenue_code, amount, "Revenue reduced")]
    if reversal > 0:
        lines.append(P.dr("2100", reversal, "Sales tax reversed — credit note"))
    lines.append(P.cr("2210", amount + reversal, "Customer credit issued"))
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "credit_note", "source_ref": cid,
        "memo": f"Credit note — {body.client_name}: {body.memo}".strip(),
        "lines": lines,
    }, status="posted")
    doc = {"id": cid, "client_name": body.client_name, "customer_id": body.customer_id,
           "amount_cents": amount, "remaining_cents": amount + reversal,
           "source": "credit_note",
           "memo": body.memo, "entry_number": entry["number"], "created_at": now_iso(),
           "invoice_id": body.invoice_id,
           "tax_jurisdiction_id": body.tax_jurisdiction_id,
           "tax_reversal_cents": reversal, "taxable": body.taxable,
           "note_date": today_str()}
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
