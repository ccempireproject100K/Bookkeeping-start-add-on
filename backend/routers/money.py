"""Money core: transactions, invoices, P&L, dashboard and workspace settings."""

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from lib.auth import Principal, get_principal, require
from lib.dates import business_date_filter, now_iso, today_iso
from lib.db import db
from lib.audit import audit
from lib.money import D, create_txn, invoice_amounts, line_total, money, msum
from lib.repo import Scoped
from lib.stock import barber_name, default_location, lot_status, stock_status
from models.money import (CategoryAmount, Dashboard, DayMoney, Invoice, InvoiceIn, InvoiceStatusIn, InventoryWidget,
                          PaymentIn, Pnl, RefundIn, RetailPnl, Transaction, TransactionIn, WorkspaceSettings)

router = APIRouter()


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "tenant_id"}


# ---------- Transactions ----------
@router.get("/transactions", response_model=list[Transaction])
async def list_txns(kind: str = "", start: str = "", end: str = "", p: Principal = Depends(require("txn:read"))):
    f: dict = {}
    if kind:
        f["kind"] = kind
    if start or end:
        f["date"] = {**({"$gte": start} if start else {}), **({"$lte": end} if end else {})}
    return [Transaction(**_strip(d)) for d in await Scoped("transactions", p).find(f, sort=[("date", -1), ("created_at", -1)], limit=2000)]


@router.post("/transactions", response_model=Transaction)
async def add_txn(body: TransactionIn, p: Principal = Depends(require("txn:write"))):
    doc = await create_txn(p, body.kind, body.category.strip(), body.amount, body.date, body.description,
                           barber_id=body.barber_id, payment_method=body.payment_method)
    await audit(p, "txn.create", "transaction", doc["id"], {"kind": body.kind, "amount": doc["amount"], "category": doc["category"]})
    return Transaction(**doc)


@router.delete("/transactions/{id}")
async def delete_txn(id: str, p: Principal = Depends(require("txn:write"))):
    doc = await Scoped("transactions", p).find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Transaction not found")
    if doc["source"] != "manual":
        raise HTTPException(400, "Linked transactions come from inventory or invoices and can't be deleted here")
    from routers.ledger import reverse_txn  # "delete" = traceable reversal; posted rows are never removed
    await reverse_txn(p, id, f"removed by {p.name}", ("manual",))
    return {"ok": True}


# ---------- Invoices ----------
async def _next_invoice_no(p: Principal) -> str:
    t = await db.tenants.find_one_and_update({"id": p.tenant_id}, {"$inc": {"invoice_seq": 1}}, return_document=ReturnDocument.AFTER)
    return f"INV-{t.get('invoice_seq', 1):04d}"


@router.get("/invoices", response_model=list[Invoice])
async def list_invoices(status: str = "", p: Principal = Depends(require("invoice:read"))):
    f = {"status": status} if status else {}
    return [Invoice(**_strip(d)) for d in await Scoped("invoices", p).find(f, sort=[("created_at", -1)])]


@router.get("/invoices/{id}", response_model=Invoice)
async def get_invoice(id: str, p: Principal = Depends(require("invoice:read"))):
    doc = await Scoped("invoices", p).find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Invoice not found")
    return Invoice(**_strip(doc))


@router.post("/invoices", response_model=Invoice)
async def create_invoice(body: InvoiceIn, p: Principal = Depends(require("invoice:write"))):
    lines = []
    for l in body.lines:
        d = l.model_dump()
        d["movement_id"] = None
        d["unit_cost"] = 0
        if d.get("kind") == "product" and d.get("product_id"):
            prod = await Scoped("products", p).find_one({"id": d["product_id"]})
            if not prod:
                raise HTTPException(404, f"Product {d['product_id']} not found")
            if prod["type"] != "retail":
                raise HTTPException(400, f"{prod['name']} is a supply and can't be sold on an invoice")
            d["unit_cost"] = prod["unit_cost"]
        else:
            d["kind"] = "service"
            d["product_id"] = None
        lines.append(d)
    amt = invoice_amounts(lines, body.discount_amount, body.tax_rate, body.tip_amount)
    doc = {"id": str(uuid.uuid4()), "number": await _next_invoice_no(p), "client_name": body.client_name.strip(),
           "date": body.date, "due_date": body.due_date, "status": "draft", "lines": lines,
           "barber_id": body.barber_id, "barber_name": await barber_name(p, body.barber_id), "notes": body.notes,
           "amount_paid": 0, "payments": [], "issued_at": None, "paid_at": None, "created_at": now_iso(), **amt}
    await audit(p, "invoice.create", "invoice", doc["id"], {"number": doc["number"], "total": doc["total"]})
    return Invoice(**_strip(await Scoped("invoices", p).insert(doc)))


