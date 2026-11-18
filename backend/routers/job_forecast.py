"""Job forecast endpoints (job-costing #10): live forecast with
baseline vs forecast (A/C/D), what-if sandbox (E), snapshots +
history (F). Suggestion + estimate->job conversion live in
routers/estimates.py; the math lives in lib/jobforecast.py."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.auth import get_current_user
from lib.base import Scoped, to_cents
from lib import jobforecast as JF

router = APIRouter(prefix="/jobs", tags=["job-forecast"])


def jobs(user):
    return Scoped(db.jobs, user["tenant_id"])


async def _job_or_404(job_id, user):
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(404, "Job not found")
    return j


@router.get("/{job_id}/forecast")
async def get_forecast(job_id: str, user: dict = Depends(get_current_user)):
    j = await _job_or_404(job_id, user)
    fc = await JF.load_forecast(user["tenant_id"], j)
    # Feature F: a read is where material movement gets recorded.
    await JF.maybe_snapshot(user["tenant_id"], user, j, fc)
    return fc


class WhatIfBody(BaseModel):
    remaining: dict = {}   # category -> dollars still expected to spend


@router.post("/{job_id}/forecast/what-if")
async def what_if(job_id: str, body: WhatIfBody,
                  user: dict = Depends(get_current_user)):
    """Feature E: pure sandbox. Computes with the caller's remaining
    figures and writes NOTHING — no snapshot, no job change."""
    j = await _job_or_404(job_id, user)
    overrides = {c: to_cents(v) for c, v in (body.remaining or {}).items()
                 if c in JF.CATEGORIES}
    return await JF.load_forecast(user["tenant_id"], j, overrides)


class SnapshotBody(BaseModel):
    reason: Optional[str] = None


@router.post("/{job_id}/forecast/snapshot")
async def force_snapshot(job_id: str, body: SnapshotBody = SnapshotBody(),
                         user: dict = Depends(get_current_user)):
    j = await _job_or_404(job_id, user)
    fc = await JF.load_forecast(user["tenant_id"], j)
    return await JF.take_snapshot(user["tenant_id"], user, j, fc,
                                  body.reason or "manual")


@router.get("/{job_id}/forecast/history")
async def get_history(job_id: str, user: dict = Depends(get_current_user)):
    j = await _job_or_404(job_id, user)
    return await JF.forecast_history(user["tenant_id"], j)
