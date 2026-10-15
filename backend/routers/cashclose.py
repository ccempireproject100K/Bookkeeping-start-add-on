"""Daily cash close: reconcile counted drawer against expected, post over/short to the ledger."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/cash-close", tags=["cash-close"])


def coll(user):
    return Scoped(db.sales, user["tenant_id"])


class CloseBody(BaseModel):
    opening_float: float = 0
    counted_cash: float
    paid_in: float = 0
    paid_out: float = 0
    notes: Optional[str] = None


@router.get("")
async def list_closes(user: dict = Depends(get_current_user)):
    return await Scoped(db.cash_closes, user["tenant_id"]).find_list(sort=[("created_at", -1)], limit=60)


@router.get("/preview")
async def preview(user: dict = Depends(require("ops:manage"))):
    today = today_str()
    sales = await coll(user).find_list({"created_at": {"$gte": today}})
    cash_sales = sum(s["total_cents"] for s in sales if s.get("payment_method") == "cash")
    return {"cash_sales_cents": cash_sales, "count": len(sales)}


@router.post("")
async def close(body: CloseBody, user: dict = Depends(require("ops:manage"))):
    tenant_id = user["tenant_id"]
    today = today_str()
    sales = await coll(user).find_list({"created_at": {"$gte": today}})
    cash_sales = sum(s["total_cents"] for s in sales if s.get("payment_method") == "cash")
    opening = to_cents(body.opening_float)
    paid_in = to_cents(body.paid_in)
    paid_out = to_cents(body.paid_out)
    counted = to_cents(body.counted_cash)
    expected = opening + cash_sales + paid_in - paid_out
    variance = counted - expected

    entry_number = None
    draft = P.build_cash_close(variance_cents=variance, entry_date=today,
                               source_ref="cashclose", memo=body.notes or "Daily cash close variance")
    if draft:
        entry = await L.post_journal_entry(db, tenant_id, user, draft, status="posted")
        entry_number = entry["number"]

    doc = {
        "id": new_id(), "date": today, "opening_cents": opening, "cash_sales_cents": cash_sales,
        "paid_in_cents": paid_in, "paid_out_cents": paid_out, "expected_cents": expected,
        "counted_cents": counted, "variance_cents": variance, "notes": body.notes,
        "entry_number": entry_number, "created_by": user["email"], "created_at": now_iso(),
    }
    await Scoped(db.cash_closes, tenant_id).insert_one(doc)
    doc.pop("_id", None)
    return doc