async def _issue_invoice(p: Principal, inv: dict) -> dict:
    """draft -> sent. Fulfils stocked product lines (deducts stock + books COGS) and posts the
    accrual entry: DR A/R, CR revenue (service/product), CR sales tax, CR tips, DR discounts."""
    from lib.bookkeeping import post_entry
    from lib.stock import load_product, post_movement
    invs = Scoped("invoices", p)
    lines = [dict(l) for l in inv["lines"]]
    for l in lines:
        if l.get("kind") == "product" and l.get("product_id") and not l.get("movement_id"):
            prod = await load_product(p, l["product_id"])
            if prod["tracking_mode"] == "serial":
                raise HTTPException(400, f"{prod['name']} is serialized — sell it from Quick sell, then add the line")
            mv = await post_movement(p, prod, "sale", -int(l["quantity"]), unit_price=l["unit_price"],
                                     barber_id=inv.get("barber_id"), note=f"Invoice {inv['number']}",
                                     linked_invoice_id=inv["id"])
            l["movement_id"] = mv["id"]
            l["unit_cost"] = round(mv["cogs"] / l["quantity"], 4) if l["quantity"] else 0
    service_sub = msum(line_total(l["quantity"], l["unit_price"]) for l in lines if l.get("kind") != "product")
    product_sub = msum(line_total(l["quantity"], l["unit_price"]) for l in lines if l.get("kind") == "product")
    signed = [("1100", inv["total"]), ("4000", -service_sub), ("4010", -product_sub),
              ("4900", inv["discount_amount"]), ("2100", -inv["tax_amount"]), ("2200", -inv["tip_amount"])]
    await post_entry(p, inv["date"], f"Invoice {inv['number']} · {inv['client_name']}", signed,
                     source="invoice", ref=f"inv:{inv['id']}:issue")
    updated = await invs.update({"id": inv["id"], "status": inv["status"]},
                                {"$set": {"status": "sent", "issued_at": now_iso(), "lines": lines}})
    if not updated:
        raise HTTPException(409, "Invoice changed concurrently, refresh and retry")
    await audit(p, "invoice.issue", "invoice", inv["id"], {"number": inv["number"], "total": inv["total"]})
    return updated


async def _record_payment(p: Principal, inv: dict, amount: float, method: str, processor: str, fee: float,
                          date: str, note: str) -> dict:
    from lib.bookkeeping import MONEY_ACCT, post_entry
    invs = Scoped("invoices", p)
    amount, fee = money(amount), money(fee)
    if amount > money(D(inv["balance_due"]) + Decimal("0.005")):
        raise HTTPException(400, f"Payment {amount} exceeds balance due {inv['balance_due']}")
    pid = str(uuid.uuid4())
    acct = MONEY_ACCT.get(method, "1000")
    await post_entry(p, date, f"Payment · Invoice {inv['number']}", [(acct, amount - fee), ("6200", fee), ("1100", -amount)],
                     source="invoice", ref=f"inv:{inv['id']}:pay:{pid}")
    payment = {"id": pid, "date": date, "amount": amount, "method": method, "processor": processor, "fee": fee,
               "note": note, "kind": "payment", "created_at": now_iso()}
    paid = money(D(inv["amount_paid"]) + D(amount))
    balance = money(D(inv["total"]) - D(paid))
    patch: dict = {"amount_paid": paid, "balance_due": balance}
    if balance <= 0.005:
        patch["status"] = "paid"
        patch["paid_at"] = now_iso()
    updated = await invs.update({"id": inv["id"]}, {"$set": patch, "$push": {"payments": payment}})
    if patch.get("status") == "paid":
        await _recognize_invoice_cash(p, updated, method)
    await audit(p, "invoice.payment", "invoice", inv["id"], {"number": inv["number"], "amount": amount, "fee": fee, "processor": processor})
    return updated


