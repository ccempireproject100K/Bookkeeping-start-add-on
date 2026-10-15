import csv
import io
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from lib.auth import Principal, require
from lib.dates import business_date_filter, today_iso
from lib.db import db
from lib.money import create_txn, line_total, money, msum
from lib.ops import begin_op, commit_op, fault, idem_fail, idem_finish, idem_start
from lib.repo import Scoped
from lib.stock import load_product, post_movement
from models.inventory import (AdjustIn, CheckoutIn, CheckoutResult, CountIn, CountResult, CountSkip, Movement, RestockIn,
                              ReturnIn, SellIn, UseIn)

router = APIRouter()


def _range_filter(start: str, end: str) -> dict:
    f: dict = {}
    if start:
        f["$gte"] = start
    if end:
        f["$lt"] = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    return f


async def _movements(p: Principal, product_id: str, lot_id: str, serial_unit_id: str, type: str, barber_id: str,
                     start: str, end: str, limit: int) -> list[dict]:
    f: dict = {}
    for k, v in (("product_id", product_id), ("lot_id", lot_id), ("type", type), ("barber_id", barber_id)):
        if v:
            f[k] = v
    if serial_unit_id:
        f["serial_unit_ids"] = serial_unit_id
    f.update(business_date_filter(start, end))
    return await Scoped("movements", p).find(f, sort=[("created_at", -1)], limit=min(limit, 5000))


@router.get("/movements", response_model=list[Movement])
async def list_movements(product_id: str = "", lot_id: str = "", serial_unit_id: str = "", type: str = "",
                         barber_id: str = "", start: str = "", end: str = "", limit: int = 500,
                         p: Principal = Depends(require("product:read"))):
    return [Movement(**d) for d in await _movements(p, product_id, lot_id, serial_unit_id, type, barber_id, start, end, limit)]


@router.get("/movements/export.csv")
async def export_movements(start: str = "", end: str = "", p: Principal = Depends(require("product:read"))):
    rows = await _movements(p, "", "", "", "", "", start, end, 5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "product", "sku", "type", "quantity", "unit_cost", "unit_price", "cogs", "lot", "serials",
                "reason", "note", "barber", "performed_by", "transaction_id", "invoice_id"])
    for m in rows:
        w.writerow([m["created_at"], m["product_name"], m.get("product_sku") or "", m["type"], m["quantity"],
                    m.get("unit_cost") or "", m.get("unit_price") or "", m["cogs"], m.get("lot_number") or "",
                    " ".join(m.get("serial_numbers") or []), m["reason"], m["note"], m.get("barber_name") or "",
                    m["performed_by_name"], m.get("linked_transaction_id") or "", m.get("linked_invoice_id") or ""])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="movements.csv"'})


async def _guarded(p: Principal, key: str | None, scope: str, model, fn):
    """Idempotency wrapper: same key -> stored result. HTTP errors free the key (safe to retry);
    a crash leaves it pending until recover_ops() resolves the journal, so a retry can never double-post."""
    prior = await idem_start(p, key, scope)
    if prior is not None:
        return model(**prior)
    try:
        res = await fn()
    except HTTPException:
        await idem_fail(p, key)
        raise
    await idem_finish(p, key, res.model_dump())
    return res


async def _with_op(p: Principal, kind: str, plan: dict, key: str | None, steps):
    op_id = str(uuid.uuid4())
    await begin_op(p, op_id, kind, {**plan, "idempotency_key": key})
    try:
        res = await steps(op_id)
    except HTTPException:
        await db.ops.update_one({"id": op_id, "status": "pending"}, {"$set": {"status": "aborted"}})
        raise
    await commit_op(op_id)
    return res


IdemKey = Header(default=None, alias="Idempotency-Key")
FaultHdr = Header(default=None, alias="X-Test-Fault")


