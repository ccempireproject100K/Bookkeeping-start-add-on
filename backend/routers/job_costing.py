"""Job costing settings + commitments API (see lib/jobcosting.py).

Owner-facing surface for the two owner-approved additions:
  * committed costs toggle + subcontract commitments, and
  * burdened labor: default rate, per-worker templates, the
    learned-from-payroll suggestion, and the labor view option.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional

from db import db
from lib import ledger as L
from lib import jobcosting as JC
from lib.auth import require
from lib.base import Scoped, new_id, now_iso, to_cents

router = APIRouter(prefix="/job-costing", tags=["job-costing"])


# ---------------- config ----------------

class ConfigIn(BaseModel):
    committed_costs_enabled: Optional[bool] = None
    labor_view: Optional[str] = Field(default=None, pattern=r"^(base|burdened|both)$")
    default_burden_pct: Optional[float] = Field(default=None, ge=0, le=200)


@router.get("/config")
async def get_config_ep(user: dict = Depends(require("ledger:read"))):
    cfg = await JC.get_config(user["tenant_id"])
    cfg["templates"] = await JC.list_templates(user["tenant_id"])
    return cfg


@router.patch("/config")
async def patch_config_ep(body: ConfigIn, user: dict = Depends(require("ops:manage"))):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if patch:
        await JC.save_config(user["tenant_id"], patch)
        await L.audit(db, user["tenant_id"], user, "job_costing.config",
                      {"patch": patch})
    cfg = await JC.get_config(user["tenant_id"])
    cfg["templates"] = await JC.list_templates(user["tenant_id"])
    return cfg


# ---------------- burden templates ----------------

class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(default="employee", pattern=r"^(employee|sub|custom)$")
    pct: float = Field(ge=0, le=200)
    member_user_ids: List[str] = []


def _tpl_out(doc):
    return {k: v for k, v in doc.items() if k not in ("_id", "tenant_id")}


@router.post("/burden-templates")
async def create_template(body: TemplateIn, user: dict = Depends(require("ops:manage"))):
    doc = {"id": new_id(), **body.model_dump(), "created_at": now_iso()}
    await Scoped(db.burden_templates, user["tenant_id"]).insert_one(doc)
    await L.audit(db, user["tenant_id"], user, "job_costing.template.create",
                  {"name": doc["name"], "pct": doc["pct"]})
    return _tpl_out(doc)


@router.put("/burden-templates/{tid}")
async def update_template(tid: str, body: TemplateIn,
                          user: dict = Depends(require("ops:manage"))):
    coll = Scoped(db.burden_templates, user["tenant_id"])
    if not await coll.find_one({"id": tid}):
        raise HTTPException(status_code=404, detail="Template not found")
    await coll.update_one({"id": tid}, {"$set": body.model_dump()})
    return _tpl_out(await coll.find_one({"id": tid}))


@router.delete("/burden-templates/{tid}")
async def delete_template(tid: str, user: dict = Depends(require("ops:manage"))):
    coll = Scoped(db.burden_templates, user["tenant_id"])
    if not await coll.find_one({"id": tid}):
        raise HTTPException(status_code=404, detail="Template not found")
    await coll.c.delete_one({"tenant_id": user["tenant_id"], "id": tid})
    await L.audit(db, user["tenant_id"], user, "job_costing.template.delete",
                  {"id": tid})
    return {"ok": True}


# ---------------- actual burden suggestion ----------------

@router.get("/burden-suggestion")
async def burden_suggestion_ep(user: dict = Depends(require("ledger:read"))):
    return await JC.burden_suggestion(user["tenant_id"])


# ---------------- subcontract commitments ----------------

class CommitmentIn(BaseModel):
    vendor_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=300)
    amount: float = Field(gt=0)


async def _get_job_or_404(user, job_id):
    job = await Scoped(db.jobs, user["tenant_id"]).find_one({"id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/jobs/{job_id}/commitments")
async def create_commitment(job_id: str, body: CommitmentIn,
                            user: dict = Depends(require("ops:manage"))):
    cfg = await JC.get_config(user["tenant_id"])
    if not cfg["committed_costs_enabled"]:
        raise HTTPException(status_code=409,
                            detail="Committed costs are off for this shop — "
                                   "turn them on in Job Costing settings first")
    await _get_job_or_404(user, job_id)
    doc = {"id": new_id(), "job_id": job_id,
           "vendor_name": body.vendor_name.strip(),
           "description": body.description.strip(),
           "amount_cents": to_cents(body.amount), "fulfilled_cents": 0,
           "status": "approved", "created_at": now_iso()}
    await Scoped(db.job_commitments, user["tenant_id"]).insert_one(doc)
    await L.audit(db, user["tenant_id"], user, "job_costing.commitment.create",
                  {"job": job_id, "vendor": doc["vendor_name"],
                   "amount_cents": doc["amount_cents"]})
    return {k: v for k, v in doc.items() if k not in ("_id", "tenant_id")}


@router.get("/jobs/{job_id}/commitments")
async def list_commitments(job_id: str, user: dict = Depends(require("ledger:read"))):
    await _get_job_or_404(user, job_id)
    return await JC.job_commitments(user["tenant_id"], job_id)


@router.post("/commitments/{cid}/cancel")
async def cancel_commitment(cid: str, user: dict = Depends(require("ops:manage"))):
    coll = Scoped(db.job_commitments, user["tenant_id"])
    doc = await coll.find_one({"id": cid})
    if not doc:
        raise HTTPException(status_code=404, detail="Commitment not found")
    if doc["status"] != "approved":
        raise HTTPException(status_code=409,
                            detail=f"Commitment is {doc['status']}")
    await coll.update_one({"id": cid}, {"$set": {"status": "cancelled"}})
    return await JC.job_commitments(user["tenant_id"], doc["job_id"])


# ---------------- per-job budget view ----------------

@router.get("/jobs/{job_id}/budget")
async def job_budget(job_id: str, user: dict = Depends(require("ledger:read"))):
    from routers.jobs import _profit
    job = await _get_job_or_404(user, job_id)
    profit = await _profit(user, job)
    return await JC.budget_view(user["tenant_id"], job, profit)
