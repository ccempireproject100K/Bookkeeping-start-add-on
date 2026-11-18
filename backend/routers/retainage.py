"""Retainage sub-ledger API — track what's held, by job and phase, on
both sides; record releases that happened outside Bookkeeping.

RECORD-ONLY lane (owner correction 2026-11-14): nothing here moves or
triggers money. Releases are markings — facts the shop enters — and
the UI copy says so plainly."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from db import db
from lib.base import Scoped, to_cents
from lib.auth import require, get_current_user
from lib import retainage as R

router = APIRouter(prefix="/retainage", tags=["retainage"])


async def _job_or_404(user, job_id):
    job = await Scoped(db.jobs, user["tenant_id"]).find_one({"id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("")
async def rollup(user: dict = Depends(get_current_user)):
    await R.check_reminders(user["tenant_id"])
    return await R.tenant_rollup(user["tenant_id"])


@router.get("/jobs/{job_id}")
async def job_retainage(job_id: str, user: dict = Depends(get_current_user)):
    job = await _job_or_404(user, job_id)
    await R.check_reminders(user["tenant_id"])
    return await R.job_summary(user["tenant_id"], job)


class SettingsBody(BaseModel):
    retainage_pct: Optional[float] = None           # held from us by the customer
    retainage_payable_pct: Optional[float] = None   # held by us from subs
    retainage_phase_pcts: Optional[dict] = None     # {phase_id: pct | {receivable, payable}}
    retainage_release_days: Optional[int] = None    # days after substantial completion
    retainage_release_due_date: Optional[str] = None
    substantial_completion_date: Optional[str] = None
    retainage_release_note: Optional[str] = None    # the release condition, in words


def _check_pct(v, label):
    if v is not None and not (0 <= v <= 100):
        raise HTTPException(status_code=422, detail=f"{label} must be between 0 and 100")


@router.post("/jobs/{job_id}/settings")
async def save_settings(job_id: str, body: SettingsBody,
                        user: dict = Depends(require("ops:manage"))):
    job = await _job_or_404(user, job_id)
    _check_pct(body.retainage_pct, "Retainage %")
    _check_pct(body.retainage_payable_pct, "Payable retainage %")
    if body.retainage_phase_pcts:
        for pid, raw in body.retainage_phase_pcts.items():
            if isinstance(raw, (int, float)):
                _check_pct(raw, f"Phase {pid} %")
            elif isinstance(raw, dict):
                for side in ("receivable", "payable"):
                    _check_pct(raw.get(side), f"Phase {pid} {side} %")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates:
        await Scoped(db.jobs, user["tenant_id"]).update_one(
            {"id": job_id}, {"$set": updates})
        job = await _job_or_404(user, job_id)
    return await R.job_summary(user["tenant_id"], job)


class MarkReleaseBody(BaseModel):
    amount: Optional[float] = None    # None = the full outstanding balance
    note: str = ""


@router.post("/entries/{entry_id}/mark-released")
async def mark_entry_released(entry_id: str, body: MarkReleaseBody,
                              user: dict = Depends(require("ops:manage"))):
    """Record that this hold was released outside Bookkeeping.
    Records the fact only — no payment is made or triggered."""
    entry = await R.entries_coll(user["tenant_id"]).find_one({"id": entry_id})
    if not entry:
        raise HTTPException(status_code=404, detail="Retainage entry not found")
    amount = to_cents(body.amount) if body.amount is not None else None
    return await R.mark_release(user["tenant_id"], user, entry,
                                amount, body.note)


class JobReleaseBody(BaseModel):
    side: str                          # receivable | payable
    amount: Optional[float] = None
    note: str = ""


@router.post("/jobs/{job_id}/mark-released")
async def mark_job_released(job_id: str, body: JobReleaseBody,
                            user: dict = Depends(require("ops:manage"))):
    """Record a release against a job's holds (oldest first).
    Records the fact only — no payment is made or triggered."""
    if body.side not in ("receivable", "payable"):
        raise HTTPException(status_code=422, detail="side must be receivable or payable")
    job = await _job_or_404(user, job_id)
    amount = to_cents(body.amount) if body.amount is not None else None
    marked = await R.mark_release_for_job(user["tenant_id"], user, job,
                                          body.side, amount, body.note)
    return {"marked": marked,
            "summary": await R.job_summary(user["tenant_id"], job)}
