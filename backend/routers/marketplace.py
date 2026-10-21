"""App Marketplace (v1): browse the integration catalog.

Read-only. Every listing's status is resolved at request time from
the live environment gates in lib/marketplace.py — this router holds
no state and caches nothing, so flipping a provider key flips the
listing on the next request.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from lib import marketplace as mp
from lib.auth import require

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


@router.get("")
async def list_marketplace(category: Optional[str] = None,
                           q: Optional[str] = None,
                           user: dict = Depends(require("ledger:read"))):
    """Catalog browse: {categories: [{name, count}], listings: [...]}.

    ?category= and ?q= filters apply server-side to the listings;
    category counts always describe the full catalog.
    """
    listings = mp.list_listings()
    categories = mp.list_categories(listings)
    if category:
        listings = [i for i in listings
                    if i["category"].lower() == category.strip().lower()]
    if q:
        needle = q.strip().lower()
        if needle:
            listings = [i for i in listings if needle in " ".join([
                i.get("slug", ""), i.get("name", ""), i.get("provider", ""),
                i.get("category", ""), i.get("tagline", ""),
                i.get("description", ""),
            ]).lower()]
    return {"categories": categories, "listings": listings}


@router.get("/{slug}")
async def get_marketplace_listing(slug: str,
                                  user: dict = Depends(require("ledger:read"))):
    listing = mp.get_listing(slug)
    if not listing:
        raise HTTPException(status_code=404, detail="Marketplace listing not found")
    return listing
