"""Onboarding wizard + setup checklist (bake-off #10).

An ordered first-run path wrapped around what already exists (niche packs,
bank connections, customers, invoices): each step's completion is either
computed live from the tenant's data or marked manually; progress persists
per tenant and the checklist can be dismissed. Implemented independently
in our stack (behavior study: Akaunting Help Center setup flow).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import require, get_current_user

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# (key, title, hint, kind) — kind "auto" computes from data, "manual" is a
# checkbox the owner ticks (e.g. "logo uploaded" lives outside the books).
STEPS = [
    ("business_profile", "Name your business", "Business name and contact details", "auto"),
    ("trade_pack", "Confirm your trade pack", "Your chart of accounts is pre-loaded for your trade", "auto"),
    ("tax_profile", "Set your tax basics", "Sales-tax jurisdiction or quarterly estimates profile", "manual"),
    ("connect_bank", "Connect or add a bank account", "Bank feed or a manual account so transactions can flow in", "auto"),
    ("first_customer", "Add your first customer", "Someone you invoice", "auto"),
    ("first_invoice", "Send your first invoice", "Issue an invoice — deposits and portal payment links included", "auto"),
    ("review_categories", "Review your categories", "Check the chart matches how you actually spend", "manual"),
]


def progress_coll(user):
    return Scoped(db.onboarding_progress, user["tenant_id"])


async def _auto_state(user) -> dict:
    tid = user["tenant_id"]
    tenant = await db.tenants.find_one({"id": tid}, {"_id": 0}) or {}
    bank_accounts = await Scoped(db.bank_accounts, tid).count()
    bank_conns = 0
    try:
        bank_conns = await db.bank_connections.count_documents({"tenant_id": tid})
    except Exception:
        bank_conns = 0
    customers = await Scoped(db.customers, tid).count()
    invoices = await Scoped(db.invoices, tid).count()
    return {
        "business_profile": bool((tenant.get("name") or "").strip()),
        "trade_pack": bool(tenant.get("niche_id")),
        "connect_bank": (bank_accounts + bank_conns) > 0,
        "first_customer": customers > 0,
        "first_invoice": invoices > 0,
    }


@router.get("")
async def get_onboarding(user: dict = Depends(get_current_user)):
    doc = await progress_coll(user).find_one({"id": "progress"}) or {}
    manual_done = set(doc.get("manual_completed") or [])
    auto = await _auto_state(user)
    steps = []
    for key, title, hint, kind in STEPS:
        done = auto.get(key, False) if kind == "auto" else key in manual_done
        steps.append({"key": key, "title": title, "hint": hint,
                      "kind": kind, "done": bool(done)})
    done_count = sum(1 for s in steps if s["done"])
    return {
        "steps": steps,
        "done": done_count, "total": len(steps),
        "complete": done_count == len(steps),
        "dismissed": bool(doc.get("dismissed")),
        "next_step": next((s for s in steps if not s["done"]), None),
    }


class CompleteIn(BaseModel):
    step: str


async def _progress_doc(user):
    coll = progress_coll(user)
    doc = await coll.find_one({"id": "progress"})
    if not doc:
        doc = {"id": "progress", "manual_completed": [], "dismissed": False,
               "created_at": now_iso()}
        await coll.insert_one(doc)
    return coll, doc


@router.post("/complete")
async def complete_step(body: CompleteIn, user: dict = Depends(require("ops:manage"))):
    kinds = {k: kind for k, _t, _h, kind in STEPS}
    if body.step not in kinds:
        raise HTTPException(status_code=422, detail="Unknown step")
    if kinds[body.step] == "auto":
        raise HTTPException(status_code=422,
                            detail="That step completes itself when the data exists")
    coll, doc = await _progress_doc(user)
    done = list(doc.get("manual_completed") or [])
    if body.step not in done:
        done.append(body.step)
    await coll.update_one({"id": "progress"},
                          {"$set": {"manual_completed": done, "updated_at": now_iso()}})
    return await get_onboarding(user)


@router.post("/dismiss")
async def dismiss(user: dict = Depends(require("ops:manage"))):
    coll, _doc = await _progress_doc(user)
    await coll.update_one({"id": "progress"},
                          {"$set": {"dismissed": True, "updated_at": now_iso()}})
    return {"dismissed": True}
