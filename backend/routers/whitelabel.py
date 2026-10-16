"""White-label for accountants: firms bring their clients, our engine underneath.

- Owner sets firm branding (name, logo, accent color, support contact) on the tenant;
  the /edition profile carries it and the UI renders the firm's face.
- Accountants get a firm dashboard: every client workspace they can access,
  with a health snapshot (open AR, overdue invoices, unpaid bills, last activity).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require
from lib.plans import require_feature
from lib.base import Scoped, cents_to_dollars, today_str

router = APIRouter(prefix="/whitelabel", tags=["whitelabel"], dependencies=[Depends(require_feature("white_label"))])


class BrandingBody(BaseModel):
    firm_name: Optional[str] = None
    logo_url: Optional[str] = None
    accent_color: Optional[str] = None
    support_email: Optional[str] = None
    tagline: Optional[str] = None


@router.get("/branding")
async def get_branding(user: dict = Depends(require("dashboard:read"))):
    t = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    return t.get("branding", {})


@router.patch("/branding")
async def set_branding(body: BrandingBody, user: dict = Depends(require("ledger:admin"))):
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if "accent_color" in upd:
        c = upd["accent_color"].strip()
        if not (c.startswith("#") and len(c) in (4, 7)):
            raise HTTPException(422, "accent_color must be a hex color like #f59e0b")
    await db.tenants.update_one({"id": user["tenant_id"]},
                                {"$set": {"branding": upd}})
    await audit(user, "whitelabel.branding", "tenant", user["tenant_id"], upd)
    return upd


@router.get("/clients")
async def firm_clients(user: dict = Depends(require("dashboard:read"))):
    """Client workspaces this user can access (accountant firm view)."""
    if user.get("role") not in ("owner", "accountant"):
        raise HTTPException(403, "Accountants and owners only")
    # workspaces where this user is a member (via users collection membership)
    memberships = await db.users.find(
        {"email": user.get("email")}, {"_id": 0, "tenant_id": 1}).to_list(100)
    tids = list({m["tenant_id"] for m in memberships if m.get("tenant_id")})
    out = []
    for tid in tids:
        t = await db.tenants.find_one({"id": tid}, {"_id": 0}) or {}
        invs = await Scoped(db.invoices, tid).find_list({"status": "issued"}, limit=2000)
        ar = sum(i["total_cents"] - i.get("paid_cents", 0) for i in invs)
        today = today_str()[:10]
        overdue = sum(1 for i in invs
                      if (i.get("due_date") or "")[:10] and (i.get("due_date") or "")[:10] < today
                      and i["total_cents"] - i.get("paid_cents", 0) > 0)
        bills = await Scoped(db.bills, tid).find_list(
            {"status": {"$in": ["open", "partial"]}}, limit=500)
        ap = sum(b.get("amount_cents", 0) - b.get("paid_cents", 0) for b in bills)
        last = await Scoped(db.audit_log, tid).find_list(
            sort=[("created_at", -1)], limit=1)
        out.append({"tenant_id": tid, "name": t.get("name", tid),
                    "niche_id": t.get("niche_id"),
                    "ar_cents": ar, "overdue_invoices": overdue, "ap_cents": ap,
                    "last_activity": (last[0].get("created_at") if last else None)})
    out.sort(key=lambda c: -c["ar_cents"])
    return out
