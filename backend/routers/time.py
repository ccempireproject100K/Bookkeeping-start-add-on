"""Time tracking: clock in/out, manual entries, timesheets, approvals.

Old-style port of the public time-tracking feature. Flow:
  clock in -> open entry -> clock out -> logged -> approve -> approved -> close -> closed
Owners (role in the `time:approve` permission) can view everyone's entries and
approve/close them; everyone else sees only their own.

Field capture (job-costing requests #3): an entry may carry an optional job
link (`job_id`). When a job-linked entry is APPROVED, its labor cost
(hours x the entry's rate) posts into that job's actual labor costs through
the jobs module's one cost-posting path (routers.jobs.record_job_cost) — the
job's numbers are current the moment the approval lands, no re-keying.
Editing an approved entry re-syncs its job cost line (reverse + repost, never
a duplicate); unlinking removes it. Unlinked entries behave exactly as before.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from db import db
from lib.audit import append_audit
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import PERMISSIONS, require
from lib import ledger as L
from lib import burden as BURDEN
from lib import jobcosting as JC

router = APIRouter(prefix="/time", tags=["time"])


async def _with_labor_costs(user, rows):
    """Attach a burdened labor-cost preview to each entry (hours x
    payroll rate, burden applied) so the true cost shows on the
    timesheet. Entries get NO extra key when no burden is configured
    for the worker — the response is then exactly as before."""
    basis_cache = {}
    out = []
    for r in rows:
        o = _out(r)
        uid = r.get("user_id")
        if uid not in basis_cache:
            basis_cache[uid] = await JC.user_labor_basis(user["tenant_id"], uid)
        preview = await JC.entry_labor_preview(
            user["tenant_id"], uid, r.get("hours"), basis=basis_cache[uid])
        if preview:
            o["labor_cost"] = preview
        out.append(o)
    return out


async def _audit(user, action, entity, entity_id, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


def coll(user):
    return Scoped(db.time_entries, user["tenant_id"])


def jobcosts(user):
    return Scoped(db.job_costs, user["tenant_id"])


async def _resolve_job(user, job_id):
    """Tenant-scoped job lookup for a link target; None when absent."""
    if not job_id:
        return None
    return await Scoped(db.jobs, user["tenant_id"]).find_one({"id": job_id})


async def _default_rate_cents(user) -> int:
    """The entry's labor rate: the worker's pay-settings hourly rate, if set."""
    ps = user.get("pay_settings") or {}
    if ps.get("hourly_rate_cents"):
        return int(ps["hourly_rate_cents"])
    doc = await db.users.find_one(
        {"tenant_id": user["tenant_id"], "id": user["id"]}, {"_id": 0}) or {}
    return int((doc.get("pay_settings") or {}).get("hourly_rate_cents") or 0)


# ---------------- Job-cost sync (field capture) ----------------
async def _unpost_job_cost(user, entry):
    """Reverse the journal entry behind the entry's job cost line and drop the
    line, so an edit/unlink never leaves a stale or duplicate cost behind."""
    cost_id = entry.get("job_cost_id")
    if not cost_id:
        return
    row = await jobcosts(user).find_one({"id": cost_id})
    if row:
        entry_id = row.get("journal_entry_id")
        if entry_id:
            je = await Scoped(db.journal_entries, user["tenant_id"]).find_one({"id": entry_id})
            if je and je.get("status") == "posted":
                await L.reverse_entry(db, user["tenant_id"], user, entry_id)
        await jobcosts(user).c.delete_one(
            {"tenant_id": user["tenant_id"], "id": cost_id})
    await coll(user).update_one({"id": entry["id"]}, {"$set": {
        "job_cost_id": None, "job_cost_posted_cents": None, "burden_percent": None}})
    entry["job_cost_id"] = None
    entry["job_cost_posted_cents"] = None
    entry["burden_percent"] = None


async def sync_entry_job_cost(user, entry):
    """Make the job's actual labor match this entry's current state.

    Posts exactly one job cost line for an approved, job-linked entry with
    hours and a rate; removes the line when the entry no longer qualifies;
    reverse + reposts when hours/rate/job changed since posting. Idempotent —
    calling it on an unchanged entry is a no-op (returns the entry unchanged).
    """
    from routers.jobs import record_job_cost

    job_id = entry.get("job_id")
    eligible = bool(
        job_id
        and entry.get("status") in ("approved", "closed")
        and (entry.get("hours") or 0) > 0
        and (entry.get("rate_cents") or 0) > 0
    )
    if not eligible:
        if entry.get("job_cost_id"):
            await _unpost_job_cost(user, entry)
        return entry

    job = await _resolve_job(user, job_id)
    if not job:
        # The job was deleted out from under the entry — drop the stale line.
        if entry.get("job_cost_id"):
            await _unpost_job_cost(user, entry)
        return entry

    amount = int(round(entry["hours"] * entry["rate_cents"]))
    if entry.get("job_cost_id"):
        row = await jobcosts(user).find_one({"id": entry["job_cost_id"]})
        if (row and row.get("job_id") == job_id
                and row.get("amount_cents") == amount
                and row.get("hours") == entry["hours"]
                and row.get("rate") == entry["rate_cents"]):
            return entry   # already posted, unchanged — never duplicate
        await _unpost_job_cost(user, entry)

    burden_pct = await BURDEN.tenant_burden_percent(db, user["tenant_id"])
    desc = (entry.get("notes") or entry.get("client_name") or "Field time").strip()
    doc = await record_job_cost(
        user, job, category="labor",
        description=f"{entry.get('user_name') or 'Tech'} — {desc}",
        amount_cents=amount, hours=entry["hours"], rate_cents=entry["rate_cents"],
        technician_id=entry.get("user_id"), paid=False,
        source="time_entry", source_ref=entry["id"], burden_percent=burden_pct)
    await coll(user).update_one({"id": entry["id"]}, {"$set": {
        "job_cost_id": doc["id"], "job_cost_posted_cents": amount,
        "burden_percent": burden_pct}})
    entry["job_cost_id"] = doc["id"]
    entry["job_cost_posted_cents"] = amount
    entry["burden_percent"] = burden_pct
    return entry


def _can_approve(user) -> bool:
    return user.get("role") in PERMISSIONS.get("time:approve", set())


class ClockInBody(BaseModel):
    notes: str = Field(default="", max_length=300)
    client_name: str = Field(default="", max_length=160)
    job_id: Optional[str] = Field(default=None, max_length=64)  # optional job link
    rate: Optional[float] = Field(default=None, ge=0, le=10000)   # $/hr override
    billable_rate: Optional[float] = Field(default=None, ge=0, le=10000)  # $/hr charged to the client


class ManualEntryBody(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    hours: float = Field(gt=0, le=24)
    notes: str = Field(default="", max_length=300)
    client_name: str = Field(default="", max_length=160)
    billable: bool = True
    job_id: Optional[str] = Field(default=None, max_length=64)  # optional job link
    rate: Optional[float] = Field(default=None, ge=0, le=10000)   # $/hr override
    billable_rate: Optional[float] = Field(default=None, ge=0, le=10000)  # $/hr charged to the client


class EntryPatchBody(BaseModel):
    hours: Optional[float] = Field(default=None, gt=0, le=24)
    notes: Optional[str] = Field(default=None, max_length=300)
    client_name: Optional[str] = Field(default=None, max_length=160)
    billable: Optional[bool] = None
    rate: Optional[float] = Field(default=None, ge=0, le=10000)
    billable_rate: Optional[float] = Field(default=None, ge=0, le=10000)
    job_id: Optional[str] = None     # link/relink to this job
    clear_job: bool = False          # unlink from the job


def _out(d: dict) -> dict:
    out = {k: d.get(k) for k in ("id", "user_id", "user_name", "date", "clock_in",
                                 "clock_out", "hours", "notes", "client_name", "billable",
                                 "job_id", "status", "approved_by", "approved_at",
                                 "closed_at", "created_at",
                                 "job_cost_id", "job_cost_posted_cents",
                                 "burden_percent", "billed_invoice_id", "billed_at",
                                 "billed_rate_cents")}
    out["rate"] = cents_to_dollars(d.get("rate_cents") or 0)
    out["rate_cents"] = d.get("rate_cents") or 0
    out["billable_rate_cents"] = d.get("billable_rate_cents")
    out["billable_rate"] = (cents_to_dollars(d["billable_rate_cents"])
                            if d.get("billable_rate_cents") else None)
    return out


async def _new_entry_fields(user, body) -> dict:
    """Shared job-link + rate resolution for clock-in and manual entries."""
    job_id = body.job_id or None
    if job_id and not await _resolve_job(user, job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    rate_cents = to_cents(body.rate) if body.rate is not None else await _default_rate_cents(user)
    return {"job_id": job_id, "rate_cents": rate_cents,
            "billable_rate_cents": (to_cents(body.billable_rate)
                                    if getattr(body, "billable_rate", None) else None),
            "billed_invoice_id": None, "billed_at": None, "billed_rate_cents": None,
            "billing_history": [],
            "job_cost_id": None, "job_cost_posted_cents": None, "burden_percent": None}


@router.post("/clock-in")
async def clock_in(body: ClockInBody, user: dict = Depends(require("time:manage"))):
    if await coll(user).find_one({"user_id": user["id"], "status": "open"}):
        raise HTTPException(status_code=409, detail="Already clocked in")
    doc = {
        "id": new_id(), "user_id": user["id"], "user_name": user.get("name"),
        "date": today_str(), "clock_in": now_iso(), "clock_out": None, "hours": 0.0,
        "notes": body.notes, "client_name": body.client_name, "billable": True,
        "job_id": body.job_id or None,
        "status": "open", "approved_by": None, "approved_at": None,
        "closed_at": None, "created_at": now_iso(),
        **await _new_entry_fields(user, body),
    }
    await coll(user).insert_one(doc)
    await _audit(user, "time.clock_in", "time_entry", doc["id"], {"client": body.client_name})
    return _out(doc)


@router.post("/clock-out")
async def clock_out(user: dict = Depends(require("time:manage"))):
    doc = await coll(user).find_one({"user_id": user["id"], "status": "open"})
    if not doc:
        raise HTTPException(status_code=404, detail="Not clocked in")
    out = datetime.now(timezone.utc)
    inn = datetime.fromisoformat(doc["clock_in"])
    hours = round((out - inn).total_seconds() / 3600, 2)
    await coll(user).update_one({"id": doc["id"]}, {"$set": {
        "clock_out": out.isoformat(), "hours": hours, "status": "logged"}})
    await _audit(user, "time.clock_out", "time_entry", doc["id"], {"hours": hours})
    doc.update(clock_out=out.isoformat(), hours=hours, status="logged")
    return _out(doc)


@router.get("/entries")
async def list_entries(user_id: Optional[str] = None, date_from: Optional[str] = None,
                       date_to: Optional[str] = None, user: dict = Depends(require("time:manage"))):
    f = {}
    if _can_approve(user) and user_id:
        f["user_id"] = user_id
    else:
        f["user_id"] = user["id"]
    if date_from:
        f["date"] = {**f.get("date", {}), "$gte": date_from}
    if date_to:
        f["date"] = {**f.get("date", {}), "$lte": date_to}
    rows = await coll(user).find_list(f, sort=[("date", -1), ("created_at", -1)], limit=2000)
    return await _with_labor_costs(user, rows)


@router.post("/entries")
async def log_entry(body: ManualEntryBody, user: dict = Depends(require("time:manage"))):
    doc = {
        "id": new_id(), "user_id": user["id"], "user_name": user.get("name"),
        "date": body.date, "clock_in": None, "clock_out": None, "hours": round(body.hours, 2),
        "notes": body.notes, "client_name": body.client_name, "billable": body.billable,
        "job_id": body.job_id or None,
        "status": "logged", "approved_by": None, "approved_at": None,
        "closed_at": None, "created_at": now_iso(),
        **await _new_entry_fields(user, body),
    }
    await coll(user).insert_one(doc)
    await _audit(user, "time.create", "time_entry", doc["id"], {"hours": doc["hours"]})
    return _out(doc)


@router.patch("/entries/{entry_id}")
async def update_entry(entry_id: str, body: EntryPatchBody, user: dict = Depends(require("time:manage"))):
    """Edit an entry (hours/notes/client/rate/job link). Non-approvers may only
    edit their own entries. Editing an APPROVED entry re-syncs its job cost
    line — the line is updated (reverse + repost), never duplicated."""
    doc = await coll(user).find_one({"id": entry_id})
    if not doc or (not _can_approve(user) and doc["user_id"] != user["id"]):
        raise HTTPException(status_code=404, detail="Entry not found")
    if doc["status"] == "closed":
        raise HTTPException(status_code=409, detail="Closed entries can't be edited")
    patch = {}
    if body.hours is not None:
        if doc["status"] == "open":
            raise HTTPException(status_code=409, detail="Clock out before setting hours")
        patch["hours"] = round(body.hours, 2)
    if body.notes is not None:
        patch["notes"] = body.notes
    if body.client_name is not None:
        patch["client_name"] = body.client_name
    if body.billable is not None:
        patch["billable"] = body.billable
    if body.rate is not None:
        patch["rate_cents"] = to_cents(body.rate)
    if body.billable_rate is not None:
        patch["billable_rate_cents"] = to_cents(body.billable_rate) or None
    if body.clear_job:
        patch["job_id"] = None
    elif body.job_id is not None:
        if not await _resolve_job(user, body.job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        patch["job_id"] = body.job_id
    if patch:
        await coll(user).update_one({"id": entry_id}, {"$set": patch})
        doc.update(patch)
        await _audit(user, "time.update", "time_entry", entry_id, {"fields": sorted(patch)})
        doc = await sync_entry_job_cost(user, doc)
    return _out(doc)


@router.get("/timesheet")
async def timesheet(period: Optional[str] = None, user_id: Optional[str] = None,
                    user: dict = Depends(require("time:manage"))):
    """Per-user, per-period timesheet: entries + hour/status totals.

    Non-approvers always get their own; approvers (owner) may pass user_id.
    """
    period = period or today_str()[:7]
    if len(period) != 7 or period[4] != "-":
        raise HTTPException(status_code=422, detail="period must be YYYY-MM")
    if user_id and not _can_approve(user):
        raise HTTPException(status_code=403, detail="Only approvers can view other timesheets")
    target = user_id or user["id"]
    rows = await coll(user).find_list(
        {"user_id": target, "date": {"$gte": f"{period}-01", "$lte": f"{period}-31"}},
        sort=[("date", 1), ("created_at", 1)], limit=2000)
    total = round(sum(r.get("hours") or 0 for r in rows), 2)
    by_status = {}
    for r in rows:
        by_status[r.get("status", "?")] = round(by_status.get(r.get("status", "?"), 0) + (r.get("hours") or 0), 2)
    name = rows[0].get("user_name") if rows else None
    return {
        "user_id": target, "user_name": name, "period": period,
        "total_hours": total, "hours_by_status": by_status,
        "entries": await _with_labor_costs(user, rows),
    }


@router.post("/entries/{entry_id}/approve")
async def approve_entry(entry_id: str, user: dict = Depends(require("time:approve"))):
    doc = await coll(user).find_one({"id": entry_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Entry not found")
    if doc["status"] != "logged":
        raise HTTPException(status_code=409, detail=f"Only logged entries can be approved (is {doc['status']})")
    await coll(user).update_one({"id": entry_id}, {"$set": {
        "status": "approved", "approved_by": user.get("name"), "approved_at": now_iso()}})
    await _audit(user, "time.approve", "time_entry", entry_id, {})
    # Field capture: a job-linked entry's labor lands on the job's actuals
    # the moment it is approved — same request, no batch step.
    doc["status"] = "approved"
    await sync_entry_job_cost(user, doc)
    return {"ok": True}


@router.post("/entries/{entry_id}/close")
async def close_entry(entry_id: str, user: dict = Depends(require("time:approve"))):
    doc = await coll(user).find_one({"id": entry_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Entry not found")
    if doc["status"] != "approved":
        raise HTTPException(status_code=409, detail=f"Only approved entries can be closed (is {doc['status']})")
    await coll(user).update_one({"id": entry_id}, {"$set": {"status": "closed", "closed_at": now_iso()}})
    await _audit(user, "time.close", "time_entry", entry_id, {})
    return {"ok": True}
