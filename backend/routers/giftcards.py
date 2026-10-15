"""Gift cards / prepaid packages as deferred revenue (2200 Unearned Revenue)."""
import random
import string
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/gift-cards", tags=["gift-cards"])


def coll(user):
    return Scoped(db.gift_cards, user["tenant_id"])


def _code():
    return "GC-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


class IssueBody(BaseModel):
    amount: float
    recipient: str
    payment_method: str = "card"


class RedeemBody(BaseModel):
    amount: float
    revenue_code: str = "4020"


@router.get("")
async def list_cards(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("created_at", -1)], limit=300)


@router.get("/outstanding")
async def outstanding(user: dict = Depends(require("ledger:read"))):
    cards = await coll(user).find_list({"status": "active"})
    total = sum(c["balance_cents"] for c in cards)
    return {"total": cents_to_dollars(total), "total_cents": total, "count": len(cards), "cards": cards}


@router.post("")
async def issue(body: IssueBody, user: dict = Depends(require("ops:manage"))):
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    cash = P._cash_account(body.payment_method)
    card_id = new_id()
    code = _code()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "giftcard_issue", "source_ref": card_id,
        "memo": f"Gift card {code} — {body.recipient}",
        "lines": [P.dr(cash, amount, "Gift card sold"), P.cr("2200", amount, "Unearned revenue")],
    }, status="posted")
    doc = {
        "id": card_id, "code": code, "recipient": body.recipient,
        "initial_cents": amount, "balance_cents": amount, "status": "active",
        "issue_entry": entry["number"], "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.post("/{code}/redeem")
async def redeem(code: str, body: RedeemBody, user: dict = Depends(require("ops:manage"))):
    card = await coll(user).find_one({"code": code})
    if not card:
        raise HTTPException(status_code=404, detail="Gift card not found")
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    if amount > card["balance_cents"]:
        raise HTTPException(status_code=422, detail="Amount exceeds remaining balance")
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "giftcard_redeem", "source_ref": card["id"],
        "memo": f"Redeem gift card {code}",
        "lines": [P.dr("2200", amount, "Unearned revenue earned"), P.cr(body.revenue_code, amount, "Service revenue")],
    }, status="posted")
    new_bal = card["balance_cents"] - amount
    await coll(user).update_one({"code": code}, {"$set": {
        "balance_cents": new_bal, "status": "redeemed" if new_bal == 0 else "active",
    }})
    return {"code": code, "balance_cents": new_bal, "redeem_entry": entry["number"]}
