"""Living tax forecast: your estimated tax bill, recalculated with every
transaction.

Takes year-to-date net income, annualizes it, and estimates:
- Self-employment tax (statutory: 12.4% OASDI up to the 2026 SS wage base
  $184,500 + 2.9% Medicare uncapped, on 92.35% of net; +0.9% Additional
  Medicare Tax above $200k)
- Federal income tax (simplified brackets on taxable income after the
  standard deduction and half of SE tax)

Shows the current quarter's pacing ("you're on pace to owe ~$X"), quarterly
breakdown, payments already made, and the safe-harbor target. Brackets and
the standard deduction are workspace-configurable; defaults are labeled.
"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import Optional

from db import db
from lib.audit import audit
from lib.auth import require
from lib.plans import require_feature
from lib.base import Scoped, new_id, now_iso, today_str, cents_to_dollars
from lib import ledger as L

router = APIRouter(prefix="/tax-forecast", tags=["tax-forecast"], dependencies=[Depends(require_feature("advanced_reports"))])

# 2026 federal single-filer brackets as defaults (IRS IR-2025-103).
# Rates apply to taxable income.
DEFAULT_BRACKETS = [
    {"up_to": 12400, "rate": 0.10},
    {"up_to": 50400, "rate": 0.12},
    {"up_to": 105700, "rate": 0.22},
    {"up_to": 201775, "rate": 0.24},
    {"up_to": 256225, "rate": 0.32},
    {"up_to": 640600, "rate": 0.35},
    {"up_to": None, "rate": 0.37},
]
DEFAULT_STD_DEDUCTION = 16100  # single, 2026 (IRS IR-2025-103)

# 2026 Social Security wage base (SSA). Only the 12.4% OASDI portion of SE
# tax caps at the wage base; the 2.9% Medicare portion has no cap.
SS_WAGE_BASE_2026 = 184500
# Additional Medicare Tax: 0.9% on self-employment earnings above $200k.
# Single-filer threshold shown; the app is single-filer only by design
# ("filing" is fixed to "single" in settings).
ADDITIONAL_MEDICARE_THRESHOLD = 200000
ADDITIONAL_MEDICARE_RATE = 0.009
# Estimated-tax safe harbor: 110% of prior-year tax when prior-year AGI
# exceeds $150k, otherwise 100%.
SAFE_HARBOR_AGI_THRESHOLD_CENTS = 15_000_000

QUARTERS_2026 = [
    {"q": "Q1", "from": "2026-01-01", "to": "2026-03-31", "due": "2026-04-15"},
    {"q": "Q2", "from": "2026-04-01", "to": "2026-05-31", "due": "2026-06-15"},
    {"q": "Q3", "from": "2026-06-01", "to": "2026-08-31", "due": "2026-09-15"},
    {"q": "Q4", "from": "2026-09-01", "to": "2026-12-31", "due": "2027-01-15"},
]


def _cfg(user):
    return Scoped(db.tax_forecast_config, user["tenant_id"])


def _payments(user):
    return Scoped(db.tax_payments, user["tenant_id"])


async def _settings(user) -> dict:
    doc = await _cfg(user).find_one({"id": "settings"})
    base = {"brackets": DEFAULT_BRACKETS, "std_deduction": DEFAULT_STD_DEDUCTION,
            "filing": "single", "prior_year_tax_cents": 0, "prior_year_agi_cents": 0,
            "ytd_other_income_cents": 0}
    if doc:
        base.update({k: v for k, v in doc.items() if k not in ("_id", "id", "tenant_id")})
    return base


def _income_tax(taxable: float, brackets: list) -> float:
    tax, prev = 0.0, 0.0
    for b in brackets:
        up = b["up_to"]
        chunk = (taxable if up is None else min(taxable, up)) - prev
        if chunk > 0:
            tax += chunk * b["rate"]
        if up is None or taxable <= up:
            break
        prev = up
    return tax


def _se_tax(se_taxable: float) -> float:
    """2026 self-employment tax on 92.35% of net earnings."""
    oasdi = 0.124 * min(se_taxable, SS_WAGE_BASE_2026)
    medicare = 0.029 * se_taxable
    additional = ADDITIONAL_MEDICARE_RATE * max(0.0, se_taxable - ADDITIONAL_MEDICARE_THRESHOLD)
    return max(0.0, oasdi + medicare + additional)


def _safe_harbor(prior_year_tax_cents: int, prior_year_agi_cents: int) -> int:
    """Estimated-tax safe-harbor target in cents."""
    if prior_year_tax_cents and prior_year_agi_cents > SAFE_HARBOR_AGI_THRESHOLD_CENTS:
        return int(round(prior_year_tax_cents * 1.10))
    return prior_year_tax_cents


def _estimate(net_cents: int, settings: dict) -> dict:
    net = net_cents / 100
    se_taxable = net * 0.9235
    se_tax = _se_tax(se_taxable)
    taxable = max(0, net - se_tax / 2 - settings["std_deduction"]
                  + settings["ytd_other_income_cents"] / 100)
    fed = _income_tax(taxable, settings["brackets"])
    total = se_tax + fed
    return {"net_income": round(net, 2),
            "se_tax": round(se_tax, 2), "federal_income_tax": round(fed, 2),
            "total": round(total, 2), "effective_rate": round(total / net * 100, 1) if net > 0 else 0}


@router.get("")
async def forecast(year: str = Query("2026"), user: dict = Depends(require("tax:read"))):
    settings = await _settings(user)
    today = today_str()[:10]
    ytd = await L.pnl(db, user["tenant_id"], f"{year}-01-01", today)
    ytd_net = ytd["net_income"]

    day_of_year = date.fromisoformat(today).timetuple().tm_yday
    year_days = 366 if date(int(year), 1, 1).timetuple().tm_yday == 1 and \
        date(int(year), 12, 31).timetuple().tm_yday == 366 else 365
    annualized = int(round(ytd_net / max(day_of_year, 1) * year_days))

    ytd_est = _estimate(ytd_net, settings)
    full_est = _estimate(annualized, settings)

    pays = await _payments(user).find_list({"year": year}, sort=[("paid_date", 1)], limit=200)
    paid_total = sum(p.get("amount_cents", 0) for p in pays)

    quarters = []
    for q in QUARTERS_2026:
        qp = await L.pnl(db, user["tenant_id"], q["from"], min(q["to"], today))
        q_est = _estimate(qp["net_income"], settings)
        q_paid = sum(p.get("amount_cents", 0) for p in pays if p.get("quarter") == q["q"])
        quarters.append({"quarter": q["q"], "due": q["due"],
                         "net_cents": qp["net_income"],
                         "owed_cents": int(round(q_est["total"] * 100)),
                         "paid_cents": q_paid,
                         "remaining_cents": int(round(q_est["total"] * 100)) - q_paid})

    # current quarter pacing
    cur = None
    for q in QUARTERS_2026:
        if q["from"] <= today <= q["to"]:
            cur = q
            break
    pace_note = ""
    if cur:
        cq = next(x for x in quarters if x["quarter"] == cur["q"])
        pace_note = (f"You're on pace to owe ~${cq['owed_cents']/100:,.0f} for {cur['q']} "
                     f"(due {cur['due']}).")

    safe_harbor = _safe_harbor(settings["prior_year_tax_cents"],
                               settings.get("prior_year_agi_cents", 0))
    return {
        "year": year, "as_of": today,
        "ytd_net_cents": ytd_net,
        "annualized_net_cents": annualized,
        "ytd_estimate": {k: (int(round(v * 100)) if k != "effective_rate" else v)
                         for k, v in ytd_est.items()},
        "full_year_estimate": {k: (int(round(v * 100)) if k != "effective_rate" else v)
                               for k, v in full_est.items()},
        "paid_cents": paid_total,
        "remaining_cents": int(round(full_est["total"] * 100)) - paid_total,
        "quarters": quarters,
        "pace_note": pace_note,
        "safe_harbor_cents": safe_harbor,
        "settings": {"filing": settings["filing"], "std_deduction": settings["std_deduction"]},
    }


class PaymentBody(BaseModel):
    quarter: str
    amount_cents: int
    paid_date: Optional[str] = None
    note: str = ""


@router.get("/payments")
async def list_payments(year: str = Query("2026"), user: dict = Depends(require("tax:read"))):
    return await _payments(user).find_list({"year": year}, sort=[("paid_date", -1)], limit=200)


@router.post("/payments")
async def record_payment(body: PaymentBody, year: str = Query("2026"),
                         user: dict = Depends(require("tax:write"))):
    if body.amount_cents <= 0:
        from fastapi import HTTPException
        raise HTTPException(422, "Amount must be positive")
    doc = {"id": new_id(), "year": year, "quarter": body.quarter,
           "amount_cents": body.amount_cents,
           "paid_date": (body.paid_date or today_str())[:10],
           "note": body.note, "created_at": now_iso()}
    await _payments(user).insert_one(doc)
    await audit(user, "tax.payment", "payment", doc["id"],
                {"quarter": body.quarter, "amount": body.amount_cents})
    doc.pop("_id", None)
    return doc


class SettingsBody(BaseModel):
    filing: str = "single"
    std_deduction: float = DEFAULT_STD_DEDUCTION
    prior_year_tax_cents: int = 0
    prior_year_agi_cents: int = 0
    ytd_other_income_cents: int = 0


@router.put("/settings")
async def save_settings(body: SettingsBody, user: dict = Depends(require("tax:write"))):
    doc = {"id": "settings", **body.model_dump(), "updated_at": now_iso()}
    await _cfg(user).update_one({"id": "settings"}, {"$set": doc}, upsert=True)
    return {"ok": True}
