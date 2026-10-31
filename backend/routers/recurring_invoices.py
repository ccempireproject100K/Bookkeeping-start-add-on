"""Recurring invoice templates: schedule definitions that materialize real (issued)
invoices through the exact same issue logic as a manual invoice. Materialization
runs through ``run_due_for_user`` — invoked by the "generate due" endpoint and,
hourly, by the scheduler (lib/scheduler.run_recurring_invoices). Either way it
creates invoices for every active template whose next_run has arrived, then
advances next_run by the template's frequency.

Bake-off #2 (recurring automation): frequencies now span daily..annual plus a
custom day interval; templates carry end rules (never / after N runs / on a
date), pause, catch-up of missed occurrences (capped per run), a per-cycle
due-date offset, and an auto-send toggle that emails the generated invoice to
the customer on file (best-effort via lib/mailer). Implemented independently
in our stack (behavior study: Invoice Ninja + Akaunting docs)."""
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.audit import append_audit
from lib.base import Scoped, new_id, now_iso, today_str
from lib.auth import require, get_current_user
from routers.invoices import InvoiceBody, InvLine, issue_invoice

router = APIRouter(prefix="/recurring-invoices", tags=["recurring-invoices"])


def coll(user):
    return Scoped(db.recurring_invoices, user["tenant_id"])


