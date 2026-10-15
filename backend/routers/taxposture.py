"""Proactive tax posture: the books watch the calendar so the IRS doesn't surprise you.

- Quarterly estimated-tax deadlines with live countdowns (any year)
- "You're on pace to owe $X" projection vs what's already paid
- Recommended next payment (quarterly estimate vs safe-harbor catch-up)
- Nudges: upcoming deadlines, underpaid quarters, April balance projection
Extends the living tax forecast (#90); not tax advice.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from db import db
from lib.auth import require
from lib.base import today_str, cents_to_dollars

router = APIRouter(prefix="/taxposture", tags=["taxposture"])


def _deadlines(year: int):
    # IRS estimated-tax due dates (shift to next business day ignored for simplicity)
    return [
        {"quarter": "Q1", "due": f"{year}-04-15"},
        {"quarter": "Q2", "due": f"{year}-06-15"},
        {"quarter": "Q3", "due": f"{year}-09-15"},
        {"quarter": "Q4", "due": f"{year + 1}-01-15"},
    ]


@router.get("")
async def posture(year: str = Query("2026"), user: dict = Depends(require("tax:read"))):
    from routers.taxforecast import forecast, _settings
    fc = await forecast(year, user)
    settings = await _settings(user)
    today = today_str()[:10]
    y = int(year)

    deadlines = []
    for d in _deadlines(y):
        try:
            days_left = (date.fromisoformat(d["due"]) - date.fromisoformat(today)).days
        except ValueError:
            days_left = 999
        q = next((x for x in fc["quarters"] if x["quarter"] == d["quarter"]), None)
        deadlines.append({**d, "days_left": days_left,
                          "owed_cents": q["owed_cents"] if q else 0,
                          "paid_cents": q["paid_cents"] if q else 0,
                          "remaining_cents": q["remaining_cents"] if q else 0})

    upcoming = [d for d in deadlines if d["days_left"] >= 0]
    nxt = min(upcoming, key=lambda d: d["days_left"]) if upcoming else None

    # recommended next payment: what's left on the upcoming quarter,
    # or safe-harbor catch-up if that number is smaller
    rec_cents = 0
    rec_basis = ""
    if nxt and nxt["remaining_cents"] > 0:
        rec_cents = nxt["remaining_cents"]
        rec_basis = f"remaining {nxt['quarter']} estimate"
    prior = settings.get("prior_year_tax_cents", 0)
    if prior:
        # 110% safe harbor for higher earners; we use 100% as the floor signal
        harbor = prior
        catchup = max(0, harbor - fc["paid_cents"])
        if catchup and (not rec_cents or catchup < rec_cents):
            rec_cents = catchup
            rec_basis = "safe-harbor catch-up (100% of prior-year tax)"

    # April projection
    projected_due = fc["full_year_estimate"]["total"] - fc["paid_cents"]

    nudges = []
    if nxt and 0 <= nxt["days_left"] <= 14 and nxt["remaining_cents"] > 0:
        nudges.append({"severity": "high",
                       "text": f"{nxt['quarter']} estimated tax is due in {nxt['days_left']} days "
                               f"({nxt['due']}) — ${nxt['remaining_cents']/100:,.0f} still unpaid."})
    for d in deadlines:
        if d["days_left"] < -30 and d["remaining_cents"] > 0:
            nudges.append({"severity": "high",
                           "text": f"{d['quarter']} estimated tax was due {d['due']} and "
                                   f"${d['remaining_cents']/100:,.0f} is still unpaid — penalties may accrue."})
    if projected_due > 0:
        nudges.append({"severity": "medium",
                       "text": f"On pace to owe ~${projected_due/100:,.0f} with your April return "
                               f"at current payments. Consider raising quarterly payments."})
    elif projected_due < -100000:
        nudges.append({"severity": "low",
                       "text": f"You're on pace to overpay by ~${-projected_due/100:,.0f} — "
                               f"expect a refund, or trim remaining quarterlies."})

    return {
        "year": year, "as_of": today,
        "deadlines": deadlines,
        "next_deadline": nxt,
        "recommended_payment_cents": rec_cents,
        "recommended_basis": rec_basis,
        "projected_april_balance_cents": projected_due,
        "pace_note": fc.get("pace_note", ""),
        "safe_harbor_cents": fc.get("safe_harbor_cents", 0),
        "nudges": nudges,
        "disclaimer": "Simplified estimate, not tax advice. Confirm with your tax pro.",
    }
