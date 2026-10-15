"""Foreign-exchange endpoints (multi-currency Phase 3).

POST /api/fx/revalue — owner-triggered month-end revaluation of open
foreign-currency invoices, bills and bank balances. Idempotent per date:
re-running for the same as-of date is a no-op.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from db import db
from lib.auth import require
from lib.base import today_str
from lib import fx_posting as FXP
from lib import currencies as C

router = APIRouter(prefix="/fx", tags=["fx"])

# Separate router (no /fx prefix) for the public currency list.
currencies_router = APIRouter(prefix="/currencies", tags=["currencies"])


@currencies_router.get("")
async def list_currencies():
    """The supported currencies with symbols and decimal metadata
    (multi-currency Phase 4 — feeds the UI pickers)."""
    return [{"code": code, **meta} for code, meta in C.CURRENCIES.items()]


class RevalueBody(BaseModel):
    as_of_date: Optional[str] = None  # YYYY-MM-DD; defaults to today


@router.post("/revalue")
async def revalue(body: RevalueBody, user: dict = Depends(require("owner"))):
    as_of = (body.as_of_date or today_str())[:10]
    if len(as_of) != 10 or as_of[4] != "-" or as_of[7] != "-":
        raise HTTPException(status_code=422, detail="as_of_date must be YYYY-MM-DD")
    try:
        return await FXP.revalue_open_balances(db, user["tenant_id"], user, as_of)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Revaluation failed: {exc}")
