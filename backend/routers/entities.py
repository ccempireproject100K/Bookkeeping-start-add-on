"""Multi-entity: one login, many businesses, one consolidated view.

- GET /entities — every entity this login owns, with key stats
- GET /entities/consolidated — combined P&L + cash across entities,
  with intercompany accounts flagged for elimination review

Consolidation is read-only aggregation; each entity's books stay separate.
Only entities where the user is owner are included.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from db import db
from lib import ledger as L
from lib.auth import get_current_user
from lib.plans import require_feature
from lib.base import Scoped, cents_to_dollars, today_str
from lib.memberships import memberships

router = APIRouter(prefix="/entities", tags=["entities"], dependencies=[Depends(require_feature("multi_entity"))])


def _owned(user) -> list:
    return [m["tenant_id"] for m in memberships(user) if m.get("role") == "owner"]


async def _entity_stats(tid: str) -> dict:
    t = await db.tenants.find_one({"id": tid}, {"_id": 0}) or {}
    today = today_str()[:10]
    ytd = await L.pnl(db, tid, f"{today[:4]}-01-01", today)
    bals = await L._balances(db, tid, as_of=today)

    def bal(code):
        v = bals.get(code, {"debit": 0, "credit": 0})
        return L._net_balance(code, v["debit"], v["credit"])
    cash = bal("1010") + bal("1020") + bal("1021") + bal("1022") + bal("1023") + bal("1050")
    return {"tenant_id": tid, "name": t.get("name", tid), "niche_id": t.get("niche_id"),
            "cash_cents": cash, "revenue_cents": ytd["revenue_total"],
            "expense_cents": ytd["expense_total"], "net_cents": ytd["net_income"]}


@router.get("")
async def list_entities(user: dict = Depends(get_current_user)):
    return [await _entity_stats(tid) for tid in _owned(user)]


@router.get("/consolidated")
async def consolidated(date_from: Optional[str] = Query(None),
                       date_to: Optional[str] = Query(None),
                       user: dict = Depends(get_current_user)):
    tids = _owned(user)
    today = today_str()[:10]
    date_from = date_from or f"{today[:4]}-01-01"
    date_to = date_to or today
    by_code = {}
    entities = []
    total_cash = 0
    intercompany = {}
    for tid in tids:
        st = await _entity_stats(tid)
        entities.append(st)
        total_cash += st["cash_cents"]
        r = await L.pnl(db, tid, date_from, date_to)
        for line in r.get("revenue", []) + r.get("expense", []):
            code = line.get("code", "")
            by_code.setdefault(code, {"code": code, "name": line.get("name", ""),
                                      "amount_cents": 0})
            by_code[code]["amount_cents"] += line.get("amount", 0)
            # flag intercompany-style accounts for elimination review
            if code.startswith("14") or code.startswith("21"):
                intercompany.setdefault(code, {"code": code, "name": line.get("name", ""),
                                               "amount_cents": 0})
                intercompany[code]["amount_cents"] += line.get("amount", 0)
    lines = sorted(by_code.values(), key=lambda l: l["code"])
    revenue = sum(l["amount_cents"] for l in lines if l["code"].startswith("4"))
    expenses = sum(l["amount_cents"] for l in lines if l["code"].startswith(("5", "6", "7", "8", "9")))
    return {"entities": entities, "entity_count": len(entities),
            "date_from": date_from, "date_to": date_to,
            "lines": lines,
            "total_revenue_cents": revenue, "total_expense_cents": expenses,
            "net_cents": revenue - expenses,
            "combined_cash_cents": total_cash,
            "intercompany_review": sorted(intercompany.values(), key=lambda l: l["code"]),
            "note": "Intercompany balances are flagged, not auto-eliminated — review before reporting."}
