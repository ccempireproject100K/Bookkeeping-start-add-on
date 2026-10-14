"""Customers & service locations."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import require, get_current_user

router = APIRouter(prefix="/customers", tags=["customers"])


def coll(user):
    return Scoped(db.customers, user["tenant_id"])


class Location(BaseModel):
    label: str = "Primary"
    address: str


class CustomerBody(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    locations: List[Location] = []


@router.get("")
async def list_customers(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("name", 1)], limit=500)


@router.post("")
async def create_customer(body: CustomerBody, user: dict = Depends(require("ops:manage"))):
    doc = {
        "id": new_id(), "name": body.name, "email": body.email, "phone": body.phone,
        "locations": [l.model_dump() for l in body.locations], "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/{customer_id}")
async def get_customer(customer_id: str, user: dict = Depends(get_current_user)):
    c = await coll(user).find_one({"id": customer_id})
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {k: v for k, v in c.items() if k != "_id"}
