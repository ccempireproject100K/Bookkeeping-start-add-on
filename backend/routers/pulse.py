"""Live profit pulse: real-time margin tickers and a plain-English narration
of the business day.

- Today's revenue/expenses/net vs the same weekday last week
- Every active job's live margin, with underwater alerts the moment cost
  passes revenue
- A generated narration ("you're $2,300 ahead of last Tuesday") — no LLM
  needed, computed from the ledger
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends

from db import db
from lib.auth import require
from lib.plans import require_feature
from lib.base import Scoped, today_str, cents_to_dollars
from lib import ledger as L
from routers.jobs import _profit

router = APIRouter(prefix="/pulse", tags=["pulse"], dependencies=[Depends(require_feature("advanced_reports"))])


def _money(cents) -> str:
    return f"${cents_to_dollars(cents or 0):,.2f}"


@router.get("")
async def pulse(user: dict = Depends(require("ledger:read"))):
    tenant_id = user["tenant_id"]
    today = today_str()[:10]
    d = date.fromisoformat(today)
    last_week = (d - timedelta(days=7)).isoformat()

    t = await L.pnl(db, tenant_id, today, today)
    lw = await L.pnl(db, tenant_id, last_week, last_week)
    net_delta = t["net_income"] - lw["net_income"]
    rev_delta = t["revenue_total"] - lw["revenue_total"]

    jobs = await Scoped(db.jobs, tenant_id).find_list(
        {"status": {"$in": ["active", "in_progress", "scheduled"]}}, limit=200)
    tickers = []
    underwater = []
    for j in jobs:
        p = await _profit(user, j)
        margin = p["gross_margin_pct"]
        is_under = p["actual_cost_cents"] > p["actual_revenue_cents"] and p["actual_revenue_cents"] > 0
        est_under = (p["estimated_revenue_cents"] > 0 and
                     p["actual_cost_cents"] > p["estimated_revenue_cents"])
        tickers.append({
            "job_id": j["id"], "job_number": j.get("job_number"),
            "client": j.get("client_name", ""), "status": j.get("status"),
            "revenue_cents": p["actual_revenue_cents"],
            "cost_cents": p["actual_cost_cents"],
            "gross_profit_cents": p["gross_profit_cents"],
            "margin_pct": margin, "underwater": is_under or est_under,
        })
        if is_under or est_under:
            underwater.append(tickers[-1])
    tickers.sort(key=lambda x: x["margin_pct"])

    # narration
    weekday = d.strftime("%A")
    bits = []
    if t["net_income"] >= 0:
        bits.append(f"Today ({weekday}) you're net {_money(t['net_income'])} on {_money(t['revenue_total'])} revenue.")
    else:
        bits.append(f"Today ({weekday}) you're net {_money(t['net_income'])} — costs outran revenue.")
    if net_delta > 0:
        bits.append(f"That's {_money(net_delta)} ahead of last {weekday}.")
    elif net_delta < 0:
        bits.append(f"That's {_money(-net_delta)} behind last {weekday}.")
    else:
        bits.append(f"Right in line with last {weekday}.")
    if underwater:
        names = ", ".join(u["job_number"] or u["job_id"][:8] for u in underwater[:3])
        bits.append(f"⚠ {len(underwater)} job{'s' if len(underwater) != 1 else ''} underwater: {names}.")
    elif tickers:
        best = max(tickers, key=lambda x: x["margin_pct"])
        bits.append(f"All jobs above water — best margin is {best['job_number'] or 'a job'} at {best['margin_pct']}%.")
    if rev_delta > 0 and t["revenue_total"]:
        bits.append(f"Revenue is up {_money(rev_delta)} vs last {weekday}.")

    return {
        "today": today, "weekday": weekday,
        "today_pnl": {"revenue_cents": t["revenue_total"],
                      "expense_cents": t["expense_total"],
                      "net_cents": t["net_income"]},
        "last_week_pnl": {"revenue_cents": lw["revenue_total"],
                          "expense_cents": lw["expense_total"],
                          "net_cents": lw["net_income"]},
        "deltas": {"net_cents": net_delta, "revenue_cents": rev_delta},
        "narration": " ".join(bits),
        "tickers": tickers,
        "underwater": underwater,
        "underwater_count": len(underwater),
    }