async def _recognize_invoice_cash(p: Principal, inv: dict, method: str) -> None:
    """Legacy cash-basis P&L: when an invoice is fully paid, record income transactions for the
    net service / product revenue (tax + tips are liabilities, not income). These are invoice-sourced
    so they feed the cash P&L / dashboard but are NOT re-mirrored into the journal."""
    ss = msum(line_total(l["quantity"], l["unit_price"]) for l in inv["lines"] if l.get("kind") != "product")
    ps = msum(line_total(l["quantity"], l["unit_price"]) for l in inv["lines"] if l.get("kind") == "product")
    sub = ss + ps
    if sub <= 0:
        return
    disc = inv.get("discount_amount", 0)
    svc_net = money(D(ss) - D(disc) * D(ss) / D(sub)) if sub else 0
    prod_net = money(D(ps) - D(disc) * D(ps) / D(sub)) if sub else 0
    day = today_iso(p.settings.get("timezone"))
    if svc_net > 0:
        await create_txn(p, "income", "Services", svc_net, day, f"{inv['number']} · {inv['client_name']}",
                         source="invoice", barber_id=inv.get("barber_id"), invoice_id=inv["id"], payment_method=method)
    if prod_net > 0:
        await create_txn(p, "income", "Retail sales", prod_net, day, f"{inv['number']} · products",
                         source="invoice", barber_id=inv.get("barber_id"), invoice_id=inv["id"], payment_method=method,
                         movement_ids=[l["movement_id"] for l in inv["lines"] if l.get("movement_id")])


@router.post("/invoices/{id}/issue", response_model=Invoice)
async def issue_invoice(id: str, p: Principal = Depends(require("invoice:write"))):
    inv = await Scoped("invoices", p).find_one({"id": id})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] != "draft":
        raise HTTPException(400, f"Only draft invoices can be issued (this one is {inv['status']})")
    return Invoice(**_strip(await _issue_invoice(p, inv)))


@router.post("/invoices/{id}/payments", response_model=Invoice)
async def add_payment(id: str, body: PaymentIn, p: Principal = Depends(require("invoice:write"))):
    inv = await Scoped("invoices", p).find_one({"id": id})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] == "draft":
        inv = await _issue_invoice(p, inv)
    if inv["status"] in ("void", "paid"):
        raise HTTPException(400, f"Invoice is {inv['status']} — no balance to pay")
    date = body.date or today_iso(p.settings.get("timezone"))
    return Invoice(**_strip(await _record_payment(p, inv, body.amount, body.method, body.processor, body.fee, date, body.note)))


@router.post("/invoices/{id}/refund", response_model=Invoice)
async def refund_invoice(id: str, body: RefundIn, p: Principal = Depends(require("invoice:write"))):
    """A refund reduces revenue (contra-revenue Refunds & Returns) and pays cash back. The invoice stays
    paid; returned goods go back to stock via a stock return. Appears in the journal and income statement."""
    from lib.bookkeeping import MONEY_ACCT, post_entry
    invs = Scoped("invoices", p)
    inv = await invs.find_one({"id": id})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    amount = money(body.amount)
    refundable = money(D(inv["amount_paid"]) - D(inv.get("refunded", 0)))
    if amount > money(D(refundable) + Decimal("0.005")):
        raise HTTPException(400, f"Refund {amount} exceeds refundable amount {refundable}")
    rid = str(uuid.uuid4())
    acct = MONEY_ACCT.get(body.method, "1000")
    day = today_iso(p.settings.get("timezone"))
    await post_entry(p, day, f"Refund · Invoice {inv['number']} — {body.reason}",
                     [("4910", amount), (acct, -amount)], source="invoice", ref=f"inv:{id}:refund:{rid}")
    payment = {"id": rid, "date": day, "amount": -amount, "method": body.method, "processor": "recorded",
               "fee": 0, "note": body.reason, "kind": "refund", "created_at": now_iso()}
    await create_txn(p, "expense", "Retail refunds", amount, day, f"Refund {inv['number']}",
                     source="invoice", invoice_id=id, payment_method=body.method)
    updated = await invs.update({"id": id}, {"$set": {"refunded": money(D(inv.get("refunded", 0)) + D(amount))},
                                             "$push": {"payments": payment}})
    await audit(p, "invoice.refund", "invoice", id, {"number": inv["number"], "amount": amount, "reason": body.reason})
    return Invoice(**_strip(updated))


