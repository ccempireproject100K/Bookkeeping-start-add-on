"""Sales-tax jurisdiction tracking.

A shop can owe sales tax in many places (state + county + city + special
districts, each with its own rate and filing schedule). This module:

- stores the jurisdictions the business collects in (name, state, rate),
- tags invoices with a jurisdiction (optional),
- reports, per period and jurisdiction, taxable sales vs tax collected —
  i.e. "what you owe and where to file it."

It does NOT file or remit anything; that stays with the owner/accountant.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import db
from lib.auth import require
from lib.base import new_id, now_iso

router = APIRouter(prefix="/sales-tax", tags=["sales-tax"])


def _coll(user):
    return db.tax_jurisdictions


class JurisdictionIn(BaseModel):
    name: str = Field(..., max_length=80)      # e.g. "Travis County, TX"
    state: str = Field(..., max_length=2)     # 2-letter code
    rate_pct: float = Field(..., ge=0, le=30)  # e.g. 8.25
    notes: Optional[str] = Field(default="", max_length=200)


class JurisdictionPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    rate_pct: Optional[float] = Field(default=None, ge=0, le=30)
    notes: Optional[str] = Field(default=None, max_length=200)
    active: Optional[bool] = None


@router.get("/jurisdictions")
async def list_jurisdictions(user: dict = Depends(require("tax:read"))):
    rows = await _coll(user).find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0}
    ).sort("name", 1).to_list(200)
    return rows


@router.post("/jurisdictions")
async def create_jurisdiction(body: JurisdictionIn,
                              user: dict = Depends(require("tax:write"))):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name is required")
    dupe = await _coll(user).find_one({
        "tenant_id": user["tenant_id"], "name": name, "active": True})
    if dupe:
        raise HTTPException(409, "An active jurisdiction with that name exists")
    doc = {
        "id": new_id(), "tenant_id": user["tenant_id"], "name": name,
        "state": body.state.strip().upper(), "rate_pct": round(body.rate_pct, 4),
        "notes": (body.notes or "").strip(), "active": True,
        "created_at": now_iso(),
    }
    await _coll(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.patch("/jurisdictions/{jid}")
async def update_jurisdiction(jid: str, body: JurisdictionPatch,
                              user: dict = Depends(require("tax:write"))):
    patch = {}
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "Name can't be blank")
        patch["name"] = body.name.strip()
    if body.rate_pct is not None:
        patch["rate_pct"] = round(body.rate_pct, 4)
    if body.notes is not None:
        patch["notes"] = body.notes.strip()
    if body.active is not None:
        patch["active"] = body.active
    if not patch:
        raise HTTPException(400, "Nothing to update")
    r = await _coll(user).update_one(
        {"id": jid, "tenant_id": user["tenant_id"]}, {"$set": patch})
    if not r.matched_count:
        raise HTTPException(404, "Jurisdiction not found")
    doc = await _coll(user).find_one({"id": jid}, {"_id": 0})
    return doc


@router.get("/liability")
async def liability(year: int = Query(default_factory=lambda: datetime.now(timezone.utc).year),
                    month: Optional[int] = Query(default=None, ge=1, le=12),
                    user: dict = Depends(require("tax:read"))):
    """Per-jurisdiction sales-tax liability for a period.

    Groups invoices with tax_cents > 0 by their tax_jurisdiction_id.
    Invoices with tax but no jurisdiction land in the "unassigned" bucket —
    a nudge to tag them.
    """
    start = f"{year:04d}-{month:02d}-01" if month else f"{year:04d}-01-01"
    if month:
        end_m, end_y = (month % 12) + 1, year + (1 if month == 12 else 0)
        end = f"{end_y:04d}-{end_m:02d}-01"
    else:
        end = f"{year + 1:04d}-01-01"
    invs = await db.invoices.find({
        "tenant_id": user["tenant_id"],
        "created_at": {"$gte": start, "$lt": end},
        "tax_cents": {"$gt": 0},
    }, {"_id": 0}).to_list(5000)

    jur_rows = await _coll(user).find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0}).to_list(500)
    jur = {j["id"]: j for j in jur_rows}

    buckets: dict = {}
    for inv in invs:
        jid = inv.get("tax_jurisdiction_id") or "unassigned"
        b = buckets.setdefault(jid, {
            "jurisdiction_id": jid,
            "name": (jur.get(jid) or {}).get("name", "Unassigned"),
            "state": (jur.get(jid) or {}).get("state", ""),
            "rate_pct": (jur.get(jid) or {}).get("rate_pct"),
            "invoice_count": 0, "taxable_sales_cents": 0, "tax_collected_cents": 0,
        })
        b["invoice_count"] += 1
        b["taxable_sales_cents"] += inv.get("subtotal_cents", 0)
        b["tax_collected_cents"] += inv.get("tax_cents", 0)

    rows = sorted(buckets.values(), key=lambda r: r["name"])
    totals = {
        "invoice_count": sum(r["invoice_count"] for r in rows),
        "taxable_sales_cents": sum(r["taxable_sales_cents"] for r in rows),
        "tax_collected_cents": sum(r["tax_collected_cents"] for r in rows),
    }
    return {"year": year, "month": month, "jurisdictions": rows, "totals": totals}
