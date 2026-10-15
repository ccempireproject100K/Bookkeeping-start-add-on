"""Vertical editions, driven by niche packs (backend/niches/*.yaml).

GET /edition returns the workspace's pack: brand face, terminology,
colors, and module list. The checklist is the pack's own items with
`done` computed from live workspace data via each item's `check` key.

Public: GET /niches (pack picker for signup) and GET /niches/resolve
(subdomain -> niche id, for the Cloudflare wildcard setup).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db import db
from lib.auth import require
from lib.niches import get_pack, all_packs, public_pack, niche_from_host

router = APIRouter(prefix="/edition", tags=["edition"])
public_router = APIRouter(prefix="/niches", tags=["niches"])


async def _tenant(user):
    t = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    return t or {}


def _pack_for_tenant(tenant: dict, host: str | None = None) -> dict:
    niche_id = tenant.get("niche_id")
    if not niche_id and host:
        niche_id = niche_from_host(host)
    return get_pack(niche_id)


def _modules(settings: dict) -> list:
    mods = ["dashboard", "pos", "invoices", "expenses", "banking", "ledger",
            "reports", "customers", "commissions", "gift_cards", "tax"]
    if settings.get("inventory_enabled"):
        mods.append("inventory")
    if settings.get("ai_enabled"):
        mods.append("ai")
    return mods


@router.get("")
async def edition_profile(request: Request, user: dict = Depends(require("dashboard:read"))):
    t = await _tenant(user)
    pack = _pack_for_tenant(t, request.headers.get("host"))
    settings = t.get("settings", {})
    return {
        "edition": pack["id"],
        "vertical": pack["id"],
        "niche_id": pack["id"],
        "name": t.get("name") or pack["product_name"],
        "product_name": pack["product_name"],
        "tagline": pack["tagline"],
        "trade": pack["trade"],
        "terms": pack["terms"],
        "colors": pack.get("colors", {}),
        "modules": _modules(settings),
        "branding": t.get("branding", {}),
    }


@router.get("/checklist")
async def setup_checklist(request: Request, user: dict = Depends(require("dashboard:read"))):
    """The pack's own setup checklist, each item's `done` computed from
    live workspace data via its `check` key."""
    tid = user["tenant_id"]
    t = await _tenant(user)
    pack = _pack_for_tenant(t, request.headers.get("host"))

    checks = {
        "team": await db.users.count_documents({"tenant_id": tid}) > 1,
        "services": await db.products.count_documents({"tenant_id": tid}) > 0,
        "bank": await db.bank_accounts.count_documents({"tenant_id": tid}) > 0,
        "tax": await db.contractors.count_documents({"tenant_id": tid}) > 0,
        "sale": await db.sales.count_documents({"tenant_id": tid}) > 0,
    }
    items = []
    for item in pack.get("checklist", []):
        check = item.get("check")
        items.append({
            "id": item["id"], "label": item["label"],
            "hint": item.get("hint", ""), "link": item.get("link", "/"),
            "done": bool(checks.get(check, False)) if check else False,
        })
    done = sum(1 for s in items if s["done"])
    return {"edition": pack["id"], "vertical": pack["id"],
            "shop_label": pack["terms"].get("shop", "shop"),
            "items": items, "done": done, "total": len(items),
            "complete": bool(items) and all(s["done"] for s in items)}


class NicheBody(BaseModel):
    niche_id: str


@router.patch("/niche")
async def switch_niche(body: NicheBody, user: dict = Depends(require("ledger:admin"))):
    """Switch the workspace's face (branding, terminology, checklist).

    Display-only: the chart of accounts seeded at creation is left
    untouched, so the books never get rewired mid-stream.
    """
    pack = get_pack(body.niche_id)
    if not pack.get("id"):
        raise HTTPException(400, "Unknown niche")
    await db.tenants.update_one(
        {"id": user["tenant_id"]}, {"$set": {"niche_id": pack["id"]}})
    return {"ok": True, "niche_id": pack["id"],
            "product_name": pack["product_name"]}


# ---------- Public ----------

@public_router.get("")
async def list_niches():
    """All niche packs (safe subset) — powers the signup niche picker."""
    return [public_pack(p) for p in all_packs()]


@public_router.get("/resolve")
async def resolve_niche(host: str):
    """Which niche a hostname maps to (Cloudflare wildcard subdomains)."""
    nid = niche_from_host(host)
    pack = get_pack(nid)
    return {"niche_id": pack["id"], "product_name": pack["product_name"]}
