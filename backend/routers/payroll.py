"""Payroll tracking & calculation: pay schedules, REAL gross-to-net pay runs,
immutable pay records, tax-liability tracking, printable payslips, journal
posting, and filing workpapers (941 / 940 / W-2 data summaries).

SCOPE — what this module does and does NOT do (same text shown in the UI):
  DOES: calculate withholding with real tables (federal Pub 15-T percentage
  method; CA Method B; NY exact method), apply the Social Security wage cap
  and FUTA/SUTA wage bases against year-to-date wages, track every payroll
  tax liability with its deposit due date, produce pay stubs, post the
  payroll journal entry, and accumulate the numbers a 941/940/W-2 needs.
  DOES NOT: pay employees (no direct deposit, no checks), deposit withheld
  taxes with any agency, or file any return. Moving money stays with the
  business (or a payroll partner) — this app makes sure the amounts, the
  accounts, and the due dates are right and visible.

EXECUTOR CONTRACT (where a money-movement partner attaches later):
  A posted run is FINALIZED and immutable: its per-employee snapshots live in
  `payroll_pay_records` and its obligations live in `payroll_liabilities`
  (agency, kind, amount_cents, due_date, status). An executor (Gusto
  Embedded / Check / Zeal, or an in-house EFTPS workflow) consumes exactly
  those two collections: pay the net amounts from the pay records, remit the
  liabilities by their due dates, then mark each liability 'remitted' via
  PATCH /payroll/liabilities/{id}. No calculation code needs to change.

Journal posting for a posted run (all amounts in cents):
    Dr 6110 Wages & Salaries            (gross)
    Dr 6120 Payroll Tax Expense         (employer FICA + FUTA + SUTA)
        Cr 2120 Payroll Liabilities     (employee taxes withheld, employer
                                         taxes, pre-tax deductions held,
                                         garnishments held, other deductions)
        Cr 4200 Chair Rental Income     (booth rent collected by the shop)
        Cr 2320 Wages Payable           (net pay owed to staff — disbursement
                                         happens OUTSIDE this app)

Corrections: a posted run is never edited. POST /runs/{id}/reverse posts an
offsetting journal entry, reverses its liabilities, and marks its pay
records reversed — the audit trail stays complete.
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P
from lib import timeutil as T
from lib import payroll_calc as PC
from lib import payroll_config_2026 as CFG

router = APIRouter(prefix="/payroll", tags=["payroll"])

DISCLAIMER = (
    "Payroll tracking calculates real withholding and tracks every amount and "
    "due date, but it does not pay employees, does not deposit taxes with any "
    "agency, and does not file returns. You (or a payroll provider such as "
    "Gusto or Check) move the money — use the liability schedule in this app "
    "so the amounts and dates are right."
)

PERIODS_PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12}
TEAM_ROLES = ["owner", "barber"]  # roles treated as payroll team members
FILING_STATUSES = ("single", "married_filing_jointly",
                   "married_filing_separately", "head_of_household")


def schedules(user):
    return Scoped(db.pay_schedules, user["tenant_id"])


def runs(user):
    return Scoped(db.payroll_runs, user["tenant_id"])


def liabilities(user):
    return Scoped(db.payroll_liabilities, user["tenant_id"])


def pay_records(user):
    return Scoped(db.payroll_pay_records, user["tenant_id"])


def _pct_cents(cents: int, pct: float) -> int:
    if not cents or not pct:
        return 0
    return int((Decimal(cents) * Decimal(str(pct)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _salary_period_cents(salary_monthly_cents: int, frequency: str) -> int:
    periods = PERIODS_PER_YEAR.get(frequency, 12)
    return int((Decimal(salary_monthly_cents) * 12 / periods).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- pay schedules

class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    frequency: str = Field(pattern=r"^(weekly|biweekly|monthly)$")
    next_run: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD
    active: bool = True


@router.get("/schedules")
async def list_schedules(user: dict = Depends(require("payroll:read"))):
    return await schedules(user).find_list(sort=[("name", 1)], limit=200)


@router.post("/schedules")
async def create_schedule(body: ScheduleIn, user: dict = Depends(require("payroll:write"))):
    doc = {"id": new_id(), **body.model_dump(), "created_at": now_iso()}
    await schedules(user).insert_one(doc)
    await L.audit(db, user["tenant_id"], user, "payroll.schedule.create", {"name": doc["name"]})
    return doc


@router.put("/schedules/{sid}")
async def update_schedule(sid: str, body: ScheduleIn, user: dict = Depends(require("payroll:write"))):
    doc = await schedules(user).find_one({"id": sid})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await schedules(user).update_one({"id": sid}, {"$set": body.model_dump()})
    return await schedules(user).find_one({"id": sid})


@router.delete("/schedules/{sid}")
async def delete_schedule(sid: str, user: dict = Depends(require("payroll:write"))):
    doc = await schedules(user).find_one({"id": sid})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await schedules(user).c.delete_one({"tenant_id": user["tenant_id"], "id": sid})
    await L.audit(db, user["tenant_id"], user, "payroll.schedule.delete", {"id": sid})
    return {"ok": True}


# ---------------------------------------------------------------- team / employees

def _team_member(u):
    ps = u.get("pay_settings", {}) or {}
    w4 = ps.get("w4", {}) or {}
    return {
        "id": u["id"], "name": u.get("name"), "email": u.get("email"), "role": u.get("role"),
        "commission_rate": u.get("commission_rate", 0) or 0,
        "hourly_rate_cents": ps.get("hourly_rate_cents", 0) or 0,
        "salary_monthly_cents": ps.get("salary_monthly_cents", 0) or 0,
        "fed_withholding_pct": ps.get("fed_withholding_pct", 0) or 0,
        "booth_rent_cents": ps.get("booth_rent_cents", 0) or 0,
        # --- employee tax profile (payroll tracking) ---
        "pay_type": ps.get("pay_type") or ("salary" if (ps.get("salary_monthly_cents") or 0) > 0 else "hourly"),
        "department": ps.get("department") or "",
        "work_state": (ps.get("work_state") or "").upper() or None,
        "filing_status": ps.get("filing_status") or "single",
        "w4": {
            "filing_status": ps.get("filing_status") or "single",
            "multiple_jobs": bool(w4.get("multiple_jobs")),
            "dependents_cents": w4.get("dependents_cents", 0) or 0,
            "other_income_cents": w4.get("other_income_cents", 0) or 0,
            "deductions_cents": w4.get("deductions_cents", 0) or 0,
            "extra_withholding_cents": w4.get("extra_withholding_cents", 0) or 0,
        },
        "w4_on_file": bool(ps.get("w4_on_file")),
        "state_allowances": ps.get("state_allowances", 0) or 0,
        "state_deduction_allowances": ps.get("state_deduction_allowances", 0) or 0,
        "extra_withholding_state_cents": ps.get("extra_withholding_state_cents", 0) or 0,
        "standing_deductions": ps.get("standing_deductions", []) or [],
    }


@router.get("/team")
async def list_team(user: dict = Depends(require("payroll:read"))):
    """Active team members with pay settings + employee tax profile (W-4
    elections, work state, standing deductions)."""
    rows = await db.users.find(
        {"tenant_id": user["tenant_id"], "role": {"$in": TEAM_ROLES}},
        {"_id": 0, "password_hash": 0},
    ).sort("name", 1).to_list(200)
    return [_team_member(r) for r in rows]


class StandingDeductionIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    category: str = Field(pattern=r"^(pretax_retirement|pretax_section125|pretax_hsa|posttax|garnishment)$")
    kind: str = Field(pattern=r"^(percent|amount)$")
    value: float = Field(ge=0)  # percent (of gross; of disposable for garnishments) or $ amount
    garnishment_type: Optional[str] = Field(default=None, pattern=r"^(child_support|other)$")
    percent_of_disposable: Optional[float] = Field(default=None, ge=0, le=100)


class PaySettingsIn(BaseModel):
    hourly_rate: Optional[float] = Field(default=None, ge=0, le=10000)       # $/hr
    salary_monthly: Optional[float] = Field(default=None, ge=0, le=1000000)  # $/month
    fed_withholding_pct: Optional[float] = Field(default=None, ge=0, le=60)  # LEGACY flat-% override (est.)
    booth_rent: Optional[float] = Field(default=None, ge=0, le=100000)       # $ flat per run
    # --- employee tax profile ---
    pay_type: Optional[str] = Field(default=None, pattern=r"^(hourly|salary)$")
    department: Optional[str] = Field(default=None, max_length=60)
    work_state: Optional[str] = Field(default=None, max_length=2)            # e.g. "CA"
    filing_status: Optional[str] = Field(default=None, pattern=r"^(single|married_filing_jointly|married_filing_separately|head_of_household)$")
    w4_on_file: Optional[bool] = None
    w4_dependents: Optional[float] = Field(default=None, ge=0)               # $ annual, W-4 Step 3
    w4_other_income: Optional[float] = Field(default=None, ge=0)             # $ annual, Step 4(a)
    w4_deductions: Optional[float] = Field(default=None, ge=0)               # $ annual, Step 4(b)
    w4_extra_withholding: Optional[float] = Field(default=None, ge=0)        # $ per period, Step 4(c)
    w4_multiple_jobs: Optional[bool] = None                                  # Step 2(c) checkbox
    state_allowances: Optional[int] = Field(default=None, ge=0, le=99)       # DE 4 / IT-2104 regular allowances
    state_deduction_allowances: Optional[int] = Field(default=None, ge=0, le=99)  # DE 4 estimated-deduction allowances (CA)
    extra_withholding_state: Optional[float] = Field(default=None, ge=0)     # $ per period, state
    standing_deductions: Optional[List[StandingDeductionIn]] = None


@router.patch("/team/{user_id}")
async def set_pay_settings(user_id: str, body: PaySettingsIn, user: dict = Depends(require("payroll:write"))):
    target = await db.users.find_one({"tenant_id": user["tenant_id"], "id": user_id})
    if not target:
        raise HTTPException(status_code=404, detail="Team member not found")
    patch = {}
    if body.hourly_rate is not None:
        patch["pay_settings.hourly_rate_cents"] = to_cents(body.hourly_rate)
    if body.salary_monthly is not None:
        patch["pay_settings.salary_monthly_cents"] = to_cents(body.salary_monthly)
    if body.fed_withholding_pct is not None:
        patch["pay_settings.fed_withholding_pct"] = body.fed_withholding_pct
    if body.booth_rent is not None:
        patch["pay_settings.booth_rent_cents"] = to_cents(body.booth_rent)
    if body.pay_type is not None:
        patch["pay_settings.pay_type"] = body.pay_type
    if body.department is not None:
        patch["pay_settings.department"] = body.department
    if body.work_state is not None:
        patch["pay_settings.work_state"] = body.work_state.upper()
    if body.filing_status is not None:
        patch["pay_settings.filing_status"] = body.filing_status
    if body.w4_on_file is not None:
        patch["pay_settings.w4_on_file"] = body.w4_on_file
    if body.w4_dependents is not None:
        patch["pay_settings.w4.dependents_cents"] = to_cents(body.w4_dependents)
    if body.w4_other_income is not None:
        patch["pay_settings.w4.other_income_cents"] = to_cents(body.w4_other_income)
    if body.w4_deductions is not None:
        patch["pay_settings.w4.deductions_cents"] = to_cents(body.w4_deductions)
    if body.w4_extra_withholding is not None:
        patch["pay_settings.w4.extra_withholding_cents"] = to_cents(body.w4_extra_withholding)
    if body.w4_multiple_jobs is not None:
        patch["pay_settings.w4.multiple_jobs"] = body.w4_multiple_jobs
    if body.state_allowances is not None:
        patch["pay_settings.state_allowances"] = body.state_allowances
    if body.state_deduction_allowances is not None:
        patch["pay_settings.state_deduction_allowances"] = body.state_deduction_allowances
    if body.extra_withholding_state is not None:
        patch["pay_settings.extra_withholding_state_cents"] = to_cents(body.extra_withholding_state)
    if body.standing_deductions is not None:
        patch["pay_settings.standing_deductions"] = [d.model_dump() for d in body.standing_deductions]
    if patch:
        await db.users.update_one({"tenant_id": user["tenant_id"], "id": user_id}, {"$set": patch})
        await L.audit(db, user["tenant_id"], user, "payroll.pay_settings.update",
                      {"user_id": user_id, **patch})
    doc = await db.users.find_one({"tenant_id": user["tenant_id"], "id": user_id}, {"_id": 0, "password_hash": 0})
    return _team_member(doc)


class PayrollSettingsIn(BaseModel):
    default_fed_withholding_pct: Optional[float] = Field(default=None, ge=0, le=60)
    suta_rates: Optional[Dict[str, float]] = None  # {state: rate %} tenant overrides


@router.get("/settings")
async def get_payroll_settings(user: dict = Depends(require("payroll:read"))):
    doc = await db.payroll_settings.find_one({"tenant_id": user["tenant_id"]}, {"_id": 0})
    doc = doc or {}
    return {
        "default_fed_withholding_pct": doc.get("default_fed_withholding_pct", 0),
        "suta_rates": doc.get("suta_rates", {}) or {},
        "config_version": CFG.CONFIG_VERSION,
        "fica_pct": 7.65,  # informational: SS 6.2% + Medicare 1.45% under the cap
        "disclaimer": DISCLAIMER,
    }


@router.patch("/settings")
async def update_payroll_settings(body: PayrollSettingsIn, user: dict = Depends(require("payroll:write"))):
    patch = {"updated_at": now_iso()}
    if body.default_fed_withholding_pct is not None:
        patch["default_fed_withholding_pct"] = body.default_fed_withholding_pct
    if body.suta_rates is not None:
        patch["suta_rates"] = {k.upper(): v for k, v in body.suta_rates.items()}
    await db.payroll_settings.update_one(
        {"tenant_id": user["tenant_id"]}, {"$set": patch}, upsert=True)
    return await get_payroll_settings(user)


async def _tenant_suta_rates_decimal(user) -> dict:
    """Tenant SUTA overrides as decimal rates {state: 0.034}."""
    doc = await db.payroll_settings.find_one({"tenant_id": user["tenant_id"]}, {"_id": 0})
    raw = (doc or {}).get("suta_rates", {}) or {}
    return {k.upper(): float(v) / 100.0 for k, v in raw.items()}


# ---------------------------------------------------------------- YTD

async def _ytd_for(user, user_id: str, year: int) -> dict:
    """Year-to-date accumulations from FINALIZED (posted, non-reversed) pay
    records. Drafts and approved-but-unposted runs never move YTD — only
    finalized pay counts toward wage-base caps."""
    if not user_id:
        return {"gross_cents": 0, "fica_wages_cents": 0,
                "federal_withholding_cents": 0, "net_cents": 0}
    recs = await pay_records(user).find_list(
        {"user_id": user_id, "tax_year": year, "reversed": {"$ne": True}}, limit=5000)
    return {
        "gross_cents": sum(r.get("gross_cents", 0) for r in recs),
        "fica_wages_cents": sum(r.get("fica_wages_cents") or r.get("gross_cents", 0) for r in recs),
        "federal_withholding_cents": sum(r.get("federal_withholding_cents", 0) for r in recs),
        "net_cents": sum(r.get("net_cents", 0) for r in recs),
    }


# ---------------------------------------------------------------- pay runs

class DeductionIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern=r"^(percent|amount)$")
    value: float = Field(ge=0)
    category: str = Field(default="posttax",
                          pattern=r"^(pretax_retirement|pretax_section125|pretax_hsa|posttax|garnishment)$")
    garnishment_type: Optional[str] = Field(default=None, pattern=r"^(child_support|other)$")
    percent_of_disposable: Optional[float] = Field(default=None, ge=0, le=100)


class RunItemIn(BaseModel):
    user_id: Optional[str] = None
    name: Optional[str] = None
    pay_type: Optional[str] = Field(default=None, pattern=r"^(hourly|salary)$")
    # None => pre-fill from approved time entries for the pay period (hourly only).
    # Any explicit number (including 0) is a manual override and wins.
    hours: Optional[float] = Field(default=None, ge=0)
    weekly_hours: Optional[List[float]] = Field(default=None)  # per-week split for exact OT
    rate: Optional[float] = Field(default=None, ge=0)          # $/hr override
    amount: Optional[float] = Field(default=None, ge=0)        # salary $ per period override
    commission: float = Field(default=0, ge=0)                 # extra commission $ this period
    tips: float = Field(default=0, ge=0)                       # reported tips $ this period
    fed_withholding_pct: Optional[float] = Field(default=None, ge=0, le=60)  # LEGACY override
    booth_rent: Optional[float] = Field(default=None, ge=0)    # $ flat override
    deductions: List[DeductionIn] = []                         # ad-hoc extras


class RunIn(BaseModel):
    schedule_id: Optional[str] = None
    period_start: str = Field(min_length=10, max_length=10)
    period_end: str = Field(min_length=10, max_length=10)
    pay_date: Optional[str] = Field(default=None, min_length=10, max_length=10)  # defaults to period_end
    notes: str = Field(default="", max_length=500)
    items: List[RunItemIn] = []  # empty => auto-build from team pay settings


async def _member_map(user):
    rows = await db.users.find(
        {"tenant_id": user["tenant_id"], "role": {"$in": TEAM_ROLES}},
        {"_id": 0, "password_hash": 0},
    ).to_list(200)
    return {r["id"]: _team_member(r) for r in rows}


async def _default_fed_pct(user):
    doc = await db.payroll_settings.find_one({"tenant_id": user["tenant_id"]}, {"_id": 0})
    return (doc or {}).get("default_fed_withholding_pct", 0) or 0


async def _fetch_period_entries(user, user_id: str, start: str, end: str):
    """Approved time entries for one staffer inside the pay period."""
    return await Scoped(db.time_entries, user["tenant_id"]).find_list(
        {"user_id": user_id, "status": {"$in": list(T.APPROVED_STATUSES)},
         "date": {"$gte": start, "$lte": end}},
        sort=[("date", 1)], limit=5000)


async def _resolve_hours(user, inp: RunItemIn, member, period_start: str, period_end: str):
    """Resolve an item's hours: explicit hours win; otherwise (hourly staff only)
    pre-fill from approved time entries in the pay period.

    Returns (hours, hours_source, time_entry_ids)."""
    if inp.hours is not None:
        return inp.hours, "manual", []
    member = member or {}
    pay_type = inp.pay_type or member.get("pay_type") or \
        ("salary" if (member.get("salary_monthly_cents") or 0) > 0 else "hourly")
    if pay_type != "hourly" or not inp.user_id:
        return 0.0, "manual", []
    entries = await _fetch_period_entries(user, inp.user_id, period_start, period_end)
    hours, ids = T.period_hours(entries, inp.user_id, period_start, period_end)
    return hours, "time_entries", ids


def _weekly_split_from_entries(entries, user_id: str):
    """Group approved-entry hours by ISO week -> [week_hours, ...] for exact
    overtime (FLSA workweek) computation."""
    weeks: Dict[str, float] = {}
    for e in entries or []:
        if e.get("user_id") != user_id or e.get("status") not in T.APPROVED_STATUSES:
            continue
        d = e.get("date") or ""
        try:
            iso = date.fromisoformat(d).isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        except ValueError:
            key = "unknown"
        weeks[key] = weeks.get(key, 0.0) + (e.get("hours") or 0)
    return [round(weeks[k], 2) for k in sorted(weeks)]


def _split_overtime(weekly_hours):
    """(regular_hours, overtime_hours) with the 40h/week FLSA threshold."""
    reg = sum(min(w, 40.0) for w in weekly_hours)
    ot = sum(max(w - 40.0, 0.0) for w in weekly_hours)
    return round(reg, 2), round(ot, 2)


def _approx_weekly(total_hours: float, frequency: str):
    weeks = {"weekly": 1, "biweekly": 2, "monthly": 52 / 12}.get(frequency, 1)
    n = max(1, round(weeks))
    per = total_hours / n
    split = [round(per, 2)] * n
    split[-1] = round(total_hours - sum(split[:-1]), 2)  # keep the total exact
    return split


def _build_item(member, frequency, inp: RunItemIn, default_fed_pct,
                hours_source="manual", time_entry_ids=None, ytd=None,
                weekly_hours=None, tenant_suta_rates=None):
    """Compute one employee's paycheck with the real engine (lib.payroll_calc).

    Back-compat: a legacy flat fed_withholding_pct (run input, then member
    setting, then tenant default) still wins as an explicit OVERRIDE and is
    labeled as an estimate; with no override the Pub 15-T tables apply."""
    member = member or {}
    name = inp.name or member.get("name") or "Unnamed"
    pay_type = inp.pay_type or member.get("pay_type") or \
        ("salary" if (member.get("salary_monthly_cents") or 0) > 0 else "hourly")
    hours = inp.hours if inp.hours is not None else 0.0
    rate_cents = to_cents(inp.rate) if inp.rate is not None else (member.get("hourly_rate_cents") or 0)

    ot_note = None
    if pay_type == "hourly":
        weeks = weekly_hours or _approx_weekly(hours, frequency)
        if not weekly_hours and hours:
            ot_note = ("Overtime split estimated by spreading hours evenly "
                       "across the period's weeks; supply weekly hours or use "
                       "time entries for an exact FLSA split.")
        reg_hours, ot_hours = _split_overtime(weeks)
        wage_cents = int((Decimal(rate_cents) * Decimal(str(reg_hours))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))
        ot_cents = int((Decimal(rate_cents) * Decimal("1.5") * Decimal(str(ot_hours))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))
    else:
        reg_hours, ot_hours, ot_cents = 0.0, 0.0, 0
        if inp.amount is not None:
            wage_cents = to_cents(inp.amount)
        else:
            wage_cents = _salary_period_cents(member.get("salary_monthly_cents") or 0, frequency)

    commission_cents = to_cents(inp.commission)
    tips_cents = to_cents(inp.tips)
    gross = wage_cents + ot_cents + commission_cents + tips_cents

    # ---- deductions fed to the engine: member standing + legacy booth rent
    #      + ad-hoc run deductions.
    engine_deductions = []
    for d in member.get("standing_deductions", []) or []:
        engine_deductions.append({
            "label": d["label"], "category": d.get("category", "posttax"),
            "kind": d.get("kind", "amount"), "value": d.get("value", 0),
            "garnishment_type": d.get("garnishment_type"),
            "percent_of_disposable": (float(d["percent_of_disposable"]) / 100.0)
            if d.get("category") == "garnishment" and d.get("percent_of_disposable") else None,
        })
    booth_cents = to_cents(inp.booth_rent) if inp.booth_rent is not None \
        else (member.get("booth_rent_cents") or 0)
    if booth_cents:
        engine_deductions.append({"label": "Booth rent", "category": "booth_rent",
                                  "kind": "amount", "value": 0,
                                  "amount_cents": booth_cents})
    for d in inp.deductions:
        engine_deductions.append({
            "label": d.label, "category": d.category, "kind": d.kind, "value": d.value,
            "garnishment_type": d.garnishment_type,
            "percent_of_disposable": (d.percent_of_disposable / 100.0)
            if d.percent_of_disposable is not None else None,
        })

    profile = {
        "work_state": member.get("work_state"),
        "filing_status": member.get("filing_status") or "single",
        "state_allowances": member.get("state_allowances", 0),
        "state_deduction_allowances": member.get("state_deduction_allowances", 0),
        "extra_withholding_state_cents": member.get("extra_withholding_state_cents", 0),
    }
    w4 = dict(member.get("w4") or {})
    w4.setdefault("filing_status", profile["filing_status"])

    try:
        calc = PC.compute_paycheck(
            gross_cents=gross, frequency=frequency, w4=w4, profile=profile,
            deductions_in=engine_deductions, ytd=ytd or {},
            tenant_suta_rates=tenant_suta_rates or {})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"{e} for {name}")

    # ---- legacy flat-% override (explicitly labeled estimate)
    fed_override_pct = inp.fed_withholding_pct if inp.fed_withholding_pct is not None \
        else (member.get("fed_withholding_pct") or default_fed_pct)
    override_note = None
    if fed_override_pct:
        override_amt = _pct_cents(calc["fit_wages_cents"], fed_override_pct)
        calc = _apply_fed_override(calc, override_amt, fed_override_pct)
        override_note = (f"Federal withholding uses the legacy flat {fed_override_pct}% "
                         "override (estimate), not the Pub 15-T tables.")

    earnings = []
    if pay_type == "hourly":
        src_note = " (from approved time)" if hours_source == "time_entries" else ""
        earnings.append({"label": f"Hourly — {reg_hours}h × ${cents_to_dollars(rate_cents):.2f}/hr{src_note}",
                         "amount_cents": wage_cents})
        if ot_hours:
            earnings.append({"label": f"Overtime — {ot_hours}h × ${cents_to_dollars(rate_cents) * 1.5:.2f}/hr",
                             "amount_cents": ot_cents})
    else:
        earnings.append({"label": f"Salary ({frequency})", "amount_cents": wage_cents})
    if commission_cents:
        earnings.append({"label": "Commission", "amount_cents": commission_cents})
    if tips_cents:
        earnings.append({"label": "Tips (reported)", "amount_cents": tips_cents})

    warnings = list(calc.get("warnings", []))
    if ot_note:
        warnings.append(ot_note)
    if override_note:
        warnings.append(override_note)
    if inp.user_id and not member.get("w4_on_file"):
        warnings.append("No W-4 on file for this employee — withholding used the "
                        "single / no-adjustments default the IRS prescribes.")

    return {
        "id": new_id(), "user_id": inp.user_id, "name": name, "pay_type": pay_type,
        "hours": hours, "regular_hours": reg_hours, "overtime_hours": ot_hours,
        "hours_source": hours_source, "time_entry_ids": time_entry_ids or [],
        "rate_cents": rate_cents, "commission_cents": commission_cents,
        "tips_cents": tips_cents, "department": member.get("department") or "",
        "work_state": profile["work_state"], "filing_status": profile["filing_status"],
        "earnings": earnings, "gross_cents": calc["gross_cents"],
        "fit_wages_cents": calc["fit_wages_cents"],
        "fica_wages_cents": calc["fica_wages_cents"],
        "deductions": calc["deductions"], "taxes": calc["taxes"],
        "total_deductions_cents": calc["total_deductions_cents"],
        "net_cents": calc["net_cents"],
        "federal_withholding_cents": calc["federal_withholding_cents"],
        "employee_fica_cents": calc["employee_fica_cents"],
        "state_withholding_cents": calc["state_withholding_cents"],
        "state_withholding_status": calc["state_withholding_status"],
        "employer": calc["employer"],
        "employer_fica_cents": calc["employer_fica_cents"],
        "employer_cost_cents": calc["employer_cost_cents"],
        "ytd_before": calc["ytd_before"], "ytd_after": calc["ytd_after"],
        "warnings": warnings, "config_version": calc["config_version"],
    }


def _apply_fed_override(calc: dict, override_cents: int, pct: float) -> dict:
    """Swap the engine's federal withholding for a legacy flat-% estimate,
    re-deriving downstream totals."""
    calc = dict(calc)
    old = calc["federal_withholding_cents"]
    delta = override_cents - old
    taxes = []
    for t in calc["taxes"]:
        t = dict(t)
        if t["code"] == "federal_withholding":
            t["amount_cents"] = override_cents
            t["label"] = "Federal withholding (est. — flat % override)"
            t["workpaper"] = {"method": "legacy_flat_pct_override", "pct": pct}
        taxes.append(t)
    deductions = []
    for d in calc["deductions"]:
        d = dict(d)
        if d.get("code") == "federal_withholding":
            d["amount_cents"] = override_cents
            d["label"] = "Federal withholding (est. — flat % override)"
        deductions.append(d)
    calc["taxes"] = taxes
    calc["deductions"] = deductions
    calc["federal_withholding_cents"] = override_cents
    calc["total_deductions_cents"] += delta
    calc["net_cents"] -= delta
    if calc["net_cents"] < 0:
        raise HTTPException(status_code=422, detail="Deductions exceed gross pay")
    calc["ytd_after"] = dict(calc["ytd_after"])
    calc["ytd_after"]["federal_withholding_cents"] = \
        calc["ytd_after"].get("federal_withholding_cents", 0) + delta
    calc["ytd_after"]["net_cents"] = calc["ytd_after"].get("net_cents", 0) - delta
    return calc


def _totals(items):
    gross = sum(i["gross_cents"] for i in items)
    fed = sum(i.get("federal_withholding_cents", 0) for i in items)
    fica_emp = sum(i.get("employee_fica_cents", 0) for i in items)
    state_wh = sum(i.get("state_withholding_cents", 0) for i in items)
    booth = sum(d["amount_cents"] for i in items for d in i["deductions"]
                if d.get("category") == "booth_rent")
    tax_total = fed + fica_emp + state_wh
    other = sum(i["total_deductions_cents"] for i in items) - tax_total - booth
    net = sum(i["net_cents"] for i in items)
    employer_fica = sum(i.get("employer_fica_cents", 0) for i in items)
    employer_futa = sum(i["employer"]["futa_cents"] for i in items if i.get("employer"))
    employer_suta = sum(i["employer"]["suta_cents"] for i in items if i.get("employer"))
    employer_ett = sum(i["employer"].get("ett_cents", 0) for i in items if i.get("employer"))
    return {
        "gross_cents": gross,
        "federal_withholding_cents": fed, "employee_fica_cents": fica_emp,
        "state_withholding_cents": state_wh,
        "tax_deductions_cents": tax_total, "booth_rent_cents": booth,
        "other_deductions_cents": other, "net_cents": net,
        "employer_tax_cents": employer_fica,  # back-compat key (employer FICA)
        "employer_futa_cents": employer_futa, "employer_suta_cents": employer_suta,
        "employer_ett_cents": employer_ett,
        "employer_cost_cents": gross + employer_fica + employer_futa + employer_suta + employer_ett,
        "federal_deposit_cents": fed + fica_emp + employer_fica,
    }


@router.get("/runs")
async def list_runs(user: dict = Depends(require("payroll:read"))):
    return await runs(user).find_list(sort=[("created_at", -1)], limit=200)


@router.post("/runs")
async def create_run(body: RunIn, user: dict = Depends(require("payroll:write"))):
    frequency = "monthly"
    schedule_name = None
    if body.schedule_id:
        sched = await schedules(user).find_one({"id": body.schedule_id})
        if not sched:
            raise HTTPException(status_code=404, detail="Schedule not found")
        frequency = sched["frequency"]
        schedule_name = sched["name"]

    members = await _member_map(user)
    default_fed = await _default_fed_pct(user)
    suta_rates = await _tenant_suta_rates_decimal(user)
    pay_date = body.pay_date or body.period_end
    tax_year = int(pay_date[:4])

    raw_items = body.items
    if not raw_items:
        raw_items = [RunItemIn(user_id=m["id"]) for m in members.values()
                     if (m["hourly_rate_cents"] or 0) > 0 or (m["salary_monthly_cents"] or 0) > 0]
    if not raw_items:
        raise HTTPException(status_code=422, detail="No pay items — add pay settings for team members first")

    items = []
    for inp in raw_items:
        if inp.user_id and inp.user_id not in members and not inp.name:
            raise HTTPException(status_code=404, detail=f"Team member {inp.user_id} not found")
        member = members.get(inp.user_id)
        hours, hours_source, entry_ids = await _resolve_hours(
            user, inp, member, body.period_start, body.period_end)
        weekly = inp.weekly_hours
        if weekly is None and hours_source == "time_entries" and inp.user_id:
            entries = await _fetch_period_entries(user, inp.user_id,
                                                  body.period_start, body.period_end)
            weekly = _weekly_split_from_entries(entries, inp.user_id)
        resolved = inp.model_copy(update={"hours": hours})
        ytd = await _ytd_for(user, inp.user_id, tax_year) if inp.user_id else {}
        items.append(_build_item(member, frequency, resolved, default_fed,
                                 hours_source=hours_source, time_entry_ids=entry_ids,
                                 ytd=ytd, weekly_hours=weekly,
                                 tenant_suta_rates=suta_rates))

    run_id = new_id()
    doc = {
        "id": run_id, "schedule_id": body.schedule_id, "schedule_name": schedule_name,
        "frequency": frequency, "period_start": body.period_start,
        "period_end": body.period_end, "pay_date": pay_date, "tax_year": tax_year,
        "notes": body.notes, "items": items, "totals": _totals(items),
        "status": "draft", "disclaimer": DISCLAIMER,
        "created_by": user.get("email"), "created_at": now_iso(),
    }
    await runs(user).insert_one(doc)
    await L.audit(db, user["tenant_id"], user, "payroll.run.create",
                  {"run_id": run_id, "net_cents": doc["totals"]["net_cents"]})
    doc.pop("_id", None)
    return doc


@router.get("/runs/{rid}")
async def get_run(rid: str, user: dict = Depends(require("payroll:read"))):
    doc = await runs(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(status_code=404, detail="Pay run not found")
    return doc


@router.post("/runs/{rid}/approve")
async def approve_run(rid: str, user: dict = Depends(require("payroll:write"))):
    doc = await runs(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(status_code=404, detail="Pay run not found")
    if doc["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"Run is {doc['status']}")
    await runs(user).update_one({"id": rid}, {"$set": {
        "status": "approved", "approved_by": user.get("email"), "approved_at": now_iso()}})
    await L.audit(db, user["tenant_id"], user, "payroll.run.approve", {"run_id": rid})
    return await runs(user).find_one({"id": rid})


async def _ensure_payroll_accounts(user):
    """6110/6120/2120 are payroll COA accounts — upsert them for existing tenants."""
    s = Scoped(db.accounts, user["tenant_id"])
    for code, name, atype, normal in (
            ("6110", "Wages & Salaries", "expense", "debit"),
            ("6120", "Payroll Tax Expense", "expense", "debit"),
            ("2120", "Payroll Liabilities", "liability", "credit")):
        if not await s.find_one({"code": code}):
            await s.insert_one({
                "id": new_id(), "code": code, "name": name, "type": atype,
                "normal_balance": normal, "parent_code": None, "is_system": False,
                "archived": False, "description": "Auto-created by Payroll",
                "created_at": now_iso(),
            })


# ------------------------------------------------------- deposit schedule

def _next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _federal_due_date(pay_date: date, schedule: str, deposit_cents: int) -> date:
    """IRS deposit due date. The $100,000 next-day rule wins over everything."""
    if deposit_cents >= 100_000_00:
        return _next_business_day(pay_date + timedelta(days=1))
    if schedule == "semiweekly":
        wd = pay_date.weekday()  # Mon=0
        if wd in (2, 3, 4):      # Wed/Thu/Fri payday -> following Wednesday
            delta = (2 - wd) % 7 or 7
            return pay_date + timedelta(days=delta)
        # Sat/Sun/Mon/Tue payday -> following Friday
        delta = (4 - wd) % 7 or 7
        return pay_date + timedelta(days=delta)
    # monthly: 15th of the following month
    y, m = (pay_date.year + 1, 1) if pay_date.month == 12 else (pay_date.year, pay_date.month + 1)
    return _next_business_day(date(y, m, 15))


def _quarter_due_date(year: int, quarter: int) -> date:
    """Last day of the month after quarter end (941/FUTA/NYS-45/DE 9 pattern)."""
    return {1: date(year, 4, 30), 2: date(year, 7, 31),
            3: date(year, 10, 31), 4: date(year + 1, 1, 31)}[quarter]


def _quarter_of(d: date):
    return (d.year, (d.month - 1) // 3 + 1)


async def _deposit_schedule(user, pay_date: date) -> dict:
    """Federal deposit schedule from the Reg. 31.6302 lookback period: the
    four quarters ending June 30 of the prior year. <= $50,000 of federal
    deposit liability -> monthly; more -> semiweekly. No history (new
    employer) -> monthly, which is the IRS default for new employers."""
    lb_start = date(pay_date.year - 2, 7, 1)
    lb_end = date(pay_date.year - 1, 6, 30)
    recs = await runs(user).find_list(
        {"status": "posted", "pay_date": {"$gte": lb_start.isoformat(),
                                          "$lte": lb_end.isoformat()}}, limit=2000)
    total = sum((r.get("totals") or {}).get("federal_deposit_cents", 0) for r in recs)
    schedule = "semiweekly" if total > 50_000_00 else "monthly"
    return {"schedule": schedule, "lookback_total_cents": total,
            "lookback_window": [lb_start.isoformat(), lb_end.isoformat()],
            "basis": "lookback" if recs else "new_employer_default_monthly"}


async def _create_liabilities(user, run: dict) -> list:
    """Materialize a posted run's obligations into payroll_liabilities —
    dated, agency-tagged amounts the business (or a future executor) remits.
    This app never remits them; it tracks them."""
    t = run["totals"]
    pay_date = date.fromisoformat(run.get("pay_date") or run["period_end"])
    year, quarter = _quarter_of(pay_date)
    sched = await _deposit_schedule(user, pay_date)
    docs = []

    def add(agency, kind, amount, due, note=""):
        if amount:
            docs.append({
                "id": new_id(), "run_id": run["id"], "agency": agency, "kind": kind,
                "amount_cents": amount, "pay_date": pay_date.isoformat(),
                "due_date": due.isoformat(), "tax_year": year, "quarter": quarter,
                "period_start": run["period_start"], "period_end": run["period_end"],
                "status": "accrued", "note": note, "created_at": now_iso(),
            })

    fed_due = _federal_due_date(pay_date, sched["schedule"], t["federal_deposit_cents"])
    add("IRS", "federal_deposit", t["federal_deposit_cents"], fed_due,
        f"Form 941 deposit ({sched['schedule']} schedule; includes employee "
        "withholding + both FICA shares).")
    add("IRS", "futa", t["employer_futa_cents"], _quarter_due_date(year, quarter),
        "FUTA (net of the 5.4% state credit). Deposit is required only when "
        "the quarter's cumulative FUTA exceeds $500; otherwise it carries.")

    # state amounts grouped from items
    wh_by_state, suta_by_state, ett_by_state = {}, {}, {}
    for i in run["items"]:
        st = i.get("work_state")
        if not st:
            continue
        if i.get("state_withholding_cents"):
            wh_by_state[st] = wh_by_state.get(st, 0) + i["state_withholding_cents"]
        suta_amt = (i.get("employer") or {}).get("suta_cents", 0)
        if suta_amt:
            suta_by_state[st] = suta_by_state.get(st, 0) + suta_amt
        ett_amt = (i.get("employer") or {}).get("ett_cents", 0)
        if ett_amt:
            ett_by_state[st] = ett_by_state.get(st, 0) + ett_amt
    for st, amt in wh_by_state.items():
        if st == "CA":
            # CA PIT deposits follow the same schedule class as federal.
            due = _federal_due_date(pay_date, sched["schedule"], amt)
            add("CA", "state_withholding", amt, due,
                "CA PIT withholding deposit (EDD) — schedule mirrors the federal class.")
        else:
            add(st, "state_withholding", amt, _quarter_due_date(year, quarter),
                f"{st} withholding — quarterly return due date (e.g. NYS-45 for NY).")
    for st, amt in suta_by_state.items():
        add(st, "suta", amt, _quarter_due_date(year, quarter),
            f"{st} unemployment insurance — quarterly wage report due date.")
    for st, amt in ett_by_state.items():
        add(st, "ett", amt, _quarter_due_date(year, quarter),
            f"{st} Employment Training Tax — filed with the quarterly UI return.")
    for d in docs:
        await liabilities(user).insert_one(d)
    return docs


@router.post("/runs/{rid}/post")
async def post_run(rid: str, user: dict = Depends(require("payroll:write"))):
    """Finalize the run: post the journal entry, write IMMUTABLE pay records,
    and materialize its tax liabilities with due dates. Disbursement still
    happens outside the app — net pay lands in Wages Payable (2320)."""
    doc = await runs(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(status_code=404, detail="Pay run not found")
    if doc["status"] != "approved":
        raise HTTPException(status_code=409, detail="Approve the run before posting")
    t = doc["totals"]
    if t["gross_cents"] <= 0:
        raise HTTPException(status_code=422, detail="Run has no gross pay to post")

    await _ensure_payroll_accounts(user)
    employer_taxes = (t["employer_tax_cents"] + t["employer_futa_cents"]
                      + t["employer_suta_cents"] + t.get("employer_ett_cents", 0))
    held = t["other_deductions_cents"]  # pre-tax held + post-tax + garnishments
    jl = [P.dr("6110", t["gross_cents"], "Gross wages")]
    if employer_taxes:
        jl.append(P.dr("6120", employer_taxes, "Employer payroll taxes (FICA/FUTA/SUTA)"))
    if t["tax_deductions_cents"]:
        jl.append(P.cr("2120", t["tax_deductions_cents"], "Employee taxes withheld (fed/FICA/state)"))
    if employer_taxes:
        jl.append(P.cr("2120", employer_taxes, "Employer payroll tax liability"))
    if t["booth_rent_cents"]:
        jl.append(P.cr("4200", t["booth_rent_cents"], "Booth rent collected"))
    if held:
        jl.append(P.cr("2120", held, "Deductions held (pre-tax plans, garnishments, other)"))
    jl.append(P.cr("2320", t["net_cents"], "Net pay — Wages Payable"))

    pay_date = doc.get("pay_date") or doc["period_end"]
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": pay_date, "source": "payroll", "source_ref": rid,
        "memo": f"Payroll {doc['period_start']}–{doc['period_end']}",
        "lines": jl,
    }, status="posted")

    # Immutable per-employee pay records — the executor contract.
    for i in doc["items"]:
        await pay_records(user).insert_one({
            "id": new_id(), "run_id": rid, "user_id": i.get("user_id"),
            "name": i["name"], "pay_type": i["pay_type"],
            "work_state": i.get("work_state"),
            "period_start": doc["period_start"], "period_end": doc["period_end"],
            "pay_date": pay_date, "tax_year": doc.get("tax_year") or int(pay_date[:4]),
            "gross_cents": i["gross_cents"], "fit_wages_cents": i.get("fit_wages_cents"),
            "fica_wages_cents": i.get("fica_wages_cents"),
            "federal_withholding_cents": i.get("federal_withholding_cents", 0),
            "employee_fica_cents": i.get("employee_fica_cents", 0),
            "state_withholding_cents": i.get("state_withholding_cents", 0),
            "tips_cents": i.get("tips_cents", 0),
            "net_cents": i["net_cents"],
            "item": i, "entry_number": entry["number"],
            "immutable": True, "reversed": False, "finalized_at": now_iso(),
        })
    liabs = await _create_liabilities(user, doc)
    await runs(user).update_one({"id": rid}, {"$set": {
        "status": "posted", "entry_number": entry["number"],
        "journal_lines": jl, "liability_ids": [d["id"] for d in liabs],
        "posted_by": user.get("email"), "posted_at": now_iso()}})
    await L.audit(db, user["tenant_id"], user, "payroll.run.post",
                  {"run_id": rid, "entry": entry["number"],
                   "liabilities": len(liabs)})
    return await runs(user).find_one({"id": rid})


@router.post("/runs/{rid}/reverse")
async def reverse_run(rid: str, user: dict = Depends(require("payroll:write"))):
    """Reverse a posted run without editing it: offsetting journal entry,
    liabilities marked reversed, pay records flagged reversed (kept)."""
    doc = await runs(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(status_code=404, detail="Pay run not found")
    if doc["status"] != "posted":
        raise HTTPException(status_code=409, detail="Only posted runs can be reversed")
    lines = doc.get("journal_lines") or []
    if not lines:
        raise HTTPException(status_code=409, detail="Run predates journal-line snapshots; reverse it manually in the journal")
    rev_lines = [{"account_code": l["account_code"], "debit": l.get("credit", 0),
                  "credit": l.get("debit", 0), "memo": f"Reversal: {l.get('memo') or ''}"}
                 for l in lines]
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "payroll_reversal", "source_ref": rid,
        "memo": f"Reversal of payroll {doc['period_start']}–{doc['period_end']} ({doc.get('entry_number')})",
        "lines": rev_lines,
    }, status="posted")
    for lid in doc.get("liability_ids", []):
        await liabilities(user).update_one({"id": lid}, {"$set": {
            "status": "reversed", "reversed_at": now_iso()}})
    recs = await pay_records(user).find_list({"run_id": rid}, limit=2000)
    for r in recs:
        await pay_records(user).update_one({"id": r["id"]}, {"$set": {
            "reversed": True, "reversed_at": now_iso()}})
    await runs(user).update_one({"id": rid}, {"$set": {
        "status": "reversed", "reversed_by": user.get("email"),
        "reversed_at": now_iso(), "reversal_entry_number": entry["number"]}})
    await L.audit(db, user["tenant_id"], user, "payroll.run.reverse",
                  {"run_id": rid, "entry": entry["number"]})
    return await runs(user).find_one({"id": rid})


@router.delete("/runs/{rid}")
async def delete_run(rid: str, user: dict = Depends(require("payroll:write"))):
    doc = await runs(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(status_code=404, detail="Pay run not found")
    if doc["status"] != "draft":
        raise HTTPException(status_code=409, detail="Only draft runs can be deleted")
    await runs(user).c.delete_one({"tenant_id": user["tenant_id"], "id": rid})
    await L.audit(db, user["tenant_id"], user, "payroll.run.delete", {"run_id": rid})
    return {"ok": True}


# ---------------------------------------------------------------- liabilities

@router.get("/liabilities")
async def list_liabilities(user: dict = Depends(require("payroll:read")),
                           status: Optional[str] = None):
    q = {"status": status} if status else {}
    rows = await liabilities(user).find_list(q, sort=[("due_date", 1)], limit=2000)
    accrued = [r for r in rows if r["status"] == "accrued"]
    summary = {}
    for r in accrued:
        key = f"{r['agency']}:{r['kind']}"
        s = summary.setdefault(key, {"agency": r["agency"], "kind": r["kind"],
                                     "amount_cents": 0, "count": 0,
                                     "next_due_date": r["due_date"]})
        s["amount_cents"] += r["amount_cents"]
        s["count"] += 1
        s["next_due_date"] = min(s["next_due_date"], r["due_date"])
    return {"liabilities": rows,
            "accrued_summary": sorted(summary.values(), key=lambda s: s["next_due_date"]),
            "accrued_total_cents": sum(r["amount_cents"] for r in accrued),
            "note": "Amounts this business owes each agency, tracked from finalized "
                    "pay runs. This app does not remit them — pay via the agency "
                    "(EFTPS / EDD / NYS) and mark each row remitted."}


class LiabilityPatchIn(BaseModel):
    status: str = Field(pattern=r"^(remitted)$")
    reference: Optional[str] = Field(default=None, max_length=80)  # EFTPS ack / check no.
    remitted_date: Optional[str] = Field(default=None, min_length=10, max_length=10)


@router.patch("/liabilities/{lid}")
async def patch_liability(lid: str, body: LiabilityPatchIn,
                          user: dict = Depends(require("payroll:write"))):
    doc = await liabilities(user).find_one({"id": lid})
    if not doc:
        raise HTTPException(status_code=404, detail="Liability not found")
    if doc["status"] != "accrued":
        raise HTTPException(status_code=409, detail=f"Liability is {doc['status']}")
    await liabilities(user).update_one({"id": lid}, {"$set": {
        "status": "remitted", "remitted_at": now_iso(),
        "remitted_date": body.remitted_date or today_str(),
        "reference": body.reference, "remitted_by": user.get("email")}})
    await L.audit(db, user["tenant_id"], user, "payroll.liability.remit",
                  {"liability_id": lid, "amount_cents": doc["amount_cents"]})
    return await liabilities(user).find_one({"id": lid})


@router.get("/pay-records")
async def list_pay_records(user: dict = Depends(require("payroll:read")),
                           user_id: Optional[str] = None, year: Optional[int] = None):
    q = {}
    if user_id:
        q["user_id"] = user_id
    if year:
        q["tax_year"] = year
    return await pay_records(user).find_list(q, sort=[("pay_date", -1)], limit=2000)


# ---------------------------------------------------------------- workpapers

def _ss_med_split(item: dict):
    ss = med = addl = 0
    for t in item.get("taxes", []):
        if t["code"] == "social_security":
            ss = t["amount_cents"]
        elif t["code"] == "medicare":
            med = t["amount_cents"]
        elif t["code"] == "additional_medicare":
            addl = t["amount_cents"]
    wages = {t["code"]: t.get("wages_cents", 0) for t in item.get("taxes", [])}
    return ss, med, addl, wages


@router.get("/reports/form-941")
async def form_941_workpaper(year: int, quarter: int,
                             user: dict = Depends(require("payroll:read"))):
    """Workpaper for Form 941 (quarterly): the accumulations the return
    needs, from finalized pay records. NOT a filing — numbers to hand to
    whoever files (owner ruling: estimates/prep yes, filing no)."""
    recs = await pay_records(user).find_list(
        {"tax_year": year, "reversed": {"$ne": True}}, limit=10000)
    per_emp: Dict[str, dict] = {}
    for r in recs:
        pd = date.fromisoformat(r["pay_date"])
        if _quarter_of(pd) != (year, quarter):
            continue
        it = r["item"]
        ss, med, addl, wages = _ss_med_split(it)
        e = per_emp.setdefault(r["user_id"] or r["name"], {
            "user_id": r.get("user_id"), "name": r["name"], "checks": 0,
            "fit_wages_cents": 0, "federal_withholding_cents": 0,
            "ss_wages_cents": 0, "ss_tax_cents": 0, "medicare_wages_cents": 0,
            "medicare_tax_cents": 0, "addl_medicare_wages_cents": 0,
            "addl_medicare_tax_cents": 0, "tips_cents": 0})
        e["checks"] += 1
        e["fit_wages_cents"] += it.get("fit_wages_cents") or 0
        e["federal_withholding_cents"] += it.get("federal_withholding_cents", 0)
        e["ss_wages_cents"] += wages.get("social_security", 0)
        e["ss_tax_cents"] += ss
        e["medicare_wages_cents"] += wages.get("medicare", 0)
        e["medicare_tax_cents"] += med
        e["addl_medicare_wages_cents"] += wages.get("additional_medicare", 0)
        e["addl_medicare_tax_cents"] += addl
        e["tips_cents"] += it.get("tips_cents", 0)
    employees = sorted(per_emp.values(), key=lambda e: e["name"])
    totals = {k: sum(e[k] for e in employees) for k in
              ("fit_wages_cents", "federal_withholding_cents", "ss_wages_cents",
               "ss_tax_cents", "medicare_wages_cents", "medicare_tax_cents",
               "addl_medicare_wages_cents", "addl_medicare_tax_cents", "tips_cents")}
    totals["employee_count"] = len(employees)
    return {"form": "941-workpaper", "year": year, "quarter": quarter,
            "employees": employees, "totals": totals,
            "employer_note": "941 tax lines combine employee + employer shares: "
                             "the SS and Medicare tax columns here are the "
                             "employee share; double them for the combined line.",
            "disclaimer": "Workpaper only — accumulations from finalized pay runs. "
                          "This app does not file Form 941."}


@router.get("/reports/form-940")
async def form_940_workpaper(year: int, user: dict = Depends(require("payroll:read"))):
    """Workpaper for Form 940 (annual FUTA): per-employee FUTA wages/tax."""
    recs = await pay_records(user).find_list(
        {"tax_year": year, "reversed": {"$ne": True}}, limit=10000)
    per_emp: Dict[str, dict] = {}
    for r in sorted(recs, key=lambda x: x["pay_date"]):
        it = r["item"]
        emp = it.get("employer") or {}
        e = per_emp.setdefault(r["user_id"] or r["name"], {
            "user_id": r.get("user_id"), "name": r["name"], "total_wages_cents": 0,
            "futa_wages_cents": 0, "futa_gross_cents": 0, "futa_credit_cents": 0,
            "futa_net_cents": 0})
        e["total_wages_cents"] += it["gross_cents"]
        e["futa_wages_cents"] += emp.get("futa_taxable_wages_cents", 0)
        e["futa_gross_cents"] += emp.get("futa_gross_cents", 0)
        e["futa_credit_cents"] += emp.get("futa_credit_cents", 0)
        e["futa_net_cents"] += emp.get("futa_cents", 0)
    employees = sorted(per_emp.values(), key=lambda e: e["name"])
    return {"form": "940-workpaper", "year": year, "employees": employees,
            "totals": {k: sum(e[k] for e in employees) for k in
                       ("total_wages_cents", "futa_wages_cents", "futa_gross_cents",
                        "futa_credit_cents", "futa_net_cents")},
            "note": "Excess wages above the $7,000 FUTA base per employee = total "
                    "wages - FUTA wages. The 5.4% credit assumes state unemployment "
                    "taxes were paid on time.",
            "disclaimer": "Workpaper only — this app does not file Form 940."}


@router.get("/reports/w2-workpaper")
async def w2_workpaper(year: int, user: dict = Depends(require("payroll:read"))):
    """Workpaper for W-2s (annual, per employee): Boxes 1-6 + state boxes."""
    recs = await pay_records(user).find_list(
        {"tax_year": year, "reversed": {"$ne": True}}, limit=10000)
    per_emp: Dict[str, dict] = {}
    for r in recs:
        it = r["item"]
        ss, med, addl, wages = _ss_med_split(it)
        e = per_emp.setdefault(r["user_id"] or r["name"], {
            "user_id": r.get("user_id"), "name": r["name"],
            "box1_wages_cents": 0, "box2_fed_withholding_cents": 0,
            "box3_ss_wages_cents": 0, "box4_ss_tax_cents": 0,
            "box5_medicare_wages_cents": 0, "box6_medicare_tax_cents": 0,
            "box7_tips_cents": 0, "states": {}})
        e["box1_wages_cents"] += it.get("fit_wages_cents") or 0
        e["box2_fed_withholding_cents"] += it.get("federal_withholding_cents", 0)
        e["box3_ss_wages_cents"] += wages.get("social_security", 0)
        e["box4_ss_tax_cents"] += ss
        e["box5_medicare_wages_cents"] += wages.get("medicare", 0)
        e["box6_medicare_tax_cents"] += med + addl
        e["box7_tips_cents"] += it.get("tips_cents", 0)
        st = it.get("work_state")
        if st:
            srow = e["states"].setdefault(st, {"state": st, "wages_cents": 0,
                                               "withholding_cents": 0})
            st_wh = next((t for t in it.get("taxes", [])
                          if t["code"] == "state_withholding"), None)
            srow["wages_cents"] += (st_wh or {}).get("wages_cents") or it["gross_cents"]
            srow["withholding_cents"] += it.get("state_withholding_cents", 0)
    employees = sorted(per_emp.values(), key=lambda e: e["name"])
    for e in employees:
        e["states"] = sorted(e["states"].values(), key=lambda s: s["state"])
    return {"form": "w2-workpaper", "year": year, "employees": employees,
            "disclaimer": "Workpaper only — accumulations for whoever prepares "
                          "the actual W-2s. This app does not file W-2s."}


# ---------------------------------------------------------------- printable payslip

@router.get("/runs/{rid}/payslips/{item_id}")
async def get_payslip(rid: str, item_id: str, user: dict = Depends(require("payroll:read"))):
    """JSON shaped for printing — the UI renders the print-friendly view."""
    doc = await runs(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(status_code=404, detail="Pay run not found")
    item = next((i for i in doc.get("items", []) if i["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Payslip not found")
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0, "name": 1})
    employer = item.get("employer") or {}
    return {
        "company_name": (tenant or {}).get("name", "Shop"),
        "run_id": rid, "status": doc["status"],
        "period_start": doc["period_start"], "period_end": doc["period_end"],
        "pay_date": doc.get("pay_date") or doc["period_end"],
        "employee": {"name": item["name"], "user_id": item.get("user_id"),
                     "pay_type": item["pay_type"], "hours": item.get("hours", 0),
                     "regular_hours": item.get("regular_hours", 0),
                     "overtime_hours": item.get("overtime_hours", 0),
                     "rate_cents": item.get("rate_cents", 0),
                     "work_state": item.get("work_state"),
                     "department": item.get("department")},
        "earnings": item.get("earnings", []),
        "gross_cents": item["gross_cents"],
        "deductions": item.get("deductions", []),
        "total_deductions_cents": item["total_deductions_cents"],
        "net_cents": item["net_cents"],
        "ytd": item.get("ytd_after"),
        "employer_cost_cents": item.get("employer_cost_cents"),
        "employer_taxes": {
            "fica_cents": employer.get("fica_total_cents", 0),
            "futa_cents": employer.get("futa_cents", 0),
            "suta_cents": employer.get("suta_cents", 0),
            "ett_cents": employer.get("ett_cents", 0),
            "suta_status": (employer.get("suta") or {}).get("status"),
        },
        "warnings": item.get("warnings", []),
        "entry_number": doc.get("entry_number"),
        "disclaimer": doc.get("disclaimer") or DISCLAIMER,
    }
