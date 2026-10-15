import uuid

from fastapi import APIRouter, Depends, HTTPException

from lib.auth import Principal, require
from lib.dates import now_iso
from lib.repo import Scoped
from models.inventory import Supplier, SupplierIn

router = APIRouter()


async def _with_counts(p: Principal, docs: list[dict]) -> list[Supplier]:
    prod_counts = {r["_id"]: r["n"] for r in await Scoped("products", p).aggregate(
        [{"$group": {"_id": "$supplier_id", "n": {"$sum": 1}}}])}
    po_counts = {r["_id"]: r["n"] for r in await Scoped("purchase_orders", p).aggregate(
        [{"$match": {"status": {"$in": ["draft", "ordered", "partial"]}}}, {"$group": {"_id": "$supplier_id", "n": {"$sum": 1}}}])}
    return [Supplier(**{k: v for k, v in d.items() if k != "tenant_id"},
                     product_count=prod_counts.get(d["id"], 0), open_po_count=po_counts.get(d["id"], 0)) for d in docs]


@router.get("/suppliers", response_model=list[Supplier])
async def list_suppliers(p: Principal = Depends(require("supplier:read"))):
    return await _with_counts(p, await Scoped("suppliers", p).find(sort=[("name", 1)]))


@router.post("/suppliers", response_model=Supplier)
async def create_supplier(body: SupplierIn, p: Principal = Depends(require("supplier:write"))):
    doc = await Scoped("suppliers", p).insert({**body.model_dump(), "id": str(uuid.uuid4()), "created_at": now_iso()})
    return (await _with_counts(p, [doc]))[0]


@router.put("/suppliers/{id}", response_model=Supplier)
async def update_supplier(id: str, body: SupplierIn, p: Principal = Depends(require("supplier:write"))):
    doc = await Scoped("suppliers", p).update({"id": id}, {"$set": body.model_dump()})
    if not doc:
        raise HTTPException(404, "Supplier not found")
    return (await _with_counts(p, [doc]))[0]


@router.delete("/suppliers/{id}")
async def delete_supplier(id: str, p: Principal = Depends(require("supplier:delete"))):
    if not await Scoped("suppliers", p).find_one({"id": id}):
        raise HTTPException(404, "Supplier not found")
    if await Scoped("purchase_orders", p).count({"supplier_id": id, "status": {"$in": ["draft", "ordered", "partial"]}}):
        raise HTTPException(400, "Supplier has open purchase orders")
    await Scoped("suppliers", p).delete({"id": id})
    # Detach products from the removed supplier (tenant-scoped).
    from lib.db import db
    await db.products.update_many({"tenant_id": p.tenant_id, "supplier_id": id}, {"$set": {"supplier_id": None}})
    return {"ok": True}
