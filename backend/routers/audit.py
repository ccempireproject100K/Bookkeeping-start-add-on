"""Audit log viewer: read-only access to the tenant's audit trail.

Every mutating endpoint writes to `audit_log` (actor, action, entity,
timestamp). This router exposes it for review — owner and accountant only.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from db import db
from lib.auth import require

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
async def list_audit(
    action: str = Query("", description="Substring match on the action, e.g. 'tax'"),
    entity: str = Query("", description="Exact entity type, e.g. 'contractor'"),
    actor: str = Query("", description="Substring match on actor name"),
    days: int = Query(30, ge=1, le=365, description="Look back this many days"),
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(require("audit:read")),
):
    q: dict = {"tenant_id": user["tenant_id"]}
    if action:
        q["action"] = {"$regex": action, "$options": "i"}
    if entity:
        q["entity"] = entity
    if actor:
        q["actor_name"] = {"$regex": actor, "$options": "i"}
    # created_at is an ISO-8601 string; lexicographic compare works.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q["created_at"] = {"$gte": cutoff}
    cur = db.audit_log.find(q, {"_id": 0}).sort("created_at", -1).limit(limit)
    rows = await cur.to_list(length=limit)
    return {"rows": rows, "count": len(rows), "limit": limit}
