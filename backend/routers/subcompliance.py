"""Subcontractor compliance tracker: W-9s collected, insurance certs and licenses
tracked with expiry alerts — so January 1099 season has no surprises.

Compliance fields live on the vendor doc. A sub is compliant when the W-9 is
on file and insurance hasn't expired.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, now_iso, today_str

router = APIRouter(prefix="/subcompliance", tags=["subcompliance"])


def _subs(user):
    return Scoped(db.vendors, user["tenant_id"])


def _status(v: dict, today: str) -> dict:
    comp = v.get("compliance") or {}
    issues = []
    if not comp.get("w9_received"):
        issues.append("missing_w9")
    ins = (comp.get("insurance_expiry") or "")[:10]
    if ins:
        try:
            delta = (date.fromisoformat(ins) - date.fromisoformat(today)).days
            if delta < 0:
                issues.append("insurance_expired")
            elif delta <= 30:
                issues.append("insurance_expiring")
        except ValueError:
            pass
    lic = (comp.get("license_expiry") or "")[:10]
    if lic:
        try:
            if (date.fromisoformat(lic) - date.fromisoformat(today)).days < 0:
                issues.append("license_expired")
        except ValueError:
            pass
    return {"compliant": not issues, "issues": issues,
            "insurance_expiry": ins, "w9_received": bool(comp.get("w9_received"))}


@router.get("/subs")
async def list_subs(user: dict = Depends(require("ledger:read"))):
    today = today_str()[:10]
    docs = await _subs(user).find_list({"is_1099": True}, sort=[("name", 1)], limit=500)
    for d in docs:
        d["compliance_status"] = _status(d, today)
    return docs


class ComplianceBody(BaseModel):
    w9_received: bool = False
    w9_date: Optional[str] = None
    insurance_expiry: Optional[str] = None
    license_expiry: Optional[str] = None
    notes: str = ""


@router.put("/subs/{vendor_id}")
async def set_compliance(vendor_id: str, body: ComplianceBody,
                         user: dict = Depends(require("ops:manage"))):
    v = await _subs(user).find_one({"id": vendor_id})
    if not v:
        raise HTTPException(404, "Subcontractor not found")
    comp = body.model_dump()
    await _subs(user).update_one({"id": vendor_id}, {"$set": {"compliance": comp}})
    await audit(user, "subcompliance.update", "vendor", vendor_id,
                {"w9": comp["w9_received"], "insurance_expiry": comp["insurance_expiry"]})
    v = await _subs(user).find_one({"id": vendor_id})
    v["compliance_status"] = _status(v, today_str()[:10])
    return v


@router.get("/dashboard")
async def dashboard(user: dict = Depends(require("ledger:read"))):
    today = today_str()[:10]
    docs = await _subs(user).find_list({"is_1099": True}, limit=1000)
    total = len(docs)
    counts = {"compliant": 0, "missing_w9": 0, "insurance_expired": 0,
              "insurance_expiring": 0, "license_expired": 0}
    upcoming = []
    for d in docs:
        st = _status(d, today)
        if st["compliant"]:
            counts["compliant"] += 1
        for i in st["issues"]:
            counts[i] = counts.get(i, 0) + 1
        if not st["compliant"]:
            upcoming.append({"vendor_id": d["id"], "name": d["name"],
                             "issues": st["issues"],
                             "insurance_expiry": st["insurance_expiry"]})
    upcoming.sort(key=lambda u: (len(u["issues"]), u["name"]))
    readiness = round(counts["compliant"] / total * 100) if total else 100
    return {"total_subs": total, "counts": counts,
            "readiness_pct": readiness, "needs_attention": upcoming[:50]}
