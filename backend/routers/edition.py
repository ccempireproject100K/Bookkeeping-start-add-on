"""Vertical editions: a light packaging pass over the existing product.

This app already IS the barbershop edition ("The Barber's Ledger"), so
GET /edition returns the workspace edition profile — name, vertical
"barbershop", and the enabled module list derived from tenant settings —
plus an edition-aware setup checklist whose items are computed from actual
workspace data. Nothing is gated by edition; it's packaging, not a paywall.
"""
from fastapi import APIRouter, Depends

from db import db
from lib.auth import require

router = APIRouter(prefix="/edition", tags=["edition"])


async def _tenant(user):
    t = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    return t or {}


def _modules(settings: dict) -> list:
    mods = ["dashboard", "pos", "invoices", "expenses", "banking", "ledger",
            "reports", "customers", "commissions", "gift_cards", "tax"]
    if settings.get("inventory_enabled"):
        mods.append("inventory")
    if settings.get("ai_enabled"):
        mods.append("ai")
    return mods


@router.get("")
async def edition_profile(user: dict = Depends(require("dashboard:read"))):
    t = await _tenant(user)
    settings = t.get("settings", {})
    return {
        "edition": "barbershop",
        "vertical": "barbershop",
        "name": t.get("name", "The Barber's Ledger"),
        "modules": _modules(settings),
    }


@router.get("/checklist")
async def setup_checklist(user: dict = Depends(require("dashboard:read"))):
    """Setup checklist for the shop, each item computed from actual data."""
    tid = user["tenant_id"]
    team = await db.users.count_documents({"tenant_id": tid})
    services = await db.products.count_documents({"tenant_id": tid})
    bank = await db.bank_accounts.count_documents({"tenant_id": tid})
    tax_setup = await db.contractors.count_documents({"tenant_id": tid})
    sales = await db.sales.count_documents({"tenant_id": tid})

    items = [
        {"id": "team", "label": "Add your team",
         "hint": "Barbers and staff show up once they have a workspace login; "
                 "commissions split them automatically",
         "done": team > 1, "link": "/commissions"},
        {"id": "services", "label": "Add services & products",
         "hint": "Cuts, color, and your product shelf",
         "done": services > 0, "link": "/inventory"},
        {"id": "bank", "label": "Add a bank account",
         "hint": "Reconcile payouts against deposits in Banking",
         "done": bank > 0, "link": "/banking"},
        {"id": "tax", "label": "Set up contractor tracking",
         "hint": "W-9s and 1099-NECs for your booth renters or freelancers",
         "done": tax_setup > 0, "link": "/tax"},
        {"id": "sale", "label": "Run your first sale",
         "hint": "QuickSell at the chair",
         "done": sales > 0, "link": "/quick-sell"},
    ]
    done = sum(1 for s in items if s["done"])
    return {"edition": "barbershop", "vertical": "barbershop",
            "items": items, "done": done, "total": len(items),
            "complete": all(s["done"] for s in items)}
