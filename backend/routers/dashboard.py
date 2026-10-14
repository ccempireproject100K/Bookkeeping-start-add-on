"""Dashboard: headline financial tiles derived from the general ledger."""
from fastapi import APIRouter, Depends
from datetime import datetime, timezone

from db import db
from lib.base import Scoped, today_str, cents_to_dollars
from lib.auth import get_current_user
from lib import ledger as L

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def dashboard(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
    as_of = today_str()
    month_start = as_of[:7] + "-01"

    bals = await L._balances(db, tenant_id, as_of=as_of)

    def bal(code):
        v = bals.get(code, {"debit": 0, "credit": 0})
        return L._net_balance(code, v["debit"], v["credit"])

    cash = bal("1010") + bal("1020") + bal("1050")
    ar = bal("1200")
    ap = bal("2010")
    inventory = bal("1300") + bal("1310")

    pnl = await L.pnl(db, tenant_id, month_start, as_of)

    recent = await Scoped(db.journal_entries, tenant_id).find_list(
        {"status": "posted"}, sort=[("entry_date", -1), ("created_at", -1)], limit=8)

    return {
        "as_of": as_of,
        "tiles": {
            "cash": cents_to_dollars(cash),
            "revenue": cents_to_dollars(pnl["revenue_total"]),
            "expenses": cents_to_dollars(pnl["expense_total"]),
            "net_income": cents_to_dollars(pnl["net_income"]),
            "ar": cents_to_dollars(ar),
            "ap": cents_to_dollars(ap),
            "inventory": cents_to_dollars(inventory),
        },
        "recent_entries": recent,
        "month": as_of[:7],
    }
