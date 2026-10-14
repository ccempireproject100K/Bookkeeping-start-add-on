"""Daily cash close. Expected cash is always recomputed server-side from cash-method transactions:

expected = opening float + cash income − cash refunds − other cash expenses + paid-ins − paid-outs
discrepancy = counted − expected (any non-zero discrepancy needs an explanation). Owner approves or flags;
approved closes are locked. Every submit / revision / review is in the audit log.
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException

from lib.audit import audit
from lib.auth import Principal, require
from lib.dates import now_iso, today_iso
from lib.money import msum, money
from lib.repo import Scoped
from models.ledger import CashClose, CashCloseIn, CashClosePreview, CashReviewIn

router = APIRouter()


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in ("tenant_id", "history", "submitted_by")}


async def _cash_numbers(p: Principal, day: str) -> tuple[float, float, float, int]:
    txns = await Scoped("transactions", p).find({"date": day, "payment_method": "cash"})
    sales = msum(t["amount"] for t in txns if t["kind"] == "income")
    refunds = msum(t["amount"] for t in txns if t["kind"] == "expense" and t["category"] == "Retail refunds")
    other = msum(t["amount"] for t in txns if t["kind"] == "expense" and t["category"] != "Retail refunds")
    return sales, refunds, other, len(txns)


@router.get("/cash-closes", response_model=list[CashClose])
async def list_closes(p: Principal = Depends(require("cashclose:read"))):
    return [CashClose(**_strip(d)) for d in await Scoped("cash_closes", p).find({}, sort=[("date", -1)], limit=90)]


@router.get("/cash-closes/preview", response_model=CashClosePreview)
async def preview(day: str = "", p: Principal = Depends(require("cashclose:write"))):
    day = day or today_iso(p.settings.get("timezone"))
    date.fromisoformat(day)
    sales, refunds, other, n = await _cash_numbers(p, day)
    prev = await Scoped("cash_closes", p).find({"date": {"$lt": day}}, sort=[("date", -1)], limit=1)
    existing = await Scoped("cash_closes", p).find_one({"date": day})
    return CashClosePreview(date=day, suggested_opening_float=prev[0]["counted_cash"] if prev else 0, cash_sales=sales,
                            cash_refunds=refunds, cash_expenses=other, cash_txn_count=n,
                            existing=CashClose(**_strip(existing)) if existing else None)


@router.post("/cash-closes", response_model=CashClose)
async def submit_close(body: CashCloseIn, p: Principal = Depends(require("cashclose:write"))):
    if body.date > today_iso(p.settings.get("timezone")) or body.date < (date.fromisoformat(today_iso(p.settings.get("timezone"))) - timedelta(days=31)).isoformat():
        raise HTTPException(400, "Close a day from the last 31 days, not a future date")
    sales, refunds, other, _ = await _cash_numbers(p, body.date)
    paid_in = msum(m.amount for m in body.drawer_moves if m.kind == "paid_in")
    paid_out = msum(m.amount for m in body.drawer_moves if m.kind == "paid_out")
    expected = msum([body.opening_float, sales, -refunds, -other, paid_in, -paid_out])
    disc = money(money(body.counted_cash) - expected)
    closes = Scoped("cash_closes", p)
    existing = await closes.find_one({"date": body.date})
    if existing and existing["status"] == "approved":
        raise HTTPException(409, "This day was approved by the owner and is locked")
    if disc != 0 and len(body.explanation.strip()) < 5:
        raise HTTPException(400, f"The drawer is {'over' if disc > 0 else 'short'} by {abs(disc):.2f} {p.settings.get('currency', 'USD')} — add an explanation before submitting")
    snap = {"opening_float": money(body.opening_float), "cash_sales": sales, "cash_refunds": refunds, "cash_expenses": other,
            "paid_in": paid_in, "paid_out": paid_out, "drawer_moves": [m.model_dump() for m in body.drawer_moves],
            "expected_cash": expected, "counted_cash": money(body.counted_cash), "discrepancy": disc,
            "explanation": body.explanation.strip(), "status": "submitted", "submitted_by": p.user_id, "submitted_by_name": p.name,
            "submitted_at": now_iso(), "reviewed_by_name": None, "reviewed_at": None, "review_note": ""}
    if existing:
        doc = await closes.update({"id": existing["id"], "status": {"$ne": "approved"}, "revision": existing.get("revision", 1)},
                                  {"$set": {**snap, "revision": existing.get("revision", 1) + 1},
                                   "$push": {"history": {k: existing.get(k) for k in ("counted_cash", "expected_cash", "discrepancy", "explanation", "status", "submitted_by_name", "submitted_at")}}})
        if not doc:
            raise HTTPException(409, "This close changed while you were editing — refresh and retry")
    else:
        try:
            doc = await closes.insert({"id": str(uuid.uuid4()), "date": body.date, "revision": 1, "history": [], **snap})
        except Exception:  # unique (tenant, date): a parallel submit won
            raise HTTPException(409, "Someone just submitted this day's close — refresh and retry")
    await audit(p, "cash_close.submit", "cash_close", doc["id"], {"date": body.date, "expected": expected, "counted": snap["counted_cash"],
                                                                  "discrepancy": disc, "revision": doc.get("revision", 1)})
    return CashClose(**_strip(doc))


@router.post("/cash-closes/{id}/review", response_model=CashClose)
async def review_close(id: str, body: CashReviewIn, p: Principal = Depends(require("cashclose:review"))):
    if not body.approve and len(body.note.strip()) < 3:
        raise HTTPException(400, "Say what needs fixing when you flag a close")
    status = "approved" if body.approve else "flagged"
    doc = await Scoped("cash_closes", p).update({"id": id, "status": "submitted"},
                                                {"$set": {"status": status, "reviewed_by_name": p.name, "reviewed_at": now_iso(), "review_note": body.note.strip()}})
    if not doc:
        raise HTTPException(409, "Only a submitted close can be reviewed")
    await audit(p, f"cash_close.{status}", "cash_close", id, {"date": doc["date"], "note": body.note.strip()})
    return CashClose(**_strip(doc))