@router.post("/invoices/{id}/status", response_model=Invoice)
async def invoice_status(id: str, body: InvoiceStatusIn, p: Principal = Depends(require("invoice:write"))):
    invs = Scoped("invoices", p)
    doc = await invs.find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Invoice not found")
    if body.status == "sent":
        if doc["status"] != "draft":
            raise HTTPException(400, f"Cannot change a {doc['status']} invoice to sent")
        return Invoice(**_strip(await _issue_invoice(p, doc)))
    if body.status == "paid":
        if doc["status"] == "draft":
            doc = await _issue_invoice(p, doc)
        if doc["status"] != "sent":
            raise HTTPException(400, f"Cannot mark a {doc['status']} invoice paid")
        return Invoice(**_strip(await _record_payment(p, doc, doc["balance_due"], "cash", "recorded", 0,
                                                      today_iso(p.settings.get("timezone")), "Marked paid")))
    if body.status == "void":
        if doc["status"] == "paid" or doc.get("amount_paid", 0) > 0:
            raise HTTPException(400, "This invoice has payments — record a refund instead of voiding")
        if doc["status"] == "void":
            raise HTTPException(400, "Already void")
        if doc["status"] == "sent":  # reverse the issue entry and return any fulfilled stock
            from lib.bookkeeping import reverse_entry
            from lib.stock import load_product, post_movement
            entry = await db.journal_entries.find_one({"tenant_id": p.tenant_id, "ref": f"inv:{id}:issue"}, {"_id": 0})
            if entry and not entry.get("reversal_of"):
                await reverse_entry(p, entry["id"], f"void {doc['number']}")
            for l in doc["lines"]:
                if l.get("movement_id") and l.get("product_id"):
                    prod = await load_product(p, l["product_id"])
                    await post_movement(p, prod, "return", int(l["quantity"]), note=f"Void invoice {doc['number']}")
        updated = await invs.update({"id": id, "status": doc["status"]}, {"$set": {"status": "void", "balance_due": 0}})
        if not updated:
            raise HTTPException(409, "Invoice changed concurrently, refresh and retry")
        await audit(p, "invoice.void", "invoice", id, {"number": doc["number"]})
        return Invoice(**_strip(updated))
    raise HTTPException(400, f"Unsupported status {body.status}")


