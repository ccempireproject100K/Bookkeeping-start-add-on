"""Can-I-afford-it simulator: cash-flow-aware answers to big decisions.

Takes the workspace's real numbers (current cash, trailing average monthly net)
and projects N months forward with a proposed cost layered in — a hire, a truck,
a lease. Verdicts: yes / tight / no, with the lowest projected cash point.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from db import db
from lib.auth import require
from lib.base import today_str
from lib import ledger as L

router = APIRouter(prefix="/simulator", tags=["simulator"])


def _month_range(n_back: int) -> list:
    """Last n_back full months as (first_day, last_day), oldest first."""
    today = date.fromisoformat(today_str()[:10])
    first = date(today.year, today.month, 1)
    months = []
    for i in range(n_back, 0, -1):
        y, m = first.year, first.month - i
        while m <= 0:
            m += 12
            y -= 1
        start = date(y, m, 1)
        nm = m + 1 if m < 12 else 1
        ny = y if m < 12 else y + 1
        end = date(ny, nm, 1)
        months.append((start.isoformat(), (end.fromordinal(end.toordinal() - 1)).isoformat()))
    return months


async def _baseline(user) -> dict:
    tenant_id = user["tenant_id"]
    bals = await L._balances(db, tenant_id, as_of=today_str())

    def bal(code):
        v = bals.get(code, {"debit": 0, "credit": 0})
        return L._net_balance(code, v["debit"], v["credit"])

    cash = bal("1010") + bal("1020") + bal("1021") + bal("1022") + bal("1023") + bal("1050")
    nets = []
    for start, end in _month_range(6):
        p = await L.pnl(db, tenant_id, start, end)
        nets.append(p["net_income"])
    avg_net = sum(nets) / len(nets) if nets else 0
    return {"cash_cents": cash, "avg_monthly_net_cents": int(round(avg_net)),
            "trailing_nets": nets}


@router.get("/baseline")
async def baseline(user: dict = Depends(require("ledger:read"))):
    b = await _baseline(user)
    runway = None
    if b["avg_monthly_net_cents"] < 0:
        runway = round(b["cash_cents"] / -b["avg_monthly_net_cents"], 1)
    return {**b, "runway_months": runway}


class AffordBody(BaseModel):
    name: str = "Scenario"
    kind: str = "recurring"          # recurring | one_time | hire
    monthly_cost_cents: int = 0      # for recurring
    one_time_cents: int = 0          # for one_time (month 1)
    annual_salary_cents: int = 0     # for hire
    burden_percent: float = 25.0     # payroll taxes + benefits on top of salary
    months: int = 12
    min_cash_cents: int = 500000     # $5k safety floor


@router.post("/afford")
async def afford(body: AffordBody, user: dict = Depends(require("ledger:read"))):
    base = await _baseline(user)
    months = max(1, min(body.months, 36))

    if body.kind == "hire":
        monthly = int(round(body.annual_salary_cents / 12 * (1 + body.burden_percent / 100)))
    elif body.kind == "one_time":
        monthly = 0
    else:
        monthly = body.monthly_cost_cents

    cash = base["cash_cents"]
    proj = []
    low_point = {"month": 1, "cash_cents": cash}
    for m in range(1, months + 1):
        cash += base["avg_monthly_net_cents"] - monthly
        if m == 1:
            cash -= body.one_time_cents if body.kind == "one_time" else 0
        proj.append({"month": m, "cash_cents": cash,
                     "net_cents": base["avg_monthly_net_cents"] - monthly})
        if cash < low_point["cash_cents"]:
            low_point = {"month": m, "cash_cents": cash}

    floor = body.min_cash_cents
    if low_point["cash_cents"] < 0:
        verdict, note = "no", "Projected cash goes negative — not affordable on current run-rate."
    elif low_point["cash_cents"] < floor:
        verdict, note = "no", f"Dips below your ${floor/100:,.0f} safety floor in month {low_point['month']}."
    elif low_point["cash_cents"] < floor * 1.5:
        verdict, note = "tight", "Affordable but tight — you'd run close to your safety floor."
    else:
        verdict, note = "yes", "Comfortably affordable at your current run-rate."

    return {
        "name": body.name, "kind": body.kind,
        "monthly_cost_cents": monthly if body.kind != "one_time" else 0,
        "one_time_cents": body.one_time_cents if body.kind == "one_time" else 0,
        "baseline": base, "months": months,
        "projection": proj, "low_point": low_point,
        "verdict": verdict, "note": note,
    }
