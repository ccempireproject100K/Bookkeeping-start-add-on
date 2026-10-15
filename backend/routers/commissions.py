"""Barber commission payouts: statement of accrued commission, paid from 2300 Tips/Commissions Payable."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/commissions", tags=["commissions"])


async def _unpaid_sales(tenant_id, barber_id):
    return await Scoped(db.sales, tenant_id).find_list({
        "barber_id": barber_id,
        "commission_cents": {"$gt": 0},
        "payout_id": {"$in": [None]},
    })


@router.get("/summary")
async def summary(user: dict = Depends(require("ledger:read"))):
    tenant_id = user["tenant_id"]
    team = await db.users.find({"tenant_id": tenant_id, "role": "barber"}, {"_id": 0, "password_hash": 0}).to_list(100)
    sales = await Scoped(db.sales, tenant_id).find_list({"commission_cents": {"$gt": 0}})
    out = []
    for b in team:
        bsales = [s for s in sales if s.get("barber_id") == b["id"]]
        accrued = sum(s["commission_cents"] for s in bsales if not s.get("payout_id"))
        paid = sum(s["commission_cents"] for s in bsales if s.get("payout_id"))
        out.append({
            "barber_id": b["id"], "name": b["name"],
            "commission_rate": b.get("commission_rate", 0),
            "accrued_cents": accrued, "accrued": cents_to_dollars(accrued),
            "paid_cents": paid, "paid": cents_to_dollars(paid),
            "unpaid_count": len([s for s in bsales if not s.get("payout_id")]),
        })
    return out


@router.get("/statement/{barber_id}")
async def statement(barber_id: str, user: dict = Depends(require("ledger:read"))):
    sales = await _unpaid_sales(user["tenant_id"], barber_id)
    lines = [{
        "date": s.get("created_at", "")[:10], "entry": s.get("entry_number"),
        "sale_total": cents_to_dollars(s["total_cents"]),
        "commission": cents_to_dollars(s["commission_cents"]),
        "commission_cents": s["commission_cents"],
    } for s in sales]
    total = sum(s["commission_cents"] for s in sales)
    return {"barber_id": barber_id, "lines": lines, "total_cents": total, "total": cents_to_dollars(total)}


@router.get("/payouts")
async def payouts(user: dict = Depends(get_current_user)):
    return await Scoped(db.commission_payouts, user["tenant_id"]).find_list(sort=[("created_at", -1)], limit=200)


class PayoutBody(BaseModel):
    barber_id: str
    payment_method: str = "cash"


@router.post("/payout")
async def pay_out(body: PayoutBody, user: dict = Depends(require("ledger:admin"))):
    tenant_id = user["tenant_id"]
    barber = await db.users.find_one({"id": body.barber_id, "tenant_id": tenant_id}, {"_id": 0, "password_hash": 0})
    if not barber:
        raise HTTPException(status_code=404, detail="Barber not found")
    sales = await _unpaid_sales(tenant_id, body.barber_id)
    total = sum(s["commission_cents"] for s in sales)
    if total <= 0:
        raise HTTPException(status_code=422, detail="No accrued commission to pay out")
    cash = P._cash_account(body.payment_method)
    payout_id = new_id()
    entry = await L.post_journal_entry(db, tenant_id, user, {
        "entry_date": today_str(), "source": "commission_payout", "source_ref": payout_id,
        "memo": f"Commission payout — {barber['name']}",
        "lines": [P.dr("2300", total, "Commission payable settled"), P.cr(cash, total, "Payout")],
    }, status="posted")
    sale_ids = [s["id"] for s in sales]
    await db.sales.update_many(
        {"tenant_id": tenant_id, "id": {"$in": sale_ids}},
        {"$set": {"payout_id": payout_id}},
    )
    doc = {
        "id": payout_id, "barber_id": body.barber_id, "barber_name": barber["name"],
        "amount_cents": total, "amount": cents_to_dollars(total), "sale_count": len(sales),
        "entry_number": entry["number"], "payment_method": body.payment_method,
        "created_by": user["email"], "created_at": now_iso(),
    }
    await Scoped(db.commission_payouts, tenant_id).insert_one(doc)
    doc.pop("_id", None)
    return doc
