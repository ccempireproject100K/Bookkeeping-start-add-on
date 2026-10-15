"""Security alerts feed: what the anomaly detectors (new-device logins,
sentinel watcher) found. Owners/accountants see tenant-wide alerts;
other roles see only their own."""
from fastapi import APIRouter, Depends, HTTPException

from db import db
from lib.auth import get_current_user, require
from lib.base import now_iso

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _scope(user: dict) -> dict:
    q = {"tenant_id": user["tenant_id"]}
    if user.get("role") not in ("owner", "accountant"):
        q["user_id"] = user["id"]
    return q


@router.get("")
async def list_alerts(unacked: bool = False, limit: int = 50,
                      user: dict = Depends(get_current_user)):
    q = _scope(user)
    if unacked:
        q["acknowledged_at"] = None
    rows = await db.security_alerts.find(q, {"_id": 0}).sort(
        "created_at", -1).to_list(min(max(limit, 1), 200))
    unacked_n = await db.security_alerts.count_documents(
        {**_scope(user), "acknowledged_at": None})
    return {"alerts": rows, "unacked": unacked_n}


@router.post("/{alert_id}/ack")
async def ack_alert(alert_id: str, user: dict = Depends(get_current_user)):
    res = await db.security_alerts.update_one(
        {**_scope(user), "id": alert_id, "acknowledged_at": None},
        {"$set": {"acknowledged_at": now_iso(),
                  "acknowledged_by": user["id"]}})
    if not res.modified_count:
        raise HTTPException(404, "Alert not found")
    return {"ok": True}


@router.post("/sentinel/run")
async def run_sentinel_now(user: dict = Depends(require("ledger:admin"))):
    """Manually trigger the security watcher (also runs on a schedule)."""
    from lib.sentinel import run_sentinel
    return await run_sentinel()
