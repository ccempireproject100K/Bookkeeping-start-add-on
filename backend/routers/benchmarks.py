"""Anonymized benchmarks: "shops like yours do X".

Opt-in data moat. Workspaces that opt in contribute an anonymized KPI
snapshot each month. Benchmarks are only ever published as aggregates
(median / p25 / p75) and only when >= 5 workspaces of the same niche
contributed — no individual workspace is ever identifiable.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso
from lib.niches import get_pack

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])

MIN_POOL = 5  # k-anonymity floor


def _pool(user):
    # benchmark pool is GLOBAL across tenants, but rows carry no tenant id
    return db.benchmark_snapshots


@router.get("/status")
async def bench_status(user: dict = Depends(require("ledger:read"))):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    niche = tenant.get("niche_id", "general")
    month = date.today().strftime("%Y-%m")
    n = await _pool(user).count_documents({"niche_id": niche, "month": month})
    contributed = await _pool(user).count_documents(
        {"niche_id": niche, "month": month, "contrib": user["tenant_id"]}) > 0
    return {"opt_in": bool(tenant.get("benchmark_opt_in")),
            "niche_id": niche, "month": month,
            "pool_size": n, "min_pool": MIN_POOL,
            "contributed_this_month": contributed}


class OptInBody(BaseModel):
    opt_in: bool


@router.post("/opt-in")
async def opt_in(body: OptInBody, user: dict = Depends(require("owner"))):
    await db.tenants.update_one({"id": user["tenant_id"]},
                                {"$set": {"benchmark_opt_in": body.opt_in}})
    await audit(user, "benchmarks.opt_in", "tenant", user["tenant_id"],
                {"opt_in": body.opt_in})
    return {"opt_in": body.opt_in}


@router.post("/contribute")
async def contribute(user: dict = Depends(require("ops:manage"))):
    """Contribute this month's anonymized KPI snapshot (opt-in only)."""
    from routers.kpis import _compute
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    if not tenant.get("benchmark_opt_in"):
        raise HTTPException(409, "Benchmarks opt-in is off — enable it first")
    niche = tenant.get("niche_id", "general")
    month = date.today().strftime("%Y-%m")
    computed = await _compute(user, 90)
    # store only the numbers, keyed by a one-way contribution marker
    vals = {k: v["value"] for k, v in computed.items() if v.get("samples", 0) >= 3}
    if not vals:
        raise HTTPException(409, "Not enough data to contribute yet")
    await _pool(user).update_one(
        {"contrib": user["tenant_id"], "month": month},
        {"$set": {"niche_id": niche, "month": month, "values": vals,
                  "created_at": now_iso()}},
        upsert=True)
    await audit(user, "benchmarks.contribute", "tenant", user["tenant_id"],
                {"month": month, "kpis": list(vals)})
    return {"ok": True, "month": month, "kpis": list(vals)}


def _pctile(sorted_vals, p):
    if not sorted_vals:
        return 0
    i = (len(sorted_vals) - 1) * p / 100
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo), 2)


@router.get("")
async def benchmarks(user: dict = Depends(require("ledger:read"))):
    """You vs shops like yours — aggregates only, hidden until the pool is big enough."""
    from routers.kpis import _compute
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    niche = tenant.get("niche_id", "general")
    pack = get_pack(niche)
    month = date.today().strftime("%Y-%m")
    mine = await _compute(user, 90)
    wanted = pack.get("kpis") or [{"id": "avg_ticket", "label": "Average ticket"}]
    cards = []
    pool_sizes = {}
    for w in wanted:
        kid = w["id"]
        rows = await _pool(user).find_list({"niche_id": niche, "month": month}, limit=5000)
        vals = sorted(r["values"][kid] for r in rows if kid in r.get("values", {}))
        pool_sizes[kid] = len(vals)
        card = {"id": kid, "label": w.get("label", kid), "hint": w.get("hint", ""),
                "mine": (mine.get(kid) or {}).get("value"),
                "unit": (mine.get(kid) or {}).get("unit", ""),
                "pool": len(vals), "ready": len(vals) >= MIN_POOL}
        if card["ready"]:
            card["p25"] = _pctile(vals, 25)
            card["median"] = _pctile(vals, 50)
            card["p75"] = _pctile(vals, 75)
            mv = card["mine"] or 0
            card["position"] = ("above" if mv >= card["p75"] else
                                "typical" if mv >= card["p25"] else "below")
        cards.append(card)
    return {"trade": pack.get("trade", "General"), "niche_id": niche, "month": month,
            "min_pool": MIN_POOL, "cards": cards}
