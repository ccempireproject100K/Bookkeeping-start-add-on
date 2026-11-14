"""AIA-style progress billing endpoints (G702/G703 format) — an
additive billing mode per job: schedule of values, billing periods,
pay applications. Jobs without an SOV behave exactly as before.
Documents are labeled "AIA-style (G702/G703 format)" — never as
official AIA forms. Billing records only; no payment moves."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from typing import Optional, List, Dict

from db import db
from lib.base import Scoped, to_cents
from lib.auth import require, get_current_user
from lib import aia as A

router = APIRouter(prefix="/jobs", tags=["aia"])


async def _job(user, job_id):
    j = await Scoped(db.jobs, user["tenant_id"]).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    return j


def _clean(doc):
    return {k: v for k, v in (doc or {}).items() if k != "_id"}


# ---------------- schedule of values ----------------
class SovLineBody(BaseModel):
    item_no: str
    description: str = ""
    scheduled_value: float
    phase_id: Optional[str] = None
    retainage_pct: Optional[float] = None


class SovBody(BaseModel):
    lines: List[SovLineBody]


@router.get("/{job_id}/aia/sov")
async def get_sov(job_id: str, user: dict = Depends(get_current_user)):
    j = await _job(user, job_id)
    sov = await A.get_sov(user["tenant_id"], job_id)
    if not sov:
        return {"has_sov": False, "lines": [],
                "sum_check": A.sov_sum_check(
                    [], A.contract_figures(j)["contract_sum_to_date_cents"])}
    sov = _clean(sov)
    sov["has_sov"] = True
    sov["sum_check"] = A.sov_sum_check(
        sov["lines"], A.contract_figures(j)["contract_sum_to_date_cents"])
    for line in sov["lines"]:
        line["phase_name"] = A.phase_name(j, line.get("phase_id"))
    return sov


@router.put("/{job_id}/aia/sov")
async def put_sov(job_id: str, body: SovBody,
                  user: dict = Depends(require("ops:manage"))):
    j = await _job(user, job_id)
    phase_ids = {p["id"] for p in j.get("phases") or []}
    lines = []
    for raw in body.lines:
        if raw.phase_id and phase_ids and raw.phase_id not in phase_ids:
            raise HTTPException(status_code=422,
                                detail=f"Unknown phase {raw.phase_id}")
        lines.append({
            "item_no": raw.item_no, "description": raw.description,
            "scheduled_value_cents": to_cents(raw.scheduled_value),
            "phase_id": raw.phase_id, "retainage_pct": raw.retainage_pct,
        })
    try:
        doc = await A.save_sov(user["tenant_id"], user, j, lines)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _clean(doc)


# ---------------- optional mode / seed / history ----------------
class ModeBody(BaseModel):
    enabled: bool


@router.put("/{job_id}/aia/mode")
async def put_mode(job_id: str, body: ModeBody,
                   user: dict = Depends(require("ops:manage"))):
    j = await _job(user, job_id)
    res = await A.set_mode(user["tenant_id"], user, j, body.enabled)
    res["sov"] = _clean(res["sov"]) if res.get("sov") else None
    return res


@router.post("/{job_id}/aia/sov/seed")
async def seed_sov(job_id: str,
                   user: dict = Depends(require("ops:manage"))):
    j = await _job(user, job_id)
    existing = await A.get_sov(user["tenant_id"], job_id)
    if existing and existing.get("lines"):
        raise HTTPException(status_code=409,
                            detail="This job already has a schedule of "
                                   "values — edit it instead of reseeding")
    doc = await A.save_sov(user["tenant_id"], user, j,
                           A.seed_lines_from_estimate(j))
    return _clean(doc)


@router.get("/{job_id}/aia/history")
async def get_history(job_id: str,
                      user: dict = Depends(get_current_user)):
    await _job(user, job_id)
    periods = await A.list_periods(user["tenant_id"], job_id)
    return A.period_history(periods)


# ---------------- periods + application ----------------
class PeriodBody(BaseModel):
    label: str = ""
    period_from: str = ""
    period_to: str = ""


@router.get("/{job_id}/aia/periods")
async def get_periods(job_id: str, user: dict = Depends(get_current_user)):
    await _job(user, job_id)
    return [_clean(p) for p in
            await A.list_periods(user["tenant_id"], job_id)]


@router.post("/{job_id}/aia/periods")
async def create_period(job_id: str, body: PeriodBody,
                        user: dict = Depends(require("ops:manage"))):
    j = await _job(user, job_id)
    if not await A.get_sov(user["tenant_id"], job_id):
        raise HTTPException(status_code=409,
                            detail="Set a schedule of values first")
    try:
        return await A.open_period(user["tenant_id"], j, body.label,
                                   body.period_from, body.period_to)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


class EntriesBody(BaseModel):
    entries: Dict[str, dict]   # line_id -> {this_period, stored} dollars


@router.put("/{job_id}/aia/periods/{period_id}/entries")
async def put_entries(job_id: str, period_id: str, body: EntriesBody,
                      user: dict = Depends(require("ops:manage"))):
    await _job(user, job_id)
    period = await A.get_period(user["tenant_id"], period_id)
    if not period or period["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Period not found")
    sov = await A.get_sov(user["tenant_id"], job_id)
    entries = {}
    for line_id, e in body.entries.items():
        entries[line_id] = {
            "this_period_cents": to_cents(e.get("this_period", 0) or 0),
            "stored_cents": to_cents(e.get("stored", 0) or 0),
        }
    try:
        period = await A.set_entries(user["tenant_id"], period, entries,
                                     sov["lines"] if sov else [])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _clean(period)


@router.get("/{job_id}/aia/application")
async def get_application(job_id: str,
                          user: dict = Depends(get_current_user)):
    j = await _job(user, job_id)
    live = await A.live_application(user["tenant_id"], j)
    live["period"] = _clean(live["period"]) if live.get("period") else None
    return live


@router.post("/{job_id}/aia/periods/{period_id}/close")
async def close_period_ep(job_id: str, period_id: str,
                          user: dict = Depends(require("ops:manage"))):
    j = await _job(user, job_id)
    period = await A.get_period(user["tenant_id"], period_id)
    if not period or period["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Period not found")
    try:
        closed = await A.close_period(user["tenant_id"], user, j, period)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _clean(closed)


@router.get("/{job_id}/aia/periods/{period_id}/document")
async def period_document(job_id: str, period_id: str,
                          user: dict = Depends(get_current_user)):
    j = await _job(user, job_id)
    period = await A.get_period(user["tenant_id"], period_id)
    if not period or period["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Period not found")
    if period.get("application"):
        app = period["application"]
    else:
        live = await A.live_application(user["tenant_id"], j)
        app = live.get("application")
        if not app:
            raise HTTPException(status_code=409,
                                detail="No application for this period")
    lines = A.document_lines(j, app, period)
    pdf = A.pdf_bytes(
        f"{A.FORMAT_LABEL} — Application #{period['period_no']}", lines)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f"attachment; filename=pay-app-"
                             f"{j['job_number']}-{period['period_no']}.pdf"})