@router.post("/stock/restock", response_model=Movement)
async def restock(body: RestockIn, p: Principal = Depends(require("stock:write")), idem: str | None = IdemKey, flt: str | None = FaultHdr):
    product = await load_product(p, body.product_id)
    amount = line_total(body.quantity, body.unit_cost)
    txn_id = str(uuid.uuid4()) if body.record_expense and amount > 0 else None
    cat = p.settings["expense_category_retail" if product["type"] == "retail" else "expense_category_supply"]
    desc = f"Restock {body.quantity} × {product['name']}" + (f" ({body.supplier_name})" if body.supplier_name else "")
    txn_plan = {"txn_id": txn_id, "kind": "expense", "category": cat, "amount": amount, "date": today_iso(p.settings.get("timezone")), "description": desc,
                "capitalized": True} if txn_id else None

    async def steps(op_id: str):
        mv = await post_movement(p, product, "restock", body.quantity, unit_cost=body.unit_cost, lot_id=body.lot_id,
                                 lot_number=body.lot_number, expiry_date=body.expiry_date, serial_numbers=body.serial_numbers,
                                 warranty_until=body.warranty_until, supplier_name=body.supplier_name, note=body.note,
                                 linked_transaction_id=txn_id, update_avg_cost=True, op_id=op_id)
        fault("after_movements", flt)
        if txn_plan:
            await create_txn(p, "expense", cat, amount, txn_plan["date"], desc, source="inventory", movement_ids=[mv["id"]],
                             txn_id=txn_id, capitalized=True, op_id=op_id)
        return Movement(**mv)
    return await _guarded(p, idem, "restock", Movement, lambda: _with_op(p, "restock", {"movement_count": 1, "txn": txn_plan}, idem, steps))


async def _open_invoice(p: Principal, invoice_id: str | None) -> dict:
    if not invoice_id:
        raise HTTPException(400, "Choose an invoice to add the sale to")
    inv = await Scoped("invoices", p).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] not in ("draft", "sent"):
        raise HTTPException(400, f"Invoice {inv['number']} is {inv['status']} and can't take new lines")
    return inv


async def _add_invoice_lines(p: Principal, inv: dict, movements: list[dict]) -> None:
    from lib.money import invoice_amounts
    lines = inv["lines"] + [{"description": m["product_name"], "quantity": -m["quantity"], "unit_price": m["unit_price"],
                             "kind": "product", "product_id": m["product_id"], "movement_id": m["id"],
                             "unit_cost": round(m["cogs"] / -m["quantity"], 4)} for m in movements]
    amt = invoice_amounts(lines, inv.get("discount_amount", 0), inv.get("tax_rate", 0), inv.get("tip_amount", 0),
                          inv.get("amount_paid", 0))
    await Scoped("invoices", p).update({"id": inv["id"]}, {"$set": {"lines": lines, **amt}})


@router.post("/stock/sell", response_model=Movement)
async def sell(body: SellIn, p: Principal = Depends(require("stock:write")), idem: str | None = IdemKey, flt: str | None = FaultHdr):
    product = await load_product(p, body.product_id)
    if product["type"] != "retail":
        raise HTTPException(400, "Supplies can't be sold — record them as 'Use' instead")
    inv = await _open_invoice(p, body.invoice_id) if body.link == "invoice" else None
    txn_id = str(uuid.uuid4()) if body.link == "new" else None
    amount = line_total(body.quantity, body.unit_price)
    desc = f"Sold {body.quantity} × {product['name']}"
    txn_plan = {"txn_id": txn_id, "kind": "income", "category": "Retail sales", "amount": amount, "date": today_iso(p.settings.get("timezone")),
                "description": desc, "barber_id": body.barber_id} if txn_id else None

    async def steps(op_id: str):
        mv = await post_movement(p, product, "sale", -body.quantity, unit_price=body.unit_price, lot_id=body.lot_id,
                                 serial_ids=body.serial_unit_ids, serial_status="sold", barber_id=body.barber_id, note=body.note,
                                 linked_transaction_id=txn_id, linked_invoice_id=inv["id"] if inv else None, op_id=op_id)
        fault("after_movements", flt)
        if txn_id:
            await create_txn(p, "income", "Retail sales", amount, today_iso(p.settings.get("timezone")), desc, source="inventory", barber_id=body.barber_id,
                             movement_ids=[mv["id"]], txn_id=txn_id, op_id=op_id)
        if inv:
            await _add_invoice_lines(p, inv, [mv])
        return Movement(**mv)
    return await _guarded(p, idem, "sell", Movement, lambda: _with_op(p, "sell", {"movement_count": 1, "txn": txn_plan}, idem, steps))


