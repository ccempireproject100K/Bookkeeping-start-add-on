"""Categorization review queue — the doubt-flag lane (owner-approved
2026-11-14, Top-20 gap #7).

When auto-categorization is genuinely unsure about a bank transaction, the
transaction is parked here with the system's best guess and a plain-words
reason instead of being left with a silent suggestion. The user confirms the
guess or fixes it; the fix posts to the ledger through the normal banking
categorize path.

Learning is an option, always: after a correction the user is asked whether
to remember the pattern. Only a yes stores anything (database-backed, per
tenant, in cat_learned_patterns). The tenant setting
settings.cat_learning_enabled=False turns learning off absolutely — no
pattern is stored and none is applied.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import require
from lib.coa import NAME_OF, ACCOUNT_TYPE_OF
from lib import catconfidence
from routers import banking

router = APIRouter(prefix="/cat-review", tags=["cat-review"])


def _queue(user):
    return banking.review_queue(user)


def _patterns(user):
    return banking.learned_patterns(user)


# ---------------- the one posting seam ----------------
async def _post_txn(user, txn_id, category_code):
    """Post a resolved transaction through the normal categorize path —
    the same balanced journal entry a manual categorize produces."""
    return await banking.categorize(
        txn_id, banking.CategorizeBody(category_code=category_code), user)


# ---------------- queue ----------------
@router.get("/queue")
async def list_queue(user: dict = Depends(require("ledger:read"))):
    return await _queue(user).find_list({"status": "pending"},
                                        sort=[("created_at", -1)], limit=500)


@router.get("/stats")
async def stats(user: dict = Depends(require("ledger:read"))):
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    q = _queue(user)
    return {
        "pending": await q.count({"status": "pending"}),
        "flagged_this_week": await q.count({"created_at": {"$gte": week_ago}}),
        "resolved_total": await q.count({"status": "resolved"}),
        "learned_count": await _patterns(user).count({}),
    }


class ResolveBody(BaseModel):
    category_code: str
    remember: bool = False


@router.post("/queue/{item_id}/resolve")
async def resolve(item_id: str, body: ResolveBody,
                  user: dict = Depends(require("ops:manage"))):
    item = await _queue(user).find_one({"id": item_id})
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")
    if item["status"] != "pending":
        raise HTTPException(status_code=409, detail="Already resolved")
    if body.category_code not in ACCOUNT_TYPE_OF:
        raise HTTPException(status_code=422, detail="Unknown category account")
    txn = await banking.txns(user).find_one({"id": item["txn_id"]})
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    entry_number = txn.get("entry_number")
    if txn["status"] not in ("cleared", "matched"):
        posted = await _post_txn(user, item["txn_id"], body.category_code)
        entry_number = posted.get("entry_number")

    learned = False
    if body.remember and await banking.cat_learning_enabled(user):
        learned = await _remember(user, txn, body.category_code)

    await _queue(user).update_one({"id": item_id}, {"$set": {
        "status": "resolved", "resolution_code": body.category_code,
        "learned": learned, "resolved_at": now_iso(),
        "resolved_by": user.get("email"),
    }})
    await banking.txns(user).update_one({"id": item["txn_id"]}, {"$set": {
        "needs_review": False,
    }})
    return {"status": "resolved", "entry_number": entry_number, "learned": learned}


async def _remember(user, txn, category_code):
    """Store (or refresh) one taught pattern for this tenant. Database-
    backed: it survives restarts and sessions — it does not forget."""
    key = catconfidence.pattern_key(txn["description"])
    if not key:
        return False
    existing = await _patterns(user).find_one({"pattern": key})
    if existing:
        await _patterns(user).update_one({"id": existing["id"]}, {"$set": {
            "category_code": category_code,
            "category_name": NAME_OF.get(category_code),
            "sample_description": txn["description"],
            "updated_at": now_iso(),
        }, "$inc": {"times_corrected": 1}})
    else:
        doc = {
            "id": new_id(), "pattern": key,
            "category_code": category_code,
            "category_name": NAME_OF.get(category_code),
            "sample_description": txn["description"],
            "times_corrected": 1,
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        await _patterns(user).insert_one(doc)
    return True


# ---------------- learning setting ----------------
@router.get("/settings")
async def get_settings(user: dict = Depends(require("ledger:read"))):
    return {"learning_enabled": await banking.cat_learning_enabled(user)}


class SettingsBody(BaseModel):
    learning_enabled: bool


@router.patch("/settings")
async def patch_settings(body: SettingsBody,
                         user: dict = Depends(require("settings:write"))):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    settings = dict((tenant or {}).get("settings") or {})
    settings["cat_learning_enabled"] = body.learning_enabled
    await db.tenants.update_one(
        {"id": user["tenant_id"]},
        {"$set": {"settings": settings}},
        upsert=True)
    return {"learning_enabled": body.learning_enabled}


# ---------------- learned patterns (visible, removable) ----------------
@router.get("/patterns")
async def list_patterns(user: dict = Depends(require("ledger:read"))):
    return await _patterns(user).find_list(sort=[("updated_at", -1)], limit=500)


@router.delete("/patterns/{pattern_id}")
async def delete_pattern(pattern_id: str,
                         user: dict = Depends(require("ops:manage"))):
    await _patterns(user).c.delete_one(
        {"tenant_id": user["tenant_id"], "id": pattern_id})
    return {"deleted": True}
