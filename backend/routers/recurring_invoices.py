"""Recurring invoice templates: schedule definitions that materialize real (issued)
invoices through the exact same issue logic as a manual invoice. Materialization is
manual — the "generate due" endpoint creates invoices for every active template
whose next_run has arrived, then advances next_run by the template's frequency."""
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.base import Scoped, new_id, now_iso, today_str
from lib.auth import require, get_current_user
from routers.invoices import InvoiceBody, InvLine, issue_invoice

router = APIRouter(prefix="/recurring-invoices", tags=["recurring-invoices"])


def coll(user):
    return Scoped(db.recurring_invoices, user["tenant_id"])


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


class RecurringInvoiceBody(BaseModel):
    name: str
    client_name: str
    customer_id: Optional[str] = None
    lines: List[InvLine]
    tax: float = 0
    due_date: Optional[str] = None
    terms: Optional[str] = "Net 30"
    frequency: str = "monthly"          # weekly | monthly
    next_run: Optional[str] = None      # YYYY-MM-DD; defaults to today
    active: bool = True


def _advance(next_run: str, frequency: str) -> str:
    d = date.fromisoformat(next_run)
    if frequency == "weekly":
        d = d + timedelta(days=7)
    else:
        # monthly: same day next month, clamped to the 28th
        month = d.month + 1
        year = d.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        d = d.replace(year=year, month=month, day=min(d.day, 28))
    return d.isoformat()


def _materialize_body(t: dict) -> InvoiceBody:
    return InvoiceBody(
        client_name=t["client_name"], customer_id=t.get("customer_id"),
        lines=[InvLine(**l) for l in t["lines"]], tax=t.get("tax", 0),
        due_date=t.get("due_date"), terms=t.get("terms", "Net 30"),
    )


@router.get("")
async def list_templates(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("name", 1)], limit=500)


@router.post("")
async def create_template(body: RecurringInvoiceBody, user: dict = Depends(require("ops:manage"))):
    if body.frequency not in ("weekly", "monthly"):
        raise HTTPException(status_code=422, detail="frequency must be weekly or monthly")
    if not body.lines:
        raise HTTPException(status_code=422, detail="At least one line is required")
    if body.next_run:
        try:
            date.fromisoformat(body.next_run)
        except ValueError:
            raise HTTPException(status_code=422, detail="next_run must be YYYY-MM-DD")
    doc = {"id": new_id(), **body.model_dump(),
           "next_run": body.next_run or today_str(), "created_at": now_iso()}
    await coll(user).insert_one(doc)
    await _audit(user, "recurring_invoice.create", "recurring_invoice", doc["id"], {"name": body.name})
    doc.pop("_id", None)
    return doc


@router.patch("/{rid}")
async def update_template(rid: str, body: RecurringInvoiceBody,
                          user: dict = Depends(require("ops:manage"))):
    t = await coll(user).find_one({"id": rid})
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    if body.frequency not in ("weekly", "monthly"):
        raise HTTPException(status_code=422, detail="frequency must be weekly or monthly")
    await coll(user).update_one({"id": rid}, {"$set": body.model_dump()})
    await _audit(user, "recurring_invoice.update", "recurring_invoice", rid, {"name": body.name})
    return {"id": rid, **body.model_dump()}


@router.delete("/{rid}")
async def delete_template(rid: str, user: dict = Depends(require("ops:manage"))):
    res = await db.recurring_invoices.delete_one({"id": rid, "tenant_id": user["tenant_id"]})
    if not res.deleted_count:
        raise HTTPException(status_code=404, detail="Template not found")
    await _audit(user, "recurring_invoice.delete", "recurring_invoice", rid)
    return {"ok": True}


@router.post("/run-due")
async def run_due(user: dict = Depends(require("ops:manage"))):
    """Create issued invoices for every active template whose next_run has arrived,
    advancing each template by its frequency. Safe to call repeatedly — a template
    is only generated when next_run <= today."""
    today = today_str()
    made = []
    for t in await coll(user).find_list({"active": True, "next_run": {"$lte": today}}):
        try:
            inv = await issue_invoice(_materialize_body(t), user)
        except Exception as e:
            await _audit(user, "recurring_invoice.error", "recurring_invoice", t["id"],
                         {"error": str(e)})
            continue
        nxt = _advance(t["next_run"], t.get("frequency", "monthly"))
        await coll(user).update_one({"id": t["id"]}, {"$set": {"next_run": nxt}})
        await _audit(user, "recurring_invoice.generated", "invoice", inv["id"],
                     {"template": t["name"], "template_id": t["id"],
                      "total_cents": inv["total_cents"], "next_run": nxt})
        made.append({"template_id": t["id"], "template": t["name"],
                     "invoice_id": inv["id"], "total_cents": inv["total_cents"],
                     "next_run": nxt})
    return {"generated": len(made), "invoices": made}