@router.post("/stock/use", response_model=Movement)
async def use(body: UseIn, p: Principal = Depends(require("stock:write"))):
    product = await load_product(p, body.product_id)
    mv = await post_movement(p, product, "use", -body.quantity, lot_id=body.lot_id, serial_ids=body.serial_unit_ids,
                             serial_status="used", barber_id=body.barber_id, note=body.note)
    return Movement(**mv)


@router.post("/stock/adjust", response_model=Movement)
async def adjust(body: AdjustIn, p: Principal = Depends(require("stock:adjust"))):
    if body.quantity == 0:
        raise HTTPException(400, "Adjustment quantity cannot be zero")
    product = await load_product(p, body.product_id)
    if body.quantity > 0 and product["tracking_mode"] == "lot" and not body.lot_id:
        raise HTTPException(400, "Choose the lot to adjust")
    mv = await post_movement(p, product, "adjustment", body.quantity, lot_id=body.lot_id,
                             unit_cost=body.unit_cost if body.quantity > 0 else None,
                             serial_ids=body.serial_unit_ids, serial_numbers=body.serial_numbers,
                             serial_status="damaged", reason=body.reason, note=body.note)
    return Movement(**mv)


@router.post("/stock/return", response_model=Movement)
async def customer_return(body: ReturnIn, p: Principal = Depends(require("stock:write")), idem: str | None = IdemKey, flt: str | None = FaultHdr):
    product = await load_product(p, body.product_id)
    if product["tracking_mode"] == "lot" and not body.lot_id:
        raise HTTPException(400, "Choose the lot the returned units go back into")
    refund = money(body.refund_amount)
    txn_id = str(uuid.uuid4()) if refund > 0 else None
    desc = f"Refund {body.quantity} × {product['name']}"
    txn_plan = {"txn_id": txn_id, "kind": "expense", "category": "Retail refunds", "amount": refund, "date": today_iso(p.settings.get("timezone")),
                "description": desc, "barber_id": body.barber_id, "payment_method": body.refund_method} if txn_id else None

    async def steps(op_id: str):
        mv = await post_movement(p, product, "return", body.quantity, lot_id=body.lot_id, serial_ids=body.serial_unit_ids,
                                 unit_price=round(refund / body.quantity, 4), barber_id=body.barber_id,
                                 note=body.note, linked_transaction_id=txn_id, op_id=op_id)
        fault("after_movements", flt)
        if txn_id:
            await create_txn(p, "expense", "Retail refunds", refund, today_iso(p.settings.get("timezone")), desc, source="inventory", barber_id=body.barber_id,
                             movement_ids=[mv["id"]], txn_id=txn_id, payment_method=body.refund_method, op_id=op_id)
        return Movement(**mv)
    return await _guarded(p, idem, "return", Movement, lambda: _with_op(p, "return", {"movement_count": 1, "txn": txn_plan}, idem, steps))


@router.post("/stock/count", response_model=CountResult)
async def stock_count(body: CountIn, p: Principal = Depends(require("stock:adjust"))):
    """Shelf count: each difference vs the ledger becomes an audited count_correction adjustment.
    Validated up front; serial products are skipped (count them by serial on the product page)."""
    seen: dict[str, int] = {}
    for ln in body.lines:
        seen[ln.product_id] = ln.counted  # last value wins if a product is sent twice
    products = {pid: await load_product(p, pid) for pid in seen}
    adjusted, skipped, unchanged = [], [], 0
    for pid, counted in seen.items():
        prod = await load_product(p, pid)  # fresh qty right before posting
        diff = counted - prod["quantity_on_hand"]
        if diff == 0:
            unchanged += 1
            continue
        if prod["tracking_mode"] == "serial":
            skipped.append(CountSkip(product_id=pid, name=prod["name"], reason="Serial-tracked: adjust individual serials on the product page"))
            continue
        lot_id = None
        if prod["tracking_mode"] == "lot" and diff > 0:
            lots = await Scoped("lots", p).find({"product_id": pid}, sort=[("received_date", -1)], limit=1)
            if not lots:
                skipped.append(CountSkip(product_id=pid, name=prod["name"], reason="No lot to add units to — restock with a lot number"))
                continue
            lot_id = lots[0]["id"]
        try:
            mv = await post_movement(p, prod, "adjustment", diff, lot_id=lot_id, reason="count_correction",
                                     note=(body.note or "Shelf count") + f" (counted {counted}, ledger {prod['quantity_on_hand']})")
            adjusted.append(Movement(**mv))
        except HTTPException as e:
            skipped.append(CountSkip(product_id=pid, name=products[pid]["name"], reason=str(e.detail)))
    return CountResult(adjusted=adjusted, unchanged=unchanged, skipped=skipped)