async def _audit(user, action, entity, entity_id, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


FREQUENCIES = ("daily", "weekly", "biweekly", "monthly", "quarterly",
               "semiannual", "annual", "custom")


class RecurringInvoiceBody(BaseModel):
    name: str
    client_name: str
    customer_id: Optional[str] = None
    lines: List[InvLine]
    tax: float = 0
    due_date: Optional[str] = None
    terms: Optional[str] = "Net 30"
    frequency: str = "monthly"          # daily|weekly|biweekly|monthly|quarterly|semiannual|annual|custom
    interval_days: Optional[int] = None  # required when frequency == "custom"
    next_run: Optional[str] = None      # YYYY-MM-DD; defaults to today
    active: bool = True
    paused: bool = False
    end_type: str = "never"             # never | after_count | on_date
    end_count: Optional[int] = None
    end_date: Optional[str] = None
    auto_send: bool = False
    catch_up: bool = True
    due_offset_days: Optional[int] = None  # invoice due = run date + offset


def _add_months(d: date, months: int) -> date:
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return d.replace(year=year, month=month, day=min(d.day, 28))


def _advance(next_run: str, frequency: str, interval_days: Optional[int] = None) -> str:
    d = date.fromisoformat(next_run)
    if frequency == "daily":
        d = d + timedelta(days=1)
    elif frequency == "weekly":
        d = d + timedelta(days=7)
    elif frequency == "biweekly":
        d = d + timedelta(days=14)
    elif frequency == "quarterly":
        d = _add_months(d, 3)
    elif frequency == "semiannual":
        d = _add_months(d, 6)
    elif frequency == "annual":
        d = _add_months(d, 12)
    elif frequency == "custom":
        d = d + timedelta(days=max(1, int(interval_days or 30)))
    else:  # monthly (and legacy fallback)
        d = _add_months(d, 1)
    return d.isoformat()


def template_finished(t: dict, runs_made: int, next_run: str) -> bool:
    """Pure end-rule predicate: has this template reached its end?"""
    end_type = t.get("end_type") or "never"
    if end_type == "after_count" and t.get("end_count"):
        return runs_made >= int(t["end_count"])
    if end_type == "on_date" and t.get("end_date"):
        return next_run > t["end_date"]
    return False


def _validate_recurrence(body: RecurringInvoiceBody):
    if body.frequency not in FREQUENCIES:
        raise HTTPException(status_code=422,
                            detail=f"frequency must be one of {', '.join(FREQUENCIES)}")
    if body.frequency == "custom" and not (body.interval_days and body.interval_days >= 1):
        raise HTTPException(status_code=422, detail="interval_days (>= 1) is required for custom frequency")
    if body.end_type not in ("never", "after_count", "on_date"):
        raise HTTPException(status_code=422, detail="end_type must be never, after_count or on_date")
    if body.end_type == "after_count" and not (body.end_count and body.end_count >= 1):
        raise HTTPException(status_code=422, detail="end_count (>= 1) is required for end_type=after_count")
    if body.end_type == "on_date":
        if not body.end_date:
            raise HTTPException(status_code=422, detail="end_date is required for end_type=on_date")
        try:
            date.fromisoformat(body.end_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="end_date must be YYYY-MM-DD")


def _materialize_body(t: dict, run_date: str = None) -> InvoiceBody:
    due = t.get("due_date")
    if t.get("due_offset_days") is not None and run_date:
        due = (date.fromisoformat(run_date) + timedelta(days=int(t["due_offset_days"]))).isoformat()
    return InvoiceBody(
        client_name=t["client_name"], customer_id=t.get("customer_id"),
        lines=[InvLine(**l) for l in t["lines"]], tax=t.get("tax", 0),
        due_date=due, terms=t.get("terms", "Net 30"),
    )


async def _auto_send(user, t: dict, inv: dict):
    """Best-effort email of a generated invoice to the customer on file.
    Never raises; records the outcome on the invoice doc."""
    status = "skipped"
    try:
        from lib.mailer import send_email
        to = None
        if t.get("customer_id"):
            cust = await Scoped(db.customers, user["tenant_id"]).find_one({"id": t["customer_id"]})
            if cust:
                to = (cust.get("email") or "").strip() or None
        if not to:
            status = "no_recipient"
        else:
            total = inv["total_cents"] / 100
            st, _err = await send_email(
                to, f"Invoice from {t['client_name']} — ${total:,.2f}",
                f"Hi {t['client_name']},\n\nA new invoice for ${total:,.2f} has been "
                f"issued under your recurring plan \"{t['name']}\"."
                + (f"\nDue date: {inv.get('due_date')}." if inv.get("due_date") else "")
                + "\n\nThank you!")
            status = "sent" if st == "sent" else "queued"
    except Exception:
        status = "failed"
    try:
        await Scoped(db.invoices, user["tenant_id"]).update_one(
            {"id": inv["id"]}, {"$set": {"auto_send_status": status}})
    except Exception:
        pass
    return status


@router.get("")
async def list_templates(user: dict = Depends(get_current_user)):
    return await coll(user).find_list(sort=[("name", 1)], limit=500)


@router.post("")
async def create_template(body: RecurringInvoiceBody, user: dict = Depends(require("ops:manage"))):
    _validate_recurrence(body)
    if not body.lines:
        raise HTTPException(status_code=422, detail="At least one line is required")
    if body.next_run:
        try:
            date.fromisoformat(body.next_run)
        except ValueError:
            raise HTTPException(status_code=422, detail="next_run must be YYYY-MM-DD")
    doc = {"id": new_id(), **body.model_dump(), "runs_made": 0, "status": "active",
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
    _validate_recurrence(body)
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


async def run_due_for_user(user: dict) -> dict:
    """Create issued invoices for every active template of the user's tenant
    whose next_run has arrived, advancing each template by its frequency.
    Safe to call repeatedly — a template is only generated when
    next_run <= today. Shared by the manual endpoint below and the
    scheduler's hourly job (lib/scheduler.run_recurring_invoices).

    Bake-off #2 semantics: paused templates are skipped; end rules retire a
    template (status "completed", active False); catch_up generates every
    missed occurrence up to a per-run cap, otherwise a single occurrence."""
    today = today_str()
    made = []
    for t in await coll(user).find_list({"active": True, "next_run": {"$lte": today}}):
        if t.get("paused"):
            continue
        runs_made = int(t.get("runs_made") or 0)
        next_run = t["next_run"]
        if template_finished(t, runs_made, next_run):
            await coll(user).update_one({"id": t["id"]},
                                        {"$set": {"active": False, "status": "completed"}})
            continue
        cap = 24 if t.get("catch_up", True) else 1
        made_for_template = 0
        while next_run <= today and made_for_template < cap:
            if template_finished(t, runs_made, next_run):
                break
            try:
                inv = await issue_invoice(_materialize_body(t, next_run), user)
            except Exception as e:
                await _audit(user, "recurring_invoice.error", "recurring_invoice", t["id"],
                             {"error": str(e)})
                break
            runs_made += 1
            made_for_template += 1
            if t.get("auto_send"):
                await _auto_send(user, t, inv)
            nxt = _advance(next_run, t.get("frequency", "monthly"), t.get("interval_days"))
            await _audit(user, "recurring_invoice.generated", "invoice", inv["id"],
                         {"template": t["name"], "template_id": t["id"],
                          "total_cents": inv["total_cents"], "next_run": nxt})
            made.append({"template_id": t["id"], "template": t["name"],
                         "invoice_id": inv["id"], "total_cents": inv["total_cents"],
                         "run_date": next_run, "next_run": nxt})
            next_run = nxt
        finished = template_finished(t, runs_made, next_run)
        upd = {"next_run": next_run, "runs_made": runs_made, "last_run": today}
        if finished:
            upd.update({"active": False, "status": "completed"})
        await coll(user).update_one({"id": t["id"]}, {"$set": upd})
    return {"generated": len(made), "invoices": made}


@router.post("/run-due")
async def run_due(user: dict = Depends(require("ops:manage"))):
    return await run_due_for_user(user)
