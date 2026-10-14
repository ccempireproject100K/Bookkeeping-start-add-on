"""Products & inventory: CRUD, restock (moving-average cost), adjustments. Posts to the ledger."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/products", tags=["products"])


def coll(user):
    return Scoped(db.products, user["tenant_id"])


@router.get("")
async def list_products(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("name", 1)])


class ProductBody(BaseModel):
    name: str
    type: str = "retail"              # retail | supply
    revenue_code: str = "4100"
    price: float = 0
    unit_cost: float = 0
    quantity_on_hand: int = 0
    commissionable: bool = True
    image_url: Optional[str] = None


@router.post("")
async def create_product(body: ProductBody, user: dict = Depends(require("ops:manage"))):
    doc = {
        "id": new_id(), "name": body.name, "type": body.type,
        "revenue_code": body.revenue_code, "price_cents": to_cents(body.price),
        "unit_cost_cents": to_cents(body.unit_cost), "quantity_on_hand": int(body.quantity_on_hand),
        "commissionable": body.commissionable, "image_url": body.image_url, "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


class RestockBody(BaseModel):
    quantity: int
    unit_cost: float
    on_terms: bool = False


@router.post("/{product_id}/restock")
async def restock(product_id: str, body: RestockBody, user: dict = Depends(require("ops:manage"))):
    p = await coll(user).find_one({"id": product_id})
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    recv_cents = to_cents(body.unit_cost)
    old_qty = int(p["quantity_on_hand"])
    old_cost = int(p["unit_cost_cents"])
    new_qty = old_qty + body.quantity
    new_cost = round((old_qty * old_cost + body.quantity * recv_cents) / new_qty) if new_qty else recv_cents
    await coll(user).update_one({"id": product_id}, {"$set": {
        "quantity_on_hand": new_qty, "unit_cost_cents": new_cost,
    }})
    amount = body.quantity * recv_cents
    await L.post_journal_entry(db, user["tenant_id"], user, P.build_restock(
        amount_cents=amount, on_terms=body.on_terms, entry_date=today_str(),
        source_ref=product_id, memo=f"Restock {p['name']} x{body.quantity}",
    ), status="posted")
    return {"quantity_on_hand": new_qty, "unit_cost_cents": new_cost}


class AdjustBody(BaseModel):
    quantity_delta: int           # negative = shrinkage, positive = found
    reason: str


@router.post("/{product_id}/adjust")
async def adjust(product_id: str, body: AdjustBody, user: dict = Depends(require("ops:manage"))):
    if body.quantity_delta == 0:
        raise HTTPException(status_code=422, detail="Delta cannot be zero")
    p = await coll(user).find_one({"id": product_id})
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    new_qty = int(p["quantity_on_hand"]) + body.quantity_delta
    if new_qty < 0:
        raise HTTPException(status_code=422, detail="Adjustment would make quantity negative")
    await coll(user).update_one({"id": product_id}, {"$set": {"quantity_on_hand": new_qty}})
    amount = abs(body.quantity_delta) * int(p["unit_cost_cents"])
    if amount:
        await L.post_journal_entry(db, user["tenant_id"], user, P.build_adjustment(
            amount_cents=amount, found=body.quantity_delta > 0, entry_date=today_str(),
            source_ref=product_id, memo=f"Adjust {p['name']} ({body.reason})",
        ), status="posted")
    return {"quantity_on_hand": new_qty}