@router.post("/stock/checkout", response_model=CheckoutResult)
async def checkout(body: CheckoutIn, p: Principal = Depends(require("stock:write")), idem: str | None = IdemKey, flt: str | None = FaultHdr):
    return await do_checkout(body, p, idem, flt)


async def do_checkout(body: CheckoutIn, p: Principal, idem: str | None = None, flt: str | None = None,
                      processor: str = "recorded") -> CheckoutResult:
    """Quick sell at the chair: several products, one income record (or one invoice). Journaled + idempotent."""
    from routers.clients import apply_discount, release_discount, tag_sale
    products = [await load_product(p, ln.product_id) for ln in body.lines]
    for prod, ln in zip(products, body.lines):
        if prod["type"] != "retail":
            raise HTTPException(400, f"{prod['name']} is a supply and can't be sold")
        if prod["tracking_mode"] != "serial" and prod["quantity_on_hand"] < ln.quantity and not p.settings.get("allow_negative_stock"):
            raise HTTPException(409, f"Not enough stock for {prod['name']}: only {prod['quantity_on_hand']} on hand")
    inv = await _open_invoice(p, body.invoice_id) if body.invoice_id else None

    async def run():
        pct = await apply_discount(p, body)  # consumes the code only after validation passed
        # Match Stripe's representable per-unit amount, including undiscounted prices.
        for ln in body.lines:
            ln.unit_price = money(ln.unit_price)
        txn_id = None if inv else str(uuid.uuid4())
        total = msum(line_total(ln.quantity, ln.unit_price) for ln in body.lines)
        method = body.payment_method or "other"
        names = ", ".join(f"{ln.quantity}× {prod['name']}" for prod, ln in zip(products, body.lines))
        txn_plan = {"txn_id": txn_id, "kind": "income", "category": "Retail sales", "amount": total, "date": today_iso(p.settings.get("timezone")),
                    "description": f"Quick sell: {names}", "barber_id": body.barber_id, "payment_method": method,
                    "processor": processor} if txn_id and total > 0 else None
        code = body.discount_code.strip().upper() if pct and body.discount_code else None

        async def steps(op_id: str):
            done: list[dict] = []
            try:
                for prod, ln in zip(products, body.lines):
                    done.append(await post_movement(p, prod, "sale", -ln.quantity, unit_price=ln.unit_price,
                                                    serial_ids=ln.serial_unit_ids, serial_status="sold", barber_id=body.barber_id,
                                                    note="Quick sell", linked_transaction_id=txn_id,
                                                    linked_invoice_id=inv["id"] if inv else None, op_id=op_id))
            except HTTPException:
                for mv in done:  # append-only rollback: reverse what already posted
                    prod = await load_product(p, mv["product_id"])
                    await post_movement(p, prod, "return", -mv["quantity"], lot_id=mv["lot_id"], serial_ids=mv["serial_unit_ids"],
                                        unit_price=mv["unit_price"], note="Quick sell rolled back (another line failed)",
                                        op_id=op_id, compensates=mv["id"])
                if code:
                    await release_discount(p, code)
                raise
            fault("after_movements", flt)
            if txn_plan:
                await create_txn(p, "income", "Retail sales", total, txn_plan["date"], txn_plan["description"], source="inventory",
                                 barber_id=body.barber_id, movement_ids=[m["id"] for m in done], txn_id=txn_id,
                                 payment_method=method, processor=processor, op_id=op_id)
            if inv:
                await _add_invoice_lines(p, inv, done)
            await tag_sale(p, body, [m["id"] for m in done], txn_id if txn_plan else None, pct)
            return CheckoutResult(movements=[Movement(**m) for m in done], total=total,
                                  transaction_id=txn_id if txn_plan else None, invoice_id=inv["id"] if inv else None)
        return await _with_op(p, "checkout", {"movement_count": len(body.lines), "txn": txn_plan, "discount_code": code}, idem, steps)
    return await _guarded(p, idem, "checkout", CheckoutResult, run)
