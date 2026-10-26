"""Payment reminders: owner-approved, manual nudges for overdue invoices. Reminders are
never automatic — each one is created from the UI, logged, and stored in the
invoice's reminder history. This codebase has no mailer, so a reminder is recorded
as sent (the user copies the text into their own email); the record is the audit."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.audit import append_audit
from lib.base import Scoped, new_id, now_iso, today_str
from lib.auth import require, get_current_user
from lib.plans import require_feature

router = APIRouter(prefix="/reminders", tags=["reminders"])


def inv_coll(user):
    return Scoped(db.invoices, user["tenant_id"])


def rem_coll(user):
    return Scoped(db.payment_reminders, user["tenant_id"])


async def _audit(user, action, entity, entity_id, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


class DraftIn(BaseModel):
    invoice_id: str
    message: Optional[str] = None      # optional custom text; default generated


class SendIn(BaseModel):
    invoice_id: str
    message: Optional[str] = None


def _days_overdue(due_date: Optional[str], today: str) -> int:
    if not due_date:
        return 0
    return max(0, (date.fromisoformat(today) - date.fromisoformat(due_date)).days)


def _default_message(inv: dict, days: int) -> str:
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    return (
        f"Hi {inv['client_name']},\n\n"
        f"Just a friendly reminder that invoice #{inv['id'][:8]} for "
        f"${balance / 100:.2f} was due on {inv.get('due_date') or 'receipt'}"
        f" ({days} day{'s' if days != 1 else ''} ago).\n\n"
        f"Could you let us know when we can expect payment? "
        f"Reply to this message if anything looks off.\n\nThanks!"
    )


def _overdue_row(inv: dict, today: str) -> dict:
    days = _days_overdue(inv.get("due_date"), today)
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    return {"invoice_id": inv["id"], "client_name": inv["client_name"],
            "due_date": inv.get("due_date"), "days_overdue": days,
            "total_cents": inv["total_cents"], "paid_cents": inv.get("paid_cents", 0),
            "balance_cents": balance, "reminders_count": len(inv.get("reminders", []))}


@router.get("/overdue")
async def overdue(user: dict = Depends(require("ledger:read"))):
    """Invoices past their due date with an outstanding balance."""
    today = today_str()
    rows = await inv_coll(user).find_list({
        "status": {"$in": ["issued", "partial"]},
        "due_date": {"$ne": None, "$lt": today},
    }, sort=[("due_date", 1)], limit=500)
    out = []
    for inv in rows:
        if inv["total_cents"] - inv.get("paid_cents", 0) > 0:
            out.append(_overdue_row(inv, today))
    return out


@router.post("/draft", dependencies=[Depends(require_feature("collections_automation"))])
async def draft(body: DraftIn, user: dict = Depends(require("ops:manage"))):
    """Generate (or echo back) reminder text for an invoice — nothing is saved."""
    inv = await inv_coll(user).find_one({"id": body.invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    today = today_str()
    message = (body.message or "").strip() or _default_message(inv, _days_overdue(inv.get("due_date"), today))
    return {"invoice_id": inv["id"], "client_name": inv["client_name"], "message": message}


@router.post("/send", dependencies=[Depends(require_feature("collections_automation"))])
async def send(body: SendIn, user: dict = Depends(require("ops:manage"))):
    """Mark a reminder as sent: stores the record and appends it to the invoice's
    reminder history. (No email is sent — copy the text into your own mail app.)"""
    inv = await inv_coll(user).find_one({"id": body.invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    today = today_str()
    message = (body.message or "").strip() or _default_message(inv, _days_overdue(inv.get("due_date"), today))
    rec = {"id": new_id(), "invoice_id": inv["id"], "client_name": inv["client_name"],
           "message": message, "status": "sent", "sent_at": now_iso(),
           "sent_by": user.get("name"), "sent_by_id": user["id"]}
    await rem_coll(user).insert_one(dict(rec))
    await inv_coll(user).update_one({"id": inv["id"]}, {"$push": {"reminders": rec}})
    await _audit(user, "reminder.send", "invoice", inv["id"],
                 {"client": inv["client_name"], "reminder_id": rec["id"]})
    rec.pop("_id", None)
    return rec


@router.get("")
async def history(user: dict = Depends(require("ledger:read"))):
    return await rem_coll(user).find_list(sort=[("sent_at", -1)], limit=200)