# ---------- P&L ----------
def _next_day(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


async def retail_numbers(p: Principal, start: str, end: str) -> tuple[float, float, float]:
    """(revenue, cogs, supply_usage_cost) from the movement ledger for [start, end]."""
    mvs = await Scoped("movements", p).find({"type": {"$in": ["sale", "return", "use"]},
                                             **business_date_filter(start, end)})
    revenue = msum(line_total(-m["quantity"], m.get("unit_price") or 0) for m in mvs if m["type"] != "use")
    cogs = msum(m["cogs"] for m in mvs if m["type"] != "use")
    usage = msum(m["cogs"] for m in mvs if m["type"] == "use")
    return revenue, cogs, usage


@router.get("/reports/pnl", response_model=Pnl)
async def pnl(start: str = "", end: str = "", basis: str = "cash", p: Principal = Depends(require("report:read"))):
    """cash: money in/out by transaction date — inventory purchases are expenses when recorded, COGS is NOT deducted.
    accrual: inventory purchases are excluded (they're stock, an asset) and COGS + supplies used are deducted instead.
    Never both — that would double-count inventory. Invoice income is recognised when marked paid in both bases."""
    if basis not in ("cash", "accrual"):
        raise HTTPException(422, "basis must be cash or accrual")
    today = today_iso(p.settings.get("timezone"))
    start, end = start or today[:8] + "01", end or today
    txns = await Scoped("transactions", p).find({"date": {"$gte": start, "$lte": end}})
    inc: dict[str, list] = defaultdict(list)
    exp: dict[str, list] = defaultdict(list)
    excluded: list[float] = []
    for t in txns:
        if basis == "accrual" and t["kind"] == "expense" and t.get("capitalized"):
            excluded.append(t["amount"])
            continue
        (inc if t["kind"] == "income" else exp)[t["category"]].append(t["amount"])
    _, cogs, usage = await retail_numbers(p, start, end)
    cogs_cat = {"Cost of goods sold": [cogs], "Supplies used in services": [usage]} if basis == "accrual" else {}
    for k, v in cogs_cat.items():
        if v[0]:
            exp[k] = v
    total_inc = msum(msum(v) for v in inc.values())
    total_exp = msum(msum(v) for v in exp.values())
    retail = None
    if p.settings.get("inventory_enabled"):
        sales = money(msum(inc.get("Retail sales", [])) - msum(exp.get("Retail refunds", [])))
        retail = RetailPnl(retail_sales=sales, retail_cogs=cogs, retail_gross_profit=money(sales - cogs), supply_usage_cost=usage)
    srt = lambda d: sorted([CategoryAmount(category=k, amount=msum(v)) for k, v in d.items()], key=lambda c: -c.amount)  # noqa: E731
    notes = (["Cash basis: stock purchases count as expenses when recorded; cost of goods sold is shown for reference only and is not deducted."]
             if basis == "cash" else
             ["Accrual basis: stock purchases are excluded (they are inventory, an asset) and cost of goods sold + supplies used are deducted.",
              "Invoice income is recognised when the invoice is marked paid (unpaid invoices are not income)."])
    notes.append("Reversals appear as negative amounts in the category of the entry they correct.")
    return Pnl(start=start, end=end, income=srt(inc), expenses=srt(exp), total_income=total_inc, total_expenses=total_exp,
               net_profit=money(total_inc - total_exp), retail=retail, basis=basis,  # type: ignore[arg-type]
               cogs_deducted=money(cogs + usage) if basis == "accrual" else 0, inventory_purchases_excluded=msum(excluded), notes=notes)


# ---------- Dashboard ----------
@router.get("/dashboard", response_model=Dashboard)
async def dashboard(p: Principal = Depends(get_principal)):
    today = today_iso(p.settings.get("timezone"))
    month_start = today[:8] + "01"
    d0 = (date.fromisoformat(today) - timedelta(days=29)).isoformat()
    txns = await Scoped("transactions", p).find({"date": {"$gte": min(d0, month_start)}}, sort=[("date", -1), ("created_at", -1)])
    month = [t for t in txns if t["date"] >= month_start]
    income = msum(t["amount"] for t in month if t["kind"] == "income")
    expenses = msum(t["amount"] for t in month if t["kind"] == "expense")
    days: dict[str, list[float]] = {(date.fromisoformat(d0) + timedelta(days=i)).isoformat(): [0.0, 0.0] for i in range(30)}
    for t in txns:
        if t["date"] in days:
            days[t["date"]][0 if t["kind"] == "income" else 1] += t["amount"]
    outstanding = msum(i.get("balance_due", i["total"]) for i in await Scoped("invoices", p).find({"status": {"$in": ["draft", "sent"]}}))
    widget = None
    if p.settings.get("inventory_enabled"):
        products = await Scoped("products", p).find({"active": True})
        revenue, cogs, _ = await retail_numbers(p, month_start, today)
        warn = p.settings["expiry_warning_days"]
        lots = await Scoped("lots", p).find({"quantity_on_hand": {"$gt": 0}, "expiry_date": {"$ne": None}})
        widget = InventoryWidget(
            low_stock_count=sum(1 for d in products if stock_status(d["quantity_on_hand"], d["reorder_point"]) != "in_stock"),
            expiring_count=sum(1 for l in lots if lot_status(l, warn, today) in ("expiring", "expired")),
            retail_sales_month=revenue, retail_profit_month=round(revenue - cogs, 2),
            stock_value=msum(D(max(d["quantity_on_hand"], 0)) * D(d["unit_cost"]) for d in products),
        )
    return Dashboard(month_start=month_start, income_month=income, expenses_month=expenses, net_month=round(income - expenses, 2),
                     outstanding_invoices=round(outstanding, 2),
                     trend=[DayMoney(date=k, income=round(v[0], 2), expenses=round(v[1], 2)) for k, v in days.items()],
                     recent_transactions=[Transaction(**_strip(t)) for t in txns[:8]], inventory=widget)


# ---------- Settings ----------
@router.get("/settings", response_model=WorkspaceSettings)
async def get_settings(p: Principal = Depends(require("settings:read"))):
    return WorkspaceSettings(name=p.tenant_name, **{k: v for k, v in p.settings.items() if k in WorkspaceSettings.model_fields})


@router.put("/settings", response_model=WorkspaceSettings)
async def put_settings(body: WorkspaceSettings, p: Principal = Depends(require("settings:write"))):
    data = body.model_dump()
    name = data.pop("name").strip()
    # Old clients omit international fields; preserve settings rather than resetting them.
    for key in ("currency", "timezone", "locale"):
        if key not in body.model_fields_set:
            data[key] = p.settings.get(key, data[key])
    if data["currency"] != p.settings.get("currency", "USD"):
        raise HTTPException(409, "Workspace currency is fixed at creation. Create a new workspace for a different currency; historical amounts are not converted.")
    for flag, feat in (("inventory_enabled", "inventory"), ("ai_enabled", "ai")):
        if data[flag] and not p.entitlements.get(feat):
            if not p.settings.get(flag):
                raise HTTPException(402, f"The {feat} add-on needs an active subscription — start one in Settings → Plan & billing")
            data[flag] = False
    await db.tenants.update_one({"id": p.tenant_id}, {"$set": {"name": name, "settings": data}})
    if data["inventory_enabled"]:
        await default_location(p)
    await audit(p, "settings.update", "tenant", p.tenant_id, {k: v for k, v in data.items()})
    return WorkspaceSettings(name=name, **data)
