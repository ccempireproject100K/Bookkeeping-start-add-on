"""FX rates API: inspect the stored rate table, refresh it from Frankfurter
(ECB feed fallback), or enter a manual rate. Rates are stored per tenant;
documents stamp the rate they used at issue time and never re-read this
table afterwards."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.auth import require, get_current_user
from lib import fx as FX

router = APIRouter(prefix="/fx", tags=["fx"])


@router.get("/rates")
async def list_rates(base: Optional[str] = None, quote: Optional[str] = None,
                     user: dict = Depends(get_current_user)):
    q = {"tenant_id": user["tenant_id"]}
    if base:
        q["base"] = base.upper()
    if quote:
        q["quote"] = quote.upper()
    rows = await db.fx_rates.find(q, {"_id": 0}).sort(
        [("date", -1), ("quote", 1)]).to_list(length=500)
    home = await FX.home_currency(db, user["tenant_id"])
    return {"home_currency": home, "rates": rows,
            "common_currencies": FX.COMMON_CURRENCIES}


class RefreshBody(BaseModel):
    currencies: Optional[List[str]] = None


@router.post("/refresh")
async def refresh(body: RefreshBody, user: dict = Depends(require("ops:manage"))):
    home = await FX.home_currency(db, user["tenant_id"])
    return await FX.refresh_rates(db, user["tenant_id"], home,
                                  currencies=body.currencies)


class ManualRateBody(BaseModel):
    quote: str
    rate: float
    date: Optional[str] = None
    base: Optional[str] = None


@router.post("/rates/manual")
async def manual_rate(body: ManualRateBody, user: dict = Depends(require("ops:manage"))):
    from lib.base import today_str
    from decimal import Decimal
    if Decimal(str(body.rate)) <= 0:
        raise HTTPException(status_code=422, detail="FX rate must be positive")
    home = await FX.home_currency(db, user["tenant_id"])
    base = (body.base or home).upper()
    doc = await FX.store_rate(db, user["tenant_id"], base, body.quote.upper(),
                              str(body.rate), body.date or today_str(),
                              FX.SOURCE_MANUAL)
    doc.pop("tenant_id", None)
    return doc
