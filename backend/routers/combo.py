"""Momo Empire combo: AI receptionist + books + lead-gen, one subscription.

The receptionist books the job, the books do the books, lead-gen sits on top:
- Leads pipeline (call/text/web/referral/walk-in) with stages
- Receptionist interaction log per lead (every inbound touch captured)
- AI-drafted follow-ups (LLM when configured, smart template fallback)
- One-click convert: lead -> customer + estimate
- Funnel dashboard: stages, sources, conversion rate, revenue won
"""
import asyncio
import logging
import os
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso, cents_to_dollars

router = APIRouter(prefix="/combo", tags=["combo"])
logger = logging.getLogger(__name__)

STAGES = ["new", "contacted", "quoted", "won", "lost"]
SOURCES = ["call", "text", "web", "referral", "walk-in", "other"]


def _leads(user):
    return Scoped(db.combo_leads, user["tenant_id"])


def _interactions(user):
    return Scoped(db.combo_interactions, user["tenant_id"])


# ---------------- Leads ----------------
class LeadBody(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    source: str = "call"
    service_needed: str = ""
    notes: str = ""


@router.get("/leads")
async def list_leads(stage: str = "", user: dict = Depends(require("ledger:read"))):
    q = {"stage": stage} if stage in STAGES else {}
    return await _leads(user).find_list(q, sort=[("created_at", -1)], limit=500)


@router.post("/leads")
async def add_lead(body: LeadBody, user: dict = Depends(require("ops:manage"))):
    if body.source not in SOURCES:
        raise HTTPException(422, f"Unknown source: {body.source}")
    doc = {"id": new_id(), "name": body.name.strip(), "phone": body.phone,
           "email": body.email, "source": body.source,
           "service_needed": body.service_needed.strip(), "notes": body.notes.strip(),
           "stage": "new", "estimate_id": None, "customer_id": None,
           "created_at": now_iso()}
    await _leads(user).insert_one(doc)
    await audit(user, "combo.lead.add", "lead", doc["id"],
                {"name": doc["name"], "source": doc["source"]})
    doc.pop("_id", None)
    return doc


class LeadPatch(BaseModel):
    stage: Optional[str] = None
    notes: Optional[str] = None
    service_needed: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


@router.patch("/leads/{lead_id}")
async def update_lead(lead_id: str, body: LeadPatch, user: dict = Depends(require("ops:manage"))):
    lead = await _leads(user).find_one({"id": lead_id})
    if not lead:
        raise HTTPException(404, "Lead not found")
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if "stage" in upd and upd["stage"] not in STAGES:
        raise HTTPException(422, f"Unknown stage: {upd['stage']}")
    await _leads(user).update_one({"id": lead_id}, {"$set": upd})
    await audit(user, "combo.lead.update", "lead", lead_id, upd)
    return await _leads(user).find_one({"id": lead_id})


@router.delete("/leads/{lead_id}")
async def delete_lead(lead_id: str, user: dict = Depends(require("ops:manage"))):
    if not await _leads(user).delete({"id": lead_id}):
        raise HTTPException(404, "Lead not found")
    return {"ok": True}


# ---------------- Receptionist log ----------------
class InteractionBody(BaseModel):
    kind: str = "note"          # call | text | note
    direction: str = "inbound"  # inbound | outbound
    summary: str


@router.get("/leads/{lead_id}/interactions")
async def list_interactions(lead_id: str, user: dict = Depends(require("ledger:read"))):
    return await _interactions(user).find_list({"lead_id": lead_id},
                                               sort=[("created_at", -1)], limit=200)


@router.post("/leads/{lead_id}/interactions")
async def log_interaction(lead_id: str, body: InteractionBody,
                          user: dict = Depends(require("ops:manage"))):
    if not await _leads(user).find_one({"id": lead_id}):
        raise HTTPException(404, "Lead not found")
    if body.kind not in ("call", "text", "note") or body.direction not in ("inbound", "outbound"):
        raise HTTPException(422, "Bad kind/direction")
    doc = {"id": new_id(), "lead_id": lead_id, "kind": body.kind,
           "direction": body.direction, "summary": body.summary.strip(),
           "created_at": now_iso()}
    await _interactions(user).insert_one(doc)
    # any outbound touch moves a new lead to contacted
    lead = await _leads(user).find_one({"id": lead_id})
    if lead["stage"] == "new" and body.direction == "outbound":
        await _leads(user).update_one({"id": lead_id}, {"$set": {"stage": "contacted"}})
    await audit(user, "combo.interaction", "lead", lead_id,
                {"kind": body.kind, "direction": body.direction})
    doc.pop("_id", None)
    return doc


# ---------------- AI follow-up draft ----------------
def _template_followup(lead: dict) -> str:
    svc = lead.get("service_needed") or "the work you asked about"
    return (f"Hi {lead['name']}, thanks for reaching out about {svc}! "
            f"Wanted to follow up — happy to get you a quote or find a time that works. "
            f"Just reply here or call us back.")


@router.post("/leads/{lead_id}/draft-followup")
async def draft_followup(lead_id: str, user: dict = Depends(require("ops:manage"))):
    lead = await _leads(user).find_one({"id": lead_id})
    if not lead:
        raise HTTPException(404, "Lead not found")
    recent = await _interactions(user).find_list({"lead_id": lead_id},
                                                 sort=[("created_at", -1)], limit=5)
    key = os.environ.get("EMERGENT_LLM_KEY", "").strip()
    if not key:
        return {"draft": _template_followup(lead), "ai": False}
    hist = "\n".join(f"- [{i['kind']}/{i['direction']}] {i['summary']}" for i in recent)
    prompt = (f"Write a short, warm follow-up text message (under 300 characters) to a potential "
              f"customer named {lead['name']} who needs: {lead.get('service_needed') or 'general services'}. "
              f"Recent interactions:\n{hist or '(none yet)'}\nReply with ONLY the message text.")
    model = os.environ.get("AI_MODEL", "gpt-5-mini").strip()

    def _do():
        return requests.post("https://api.openai.com/v1/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "messages": [
                                 {"role": "system", "content": "You write follow-up texts for a local trades business. Warm, brief, no fluff."},
                                 {"role": "user", "content": prompt}],
                                   "temperature": 0.7, "max_tokens": 150},
                             timeout=45)
    try:
        r = await asyncio.to_thread(_do)
        text = r.json()["choices"][0]["message"]["content"].strip()
        return {"draft": text, "ai": True}
    except Exception as exc:
        logger.error("combo followup LLM failed: %s", exc)
        return {"draft": _template_followup(lead), "ai": False}


