"""DataClean console shell backend (Phase 3) — /dataclean/*.

Session-authenticated management surface behind the console's API
pages (owner/admin only, require("ledger:admin") — the same gate as
the Developers page):

* API keys — issue / list (masked) / revoke for DataClean keys
  (lib/dataclean_api.py). A created key's full value is returned ONCE
  in the create response; only its hash + stub are stored, and list
  views are metadata-only, forever.
* Usage — aggregates over the durable dataclean_api_usage metering
  rows (the same rows the external surface writes). Every number on
  the console's Usage page is counted from those rows; there is no
  other source, and nothing here estimates or invents.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import db
from lib import dataclean_api as dca
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped

router = APIRouter(prefix="/dataclean", tags=["dataclean-admin"])


class CreateKeyBody(BaseModel):
    label: str = Field(default="DataClean API key", max_length=120)
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=1)
    rate_limit_per_day: Optional[int] = Field(default=None, ge=1)
    monthly_row_quota: Optional[int] = Field(default=None, ge=0)


@router.post("/keys")
async def create_key(body: CreateKeyBody,
                     user: dict = Depends(require("ledger:admin"))):
    """Mint a DataClean API key for this workspace. The full key is
    returned ONCE in this response; only its hash is stored."""
    doc, full = dca.new_key_doc(
        tenant_id=user["tenant_id"], created_by=user.get("email", ""),
        label=body.label,
        rate_limit_per_minute=body.rate_limit_per_minute,
        rate_limit_per_day=body.rate_limit_per_day,
        monthly_row_quota=body.monthly_row_quota)
    await db.dataclean_api_keys.insert_one(doc)
    await audit(user, "dataclean.api_key_created", "dataclean_api_key",
                doc["id"], {"label": doc["label"], "key_stub": doc["key_stub"]})
    out = dca.public_key_view(doc)
    out["key"] = full
    return out


@router.get("/keys")
async def list_keys(user: dict = Depends(require("ledger:admin"))):
    rows = await Scoped(db.dataclean_api_keys, user["tenant_id"]).find_list(
        sort=[("created_at", -1)], limit=200)
    return [dca.public_key_view(r) for r in rows]


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: str,
                     user: dict = Depends(require("ledger:admin"))):
    keys = Scoped(db.dataclean_api_keys, user["tenant_id"])
    doc = await keys.find_one({"id": key_id})
    if not doc:
        raise HTTPException(status_code=404, detail="API key not found")
    if doc.get("revoked_at"):
        return {"ok": True, "id": key_id, "revoked_at": doc["revoked_at"]}
    from lib.base import now_iso
    ts = now_iso()
    await keys.update_one({"id": key_id}, {"$set": {"revoked_at": ts}})
    await audit(user, "dataclean.api_key_revoked", "dataclean_api_key",
                key_id, {"key_stub": doc.get("key_stub"),
                         "label": doc.get("label")})
    return {"ok": True, "id": key_id, "revoked_at": ts}


@router.get("/usage")
async def usage(days: int = Query(30, ge=1, le=92),
                user: dict = Depends(require("ledger:admin"))):
    """Usage aggregates for this workspace, counted from the metering
    rows. by_day covers the trailing `days` days (zero-filled, so the
    chart's gaps are honest gaps)."""
    rows = await Scoped(db.dataclean_api_usage, user["tenant_id"]).find_list(
        sort=[("created_at", -1)], limit=100000)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days - 1)).date().isoformat()
    by_day_map = {}
    for i in range(days):
        d = (now - timedelta(days=i)).date().isoformat()
        by_day_map[d] = {"date": d, "calls": 0, "rows": 0}
    totals = {"calls": 0, "rows": 0}
    by_status, by_endpoint, by_key = {}, {}, {}
    for r in rows:
        day = (r.get("created_at") or "")[:10]
        totals["calls"] += 1
        totals["rows"] += r.get("rows_processed", 0)
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_endpoint[r["endpoint"]] = by_endpoint.get(r["endpoint"], 0) + 1
        k = by_key.setdefault(r["key_id"], {"key_id": r["key_id"],
                                            "calls": 0, "rows": 0})
        k["calls"] += 1
        k["rows"] += r.get("rows_processed", 0)
        if day >= cutoff and day in by_day_map:
            by_day_map[day]["calls"] += 1
            by_day_map[day]["rows"] += r.get("rows_processed", 0)
    keys = await Scoped(db.dataclean_api_keys, user["tenant_id"]).find_list(
        limit=200)
    labels = {k["id"]: (k.get("label"), k.get("key_stub")) for k in keys}
    for kid, agg in by_key.items():
        label, stub = labels.get(kid, (None, None))
        agg["label"] = label
        agg["key_stub"] = stub
    return {
        "days": days,
        "totals": totals,
        "by_day": [by_day_map[d] for d in sorted(by_day_map)],
        "by_status": by_status,
        "by_endpoint": by_endpoint,
        "by_key": sorted(by_key.values(), key=lambda a: -a["calls"]),
        "tracking_since": (rows[-1]["created_at"] if rows else None),
    }
