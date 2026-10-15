"""Per-trade KPI dashboards: the numbers that matter for YOUR trade.

Each niche pack declares the KPIs it cares about (callback rate for HVAC,
route density for pest/pool, close rate for real estate...). This endpoint
computes them from the workspace's real books — invoices, estimates, jobs.
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from db import db
from lib.auth import require
from lib.base import Scoped, today_str, cents_to_dollars
from lib.niches import get_pack

router = APIRouter(prefix="/kpis", tags=["kpis"])


def _pct(a, b) -> float:
    return round(a / b * 100, 1) if b else 0.0


async def _compute(user, days: int = 90) -> dict:
    tid = user["tenant_id"]
    today = today_str()[:10]
    start = (date.fromisoformat(today) - timedelta(days=days)).isoformat()

    invs = await Scoped(db.invoices, tid).find_list(
        {"issue_date": {"$gte": start}}, limit=5000)
    ests = await Scoped(db.estimates, tid).find_list(limit=2000)
    jobs = await Scoped(db.jobs, tid).find_list(limit=5000)
    recs = await Scoped(db.recurring_invoices, tid).find_list(limit=1000)

    k = {}
    # avg ticket (trailing period)
    totals = [i.get("total_cents", 0) for i in invs if i.get("total_cents")]
    k["avg_ticket"] = {"value": round(sum(totals) / len(totals) / 100, 2) if totals else 0,
                       "unit": "$", "samples": len(totals)}
    # close rate
    decided = [e for e in ests if e.get("status") in ("accepted", "converted", "declined")]
    won = [e for e in decided if e.get("status") in ("accepted", "converted")]
    k["close_rate"] = {"value": _pct(len(won), len(decided)), "unit": "%",
                       "samples": len(decided)}
    # repeat customer rate
    by_client = {}
    for i in invs:
        c = i.get("customer_id") or i.get("client_name")
        by_client[c] = by_client.get(c, 0) + 1
    repeaters = sum(1 for v in by_client.values() if v > 1)
    k["repeat_rate"] = {"value": _pct(repeaters, len(by_client)), "unit": "%",
                        "samples": len(by_client)}
    # jobs per day (route density proxy)
    job_dates = {}
    for j in jobs:
        d = (j.get("scheduled_date") or j.get("created_at") or "")[:10]
        if d >= start:
            job_dates[d] = job_dates.get(d, 0) + 1
    active_days = len(job_dates)
    k["jobs_per_day"] = {"value": round(sum(job_dates.values()) / active_days, 1) if active_days else 0,
                         "unit": "/day", "samples": active_days}
    # callback rate: same customer, another job within 30 days of a completed one
    cust_jobs = {}
    for j in jobs:
        c = j.get("customer_id") or j.get("client_name")
        d = (j.get("scheduled_date") or j.get("created_at") or "")[:10]
        if c and d:
            cust_jobs.setdefault(c, []).append(d)
    callbacks, total_cj = 0, 0
    for ds in cust_jobs.values():
        ds = sorted(ds)
        total_cj += len(ds)
        for i in range(1, len(ds)):
            try:
                gap = (date.fromisoformat(ds[i]) - date.fromisoformat(ds[i - 1])).days
                if 1 <= gap <= 30:
                    callbacks += 1
                    break
            except ValueError:
                pass
    k["callback_rate"] = {"value": _pct(callbacks, len(cust_jobs)), "unit": "%",
                          "samples": len(cust_jobs)}
    # recurring revenue share
    rec_total = sum(r.get("amount_cents", 0) for r in recs if r.get("status") == "active")
    inv_total = sum(totals)
    k["recurring_share"] = {"value": _pct(rec_total, inv_total), "unit": "%",
                            "samples": len(recs)}
    # AR days: avg days outstanding for unpaid invoices
    agings = []
    for i in invs:
        if i.get("status") in ("issued", "partial"):
            due = (i.get("due_date") or "")[:10]
            if due:
                try:
                    agings.append((date.fromisoformat(today) - date.fromisoformat(due)).days)
                except ValueError:
                    pass
    k["ar_days"] = {"value": round(sum(agings) / len(agings), 1) if agings else 0,
                    "unit": "days", "samples": len(agings)}
    # revenue trend: last 30d vs prior 30d
    mid = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    r1 = sum(i.get("total_cents", 0) for i in invs if (i.get("issue_date") or "") >= mid)
    r0 = sum(i.get("total_cents", 0) for i in invs if (i.get("issue_date") or "") < mid)
    k["revenue_trend"] = {"value": _pct(r1 - r0, r0) if r0 else (100.0 if r1 else 0.0),
                          "unit": "%", "samples": len(invs)}
    return k


@router.get("")
async def kpis(days: int = Query(90, ge=7, le=365),
               user: dict = Depends(require("ledger:read"))):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    pack = get_pack(tenant.get("niche_id"))
    computed = await _compute(user, days)
    wanted = pack.get("kpis") or [
        {"id": "avg_ticket", "label": "Average ticket"},
        {"id": "close_rate", "label": "Close rate"},
        {"id": "repeat_rate", "label": "Repeat customer rate"},
        {"id": "revenue_trend", "label": "Revenue trend"},
    ]
    cards = []
    for w in wanted:
        c = computed.get(w["id"])
        if not c:
            continue
        cards.append({"id": w["id"], "label": w.get("label", w["id"]),
                      "hint": w.get("hint", ""), **c})
    return {"trade": pack.get("trade", "General"), "niche_id": pack.get("id"),
            "days": days, "cards": cards}
