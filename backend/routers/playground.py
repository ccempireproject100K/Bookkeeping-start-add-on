"""Scenario playground: drag the levers, watch the year play out.

Levers: revenue change % (raise prices / win more jobs), new monthly payroll,
expense cut %, one-time investment. Projects 12 months of P&L and cash against
the do-nothing baseline, using the shop's real trailing revenue & expenses.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from db import db
from lib.auth import require
from lib.plans import require_feature
from lib.base import today_str
from lib import ledger as L
from routers.simulator import _baseline, _month_range

router = APIRouter(prefix="/playground", tags=["playground"], dependencies=[Depends(require_feature("advanced_reports"))])


async def _rev_exp_avg(user) -> dict:
    tenant_id = user["tenant_id"]
    revs, exps = [], []
    for start, end in _month_range(6):
        p = await L.pnl(db, tenant_id, start, end)
        revs.append(p["revenue_total"])
        exps.append(p["expense_total"])
    n = len(revs) or 1
    return {"avg_rev": int(sum(revs) / n), "avg_exp": int(sum(exps) / n)}


class PlaygroundBody(BaseModel):
    revenue_change_pct: float = 0.0     # e.g. 10 = raise prices 10%
    new_payroll_monthly_cents: int = 0  # extra monthly payroll
    expense_cut_pct: float = 0.0        # e.g. 5 = cut expenses 5%
    one_time_cents: int = 0             # equipment / investment in month 1
    months: int = 12


@router.post("/run")
async def run_playground(body: PlaygroundBody, user: dict = Depends(require("ledger:read"))):
    base = await _baseline(user)
    mix = await _rev_exp_avg(user)
    months = max(1, min(body.months, 24))

    rev = int(round(mix["avg_rev"] * (1 + body.revenue_change_pct / 100)))
    exp = int(round(mix["avg_exp"] * (1 - body.expense_cut_pct / 100))) + body.new_payroll_monthly_cents
    scen_net = rev - exp
    base_net = mix["avg_rev"] - mix["avg_exp"]

    scen, base_line = [], []
    scen_cash, base_cash = base["cash_cents"], base["cash_cents"]
    for m in range(1, months + 1):
        sn = scen_net - (body.one_time_cents if m == 1 else 0)
        scen_cash += sn
        base_cash += base_net
        scen.append({"month": m, "net_cents": sn, "cash_cents": scen_cash})
        base_line.append({"month": m, "net_cents": base_net, "cash_cents": base_cash})

    end_delta = scen_cash - base_cash
    verdict = "ahead" if end_delta > 0 else "behind" if end_delta < 0 else "flat"
    return {
        "levers": body.model_dump(),
        "baseline_cash_cents": base["cash_cents"],
        "avg_monthly_revenue_cents": mix["avg_rev"],
        "avg_monthly_expense_cents": mix["avg_exp"],
        "scenario_monthly_net_cents": scen_net,
        "scenario": scen, "baseline": base_line,
        "end_cash_delta_cents": end_delta, "verdict": verdict,
    }
