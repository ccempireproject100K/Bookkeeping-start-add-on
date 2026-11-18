"""Multi-entity: one login, many businesses, one consolidated view.

- GET /entities — every entity this login owns, with key stats
- GET /entities/consolidated — combined P&L + cash across entities.
  Intercompany deals are flagged for review; when the owner turns
  eliminations ON (tenant setting, default OFF), matched intercompany
  amounts also net out of the group total — gross -> eliminations ->
  consolidated, with every eliminated amount listed.
- GET/PUT /entities/eliminations-setting — the tenant-level ON/OFF.

Consolidation is read-only aggregation; each entity's books stay separate
and are never altered by anything here. Only entities where the user is
owner are included.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from db import db
from lib import ledger as L
from lib.auth import get_current_user
from lib.plans import require_feature
from lib.base import Scoped, cents_to_dollars, today_str
from lib.eliminations import compute_eliminations
from lib.memberships import memberships

router = APIRouter(prefix="/entities", tags=["entities"], dependencies=[Depends(require_feature("multi_entity"))])

CASH_CODES = ("1010", "1020", "1021", "1022", "1023", "1050")


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


# ---------------- eliminations setting (tenant-level, default OFF) ----

async def _settings_map(tids) -> dict:
    out = {}
    for tid in tids:
        t = await db.tenants.find_one({"id": tid}, {"_id": 0}) or {}
        out[tid] = bool((t.get("settings") or {}).get("eliminations_enabled", False))
    return out


def _effective_enabled(user, tids, settings_map) -> bool:
    """The group view follows the active workspace's setting (the toggle
    below writes every owned tenant at once, so they agree)."""
    active = user.get("tenant_id")
    if active in settings_map:
        return settings_map[active]
    return settings_map[tids[0]] if tids else False


@router.get("/eliminations-setting")
async def get_eliminations_setting(user: dict = Depends(get_current_user)):
    tids = _owned(user)
    settings_map = await _settings_map(tids)
    return {"enabled": _effective_enabled(user, tids, settings_map),
            "by_entity": settings_map,
            "note": "Off by default: the consolidated view flags intercompany "
                    "deals for review and changes nothing. On: matched "
                    "intercompany amounts net out of the group total only — "
                    "each business's own books never change."}


class EliminationsSettingBody(BaseModel):
    enabled: bool


@router.put("/eliminations-setting")
async def put_eliminations_setting(body: EliminationsSettingBody,
                                   user: dict = Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403,
                            detail="Only the owner can change eliminations")
    tids = _owned(user)
    for tid in tids:
        await db.tenants.update_one(
            {"id": tid},
            {"$set": {"settings.eliminations_enabled": body.enabled}})
    await L.audit(db, user.get("tenant_id"), user,
                  "entities.eliminations_setting",
                  {"enabled": body.enabled, "entities": tids})
    return {"enabled": body.enabled, "by_entity": {tid: body.enabled for tid in tids}}


# ---------------- consolidated view -----------------------------------

async def _gross_consolidated(tids, date_from, date_to) -> dict:
    """The consolidated view exactly as it has always been: gross entity
    totals with intercompany accounts flagged, nothing eliminated."""
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


@router.get("/consolidated")
async def consolidated(date_from: Optional[str] = Query(None),
                       date_to: Optional[str] = Query(None),
                       view: Optional[str] = Query(None),
                       user: dict = Depends(get_current_user)):
    tids = _owned(user)
    today = today_str()[:10]
    date_from = date_from or f"{today[:4]}-01-01"
    date_to = date_to or today
    gross = await _gross_consolidated(tids, date_from, date_to)

    settings_map = await _settings_map(tids)
    enabled = _effective_enabled(user, tids, settings_map)
    if view not in ("net", "gross"):
        view = "net" if enabled else "gross"

    # The eliminations detail + unmatched list are computed in BOTH
    # states (owner condition): OFF keeps today's numbers untouched but
    # still shows what would eliminate and what cannot be paired.
    names = {e["tenant_id"]: e["name"] for e in gross["entities"]}
    elim = await compute_eliminations(db, tids, names, date_from, date_to)

    out = dict(gross)
    out["eliminations_enabled"] = enabled
    out["eliminations_by_entity"] = settings_map
    out["eliminations_detail"] = elim["detail"]
    out["unmatched_intercompany"] = elim["unmatched"]
    out["view"] = view

    if view == "gross" or not enabled:
        out["view"] = "gross"
        # Lines keep their legacy shape (amount_cents) untouched; the
        # three-column figures ride alongside for the UI when wanted.
        out["lines"] = [
            {**l, "gross_cents": l["amount_cents"],
             "eliminations_cents": elim["elim_by_code"].get(l["code"], 0),
             "consolidated_cents": l["amount_cents"]}
            for l in gross["lines"]]
        out["gross"] = {"total_revenue_cents": gross["total_revenue_cents"],
                        "total_expense_cents": gross["total_expense_cents"],
                        "net_cents": gross["net_cents"],
                        "combined_cash_cents": gross["combined_cash_cents"]}
        return out

    # ---- net view: gross -> eliminations -> consolidated -------------
    elim_by_code = elim["elim_by_code"]
    net_lines = []
    for l in gross["lines"]:
        e = elim_by_code.get(l["code"], 0)
        net_lines.append({**l, "gross_cents": l["amount_cents"],
                          "eliminations_cents": e,
                          "consolidated_cents": l["amount_cents"] - e,
                          "amount_cents": l["amount_cents"] - e})
    rev_elim = elim["totals"]["revenue_cents"]
    exp_elim = elim["totals"]["expense_cents"]
    net_revenue = gross["total_revenue_cents"] - rev_elim
    net_expenses = gross["total_expense_cents"] - exp_elim

    # Balance rows (assets/liabilities) get the same three columns;
    # cumulative balances as of date_to. Cash rows are included so the
    # cash treatment is visible: internal transfers eliminate $0.
    bal_by_code = {}
    from lib.coa import ACCOUNT_TYPE_OF, NAME_OF
    for tid in tids:
        bals = await L._balances(db, tid, as_of=date_to)
        for code, v in bals.items():
            if ACCOUNT_TYPE_OF.get(code) not in ("asset", "liability", "equity"):
                continue  # balance rows are balance-sheet accounts only
            amt = L._net_balance(code, v["debit"], v["credit"])
            row = bal_by_code.setdefault(code, {
                "code": code, "name": None, "gross_cents": 0})
            row["gross_cents"] += amt
    balances = []
    for code in sorted(bal_by_code):
        row = bal_by_code[code]
        if row["gross_cents"] == 0 and code not in elim_by_code:
            continue
        e = elim_by_code.get(code, 0)
        balances.append({"code": code, "name": NAME_OF.get(code) or code,
                         "gross_cents": row["gross_cents"],
                         "eliminations_cents": e,
                         "consolidated_cents": row["gross_cents"] - e})

    out["lines"] = net_lines
    out["balances"] = balances
    out["total_revenue_cents"] = net_revenue
    out["total_expense_cents"] = net_expenses
    out["net_cents"] = net_revenue - net_expenses
    out["gross"] = {"total_revenue_cents": gross["total_revenue_cents"],
                    "total_expense_cents": gross["total_expense_cents"],
                    "net_cents": gross["net_cents"],
                    "combined_cash_cents": gross["combined_cash_cents"]}
    out["eliminations_totals"] = {
        "revenue_cents": rev_elim, "expense_cents": exp_elim,
        "balance_cents": elim["totals"]["balance_cents"],
        "cash_movement_cents": elim["cash_movement_cents"],
        "cash_eliminations_cents": 0}
    out["cash"] = {"gross_cents": gross["combined_cash_cents"],
                   "eliminations_cents": 0,
                   "consolidated_cents": gross["combined_cash_cents"],
                   "note": "Cash moved between your own businesses stays "
                           "inside the group — combined cash is the same "
                           "gross and consolidated."}
    out["note"] = ("Intercompany amounts that match across your businesses "
                   "are eliminated from this group total only. Each "
                   "business's own books are unchanged, and every "
                   "elimination is listed below.")
    return out
