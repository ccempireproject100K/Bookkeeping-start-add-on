"""Time tracking: clock in/out, manual entries, timesheets, approvals.

Old-style port of the public time-tracking feature. Flow:
  clock in -> open entry -> clock out -> logged -> approve -> approved -> close -> closed
Owners (role in the `time:approve` permission) can view everyone's entries and
approve/close them; everyone else sees only their own.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from db import db
from lib.base import Scoped, new_id, now_iso, today_str
from lib.auth import PERMISSIONS, require

router = APIRouter(prefix="/time", tags=["time"])


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


def coll(user):
    return Scoped(db.time_entries, user["tenant_id"])


def _can_approve(user) -> bool:
    return user.get("role") in PERMISSIONS.get("time:approve", set())


class ClockInBody(BaseModel):
    notes: str = Field(default="", max_length=300)
    client_name: str = Field(default="", max_length=160)


class ManualEntryBody(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    hours: float = Field(gt=0, le=24)
    notes: str = Field(default="", max_length=300)
    client_name: str = Field(default="", max_length=160)
    billable: bool = True


def _out(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "user_id", "user_name", "date", "clock_in",
                                 "clock_out", "hours", "notes", "client_name", "billable",
                                 "status", "approved_by", "approved_at", "closed_at", "created_at")}


@router.post("/clock-in")
async def clock_in(body: ClockInBody, user: dict = Depends(require("time:manage"))):
    if await coll(user).find_one({"user_id": user["id"], "status": "open"}):
        raise HTTPException(status_code=409, detail="Already clocked in")
    doc = {
        "id": new_id(), "user_id": user["id"], "user_name": user.get("name"),
        "date": today_str(), "clock_in": now_iso(), "clock_out": None, "hours": 0.0,
        "notes": body.notes, "client_name": body.client_name, "billable": True,
        "status": "open", "approved_by": None, "approved_at": None,
        "closed_at": None, "created_at": now_iso(),
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
    return [_out(r) for r in rows]


@router.post("/entries")
async def log_entry(body: ManualEntryBody, user: dict = Depends(require("time:manage"))):
    doc = {
        "id": new_id(), "user_id": user["id"], "user_name": user.get("name"),
        "date": body.date, "clock_in": None, "clock_out": None, "hours": round(body.hours, 2),
        "notes": body.notes, "client_name": body.client_name, "billable": body.billable,
        "status": "logged", "approved_by": None, "approved_at": None,
        "closed_at": None, "created_at": now_iso(),
    }
    await coll(user).insert_one(doc)
    await _audit(user, "time.create", "time_entry", doc["id"], {"hours": doc["hours"]})
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
        "entries": [_out(r) for r in rows],
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