# ---------------- Convert lead -> customer + estimate ----------------
class ConvertBody(BaseModel):
    amount: float = 0
    description: str = ""
    revenue_code: str = "4020"


@router.post("/leads/{lead_id}/convert")
async def convert_lead(lead_id: str, body: ConvertBody,
                       user: dict = Depends(require("ops:manage"))):
    from routers.customers import CustomerBody
    from routers.estimates import EstimateBody, EstLine, create_estimate
    lead = await _leads(user).find_one({"id": lead_id})
    if not lead:
        raise HTTPException(404, "Lead not found")
    if lead.get("estimate_id"):
        raise HTTPException(409, "Lead already converted")
    # customer (reuse if phone/email matches)
    cust = None
    if lead.get("phone"):
        cust = await Scoped(db.customers, user["tenant_id"]).find_one({"phone": lead["phone"]})
    if not cust:
        cid = new_id()
        cust = {"id": cid, "name": lead["name"], "email": lead.get("email"),
                "phone": lead.get("phone"), "locations": [], "created_at": now_iso()}
        await Scoped(db.customers, user["tenant_id"]).insert_one(cust)
    desc = body.description or lead.get("service_needed") or "Quoted work"
    est = await create_estimate(EstimateBody(
        client_name=lead["name"], customer_id=cust["id"],
        lines=[EstLine(kind="service", name=desc,
                       revenue_code=body.revenue_code, amount=body.amount)],
        notes=f"Converted from {lead['source']} lead"), user)
    await _leads(user).update_one(
        {"id": lead_id},
        {"$set": {"stage": "quoted", "estimate_id": est["id"], "customer_id": cust["id"]}})
    await audit(user, "combo.lead.convert", "lead", lead_id,
                {"estimate_id": est["id"], "customer_id": cust["id"]})
    return {"lead_id": lead_id, "customer_id": cust["id"], "estimate": est}


# ---------------- Funnel ----------------
@router.get("/funnel")
async def funnel(user: dict = Depends(require("ledger:read"))):
    leads = await _leads(user).find_list(limit=2000)
    by_stage = {s: 0 for s in STAGES}
    by_source = {}
    won_revenue = 0
    for l in leads:
        by_stage[l.get("stage", "new")] = by_stage.get(l.get("stage", "new"), 0) + 1
        src = l.get("source", "other")
        by_source[src] = by_source.get(src, 0) + 1
    # revenue from estimates converted from leads that were won
    for l in leads:
        if l.get("stage") == "won" and l.get("estimate_id"):
            est = await Scoped(db.estimates, user["tenant_id"]).find_one({"id": l["estimate_id"]})
            if est:
                won_revenue += est.get("total_cents", 0)
    decided = by_stage["won"] + by_stage["lost"]
    conv = round(by_stage["won"] / decided * 100, 1) if decided else 0
    return {"total": len(leads), "by_stage": by_stage, "by_source": by_source,
            "conversion_pct": conv, "won_revenue_cents": won_revenue,
            "stages": STAGES}
