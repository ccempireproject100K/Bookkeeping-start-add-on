"""Point of sale (Quick Sell): decrement stock atomically and post a balanced sale entry."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/pos", tags=["pos"])


class ProductItem(BaseModel):
    product_id: str
    qty: int = 1


class ServiceItem(BaseModel):
    service_type: str = "haircut"    # haircut | beard | service
    name: str
    price: float


class SellBody(BaseModel):
    barber_id: Optional[str] = None
    payment_method: str = "cash"      # cash | card | bank
    discount: float = 0
    product_items: List[ProductItem] = []
    service_items: List[ServiceItem] = []


@router.get("/sales")
async def list_sales(user: dict = Depends(get_current_user)):
    return await Scoped(db.sales, user["tenant_id"]).find_list(sort=[("created_at", -1)], limit=100)


@router.post("/sell")
async def sell(body: SellBody, user: dict = Depends(require("ops:sell"))):
    tenant_id = user["tenant_id"]
    products = Scoped(db.products, tenant_id)
    tenant = await db.tenants.find_one({"id": tenant_id}, {"_id": 0})
    rate = float((tenant or {}).get("settings", {}).get("sales_tax_rate", 0))

    rev_by_code = {}
    product_subtotal = 0
    service_subtotal = 0
    cogs_cents = 0
    cart = []

    # product lines (atomic stock decrement)
    for it in body.product_items:
        p = await products.find_one({"id": it.product_id})
        if not p:
            raise HTTPException(status_code=404, detail=f"Product {it.product_id} not found")
        res = await products.update_one(
            {"id": it.product_id, "quantity_on_hand": {"$gte": it.qty}},
            {"$inc": {"quantity_on_hand": -it.qty}},
        )
        if res.matched_count == 0:
            raise HTTPException(status_code=409, detail=f"{p['name']} is out of stock")
        amt = int(p["price_cents"]) * it.qty
        product_subtotal += amt
        cogs_cents += int(p["unit_cost_cents"]) * it.qty
        rev_by_code[p["revenue_code"]] = rev_by_code.get(p["revenue_code"], 0) + amt
        cart.append({"name": p["name"], "qty": it.qty, "amount_cents": amt})

    # service lines
    for sv in body.service_items:
        amt = to_cents(sv.price)
        service_subtotal += amt
        code = P.REVENUE_FOR.get(sv.service_type, "4020")
        rev_by_code[code] = rev_by_code.get(code, 0) + amt
        cart.append({"name": sv.name, "qty": 1, "amount_cents": amt})

    subtotal = product_subtotal + service_subtotal
    if subtotal == 0:
        raise HTTPException(status_code=422, detail="Cart is empty")
    discount_cents = min(to_cents(body.discount), subtotal)
    disc_on_products = round(discount_cents * product_subtotal / subtotal) if subtotal else 0
    taxable = max(product_subtotal - disc_on_products, 0)
    tax_cents = round(taxable * rate)

    commission_cents = 0
    if body.barber_id:
        barber = await db.users.find_one({"id": body.barber_id, "tenant_id": tenant_id}, {"_id": 0})
        if barber:
            commission_cents = round(service_subtotal * float(barber.get("commission_rate", 0)))

    lines_in = [{"revenue_code": code, "amount_cents": amt} for code, amt in rev_by_code.items()]
    sale_id = new_id()
    entry = await L.post_journal_entry(db, tenant_id, user, P.build_sale(
        lines_in=lines_in, payment_method=body.payment_method,
        subtotal_cents=subtotal, discount_cents=discount_cents, tax_cents=tax_cents,
        cogs_cents=cogs_cents, commission_cents=commission_cents,
        entry_date=today_str(), source_ref=sale_id, memo="Quick Sell",
    ), status="posted", idempotency_key=f"sale:{sale_id}")

    total_cents = subtotal - discount_cents + tax_cents
    sale = {
        "id": sale_id, "cart": cart, "payment_method": body.payment_method,
        "subtotal_cents": subtotal, "discount_cents": discount_cents, "tax_cents": tax_cents,
        "total_cents": total_cents, "cogs_cents": cogs_cents, "commission_cents": commission_cents,
        "barber_id": body.barber_id, "entry_id": entry["id"], "entry_number": entry["number"],
        "created_at": now_iso(),
    }
    await Scoped(db.sales, tenant_id).insert_one(sale)
    sale.pop("_id", None)
    return sale
