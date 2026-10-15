"""Payroll-lite: pay schedules, gross-to-net pay runs, printable payslips, journal posting.

DISCLAIMER (also shown in the UI):
- Estimates only. This is NOT tax filing and NOT direct deposit. Deduction lines
  labeled "withholding" are planning estimates, not filed taxes.
- For compliant payroll (actual withholding, IRS/state filing, direct deposit),
  use a payroll provider such as Gusto or Check.

Journal posting for an approved run (all amounts in cents):
    Dr 6110 Wages & Salaries          (gross)
        Cr 2120 Payroll Liabilities   (federal withholding est. + FICA est.)
        Cr 4200 Chair Rental Income   (booth rent collected by the shop)
        Cr 2320 Wages Payable         (net pay owed to staff — disbursement
                                      happens OUTSIDE this app via a provider)
"""
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/payroll", tags=["payroll"])

# NOTE: "payroll:read" / "payroll:write" are NEW permissions — they must be added
# to the PERMISSIONS map in backend/lib/auth.py (see MANIFEST.md). Suggested:
#   "payroll:read": {"owner", "accountant"},
#   "payroll:write": {"owner"},

DISCLAIMER = (
    "Payroll-lite computes pay estimates only. It does not withhold or file taxes, "
    "and it does not run direct deposit. For compliant tax filing and payments, use "
    "a payroll provider (e.g. Gusto, Check)."
)

FICA_PCT = 7.65  # Social Security + Medicare (employee share), planning estimate
PERIODS_PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12}
TEAM_ROLES = ["owner", "barber"]  # roles treated as payroll team members


def schedules(user):
    return Scoped(db.pay_schedules, user["tenant_id"])


def runs(user):
    return Scoped(db.payroll_runs, user["tenant_id"])


def _pct_cents(cents: int, pct: float) -> int:
    """pct of cents, rounded half-up to whole cents."""
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


# ---------------------------------------------------------------- team pay settings

def _team_member(u):
    ps = u.get("pay_settings", {}) or {}
    return {
        "id": u["id"], "name": u.get("name"), "email": u.get("email"), "role": u.get("role"),
        "commission_rate": u.get("commission_rate", 0) or 0,
        "hourly_rate_cents": ps.get("hourly_rate_cents", 0) or 0,
        "salary_monthly_cents": ps.get("salary_monthly_cents", 0) or 0,
        "fed_withholding_pct": ps.get("fed_withholding_pct", 0) or 0,
        "booth_rent_cents": ps.get("booth_rent_cents", 0) or 0,
    }


@router.get("/team")
async def list_team(user: dict = Depends(require("payroll:read"))):
    """Active team members with their pay settings (hourly rate / monthly salary /
    federal withholding % / booth rent)."""
    rows = await db.users.find(
        {"tenant_id": user["tenant_id"], "role": {"$in": TEAM_ROLES}},
        {"_id": 0, "password_hash": 0},
    ).sort("name", 1).to_list(200)
    return [_team_member(r) for r in rows]


class PaySettingsIn(BaseModel):
    hourly_rate: Optional[float] = Field(default=None, ge=0, le=10000)       # $/hr
    salary_monthly: Optional[float] = Field(default=None, ge=0, le=1000000)  # $/month
    fed_withholding_pct: Optional[float] = Field(default=None, ge=0, le=60)  # % of gross (estimate)
    booth_rent: Optional[float] = Field(default=None, ge=0, le=100000)       # $ flat per run


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
    if patch:
        await db.users.update_one({"tenant_id": user["tenant_id"], "id": user_id}, {"$set": patch})
        await L.audit(db, user["tenant_id"], user, "payroll.pay_settings.update",
                      {"user_id": user_id, **patch})
    doc = await db.users.find_one({"tenant_id": user["tenant_id"], "id": user_id}, {"_id": 0, "password_hash": 0})
    return _team_member(doc)


class PayrollSettingsIn(BaseModel):
    default_fed_withholding_pct: Optional[float] = Field(default=None, ge=0, le=60)


@router.get("/settings")
async def get_payroll_settings(user: dict = Depends(require("payroll:read"))):
    doc = await db.payroll_settings.find_one({"tenant_id": user["tenant_id"]}, {"_id": 0})
    return {
        "default_fed_withholding_pct": (doc or {}).get("default_fed_withholding_pct", 0),
        "fica_pct": FICA_PCT,
    }


@router.patch("/settings")
async def update_payroll_settings(body: PayrollSettingsIn, user: dict = Depends(require("payroll:write"))):
    await db.payroll_settings.update_one(
        {"tenant_id": user["tenant_id"]},
        {"$set": {"default_fed_withholding_pct": body.default_fed_withholding_pct,
                  "updated_at": now_iso()}},
        upsert=True,
    )
    return await get_payroll_settings(user)


# ---------------------------------------------------------------- pay runs

class DeductionIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern=r"^(percent|amount)$")
    value: float = Field(ge=0)


class RunItemIn(BaseModel):
    user_id: Optional[str] = None
    name: Optional[str] = None
    pay_type: Optional[str] = Field(default=None, pattern=r"^(hourly|salary)$")
    hours: float = Field(default=0, ge=0)
    rate: Optional[float] = Field(default=None, ge=0)          # $/hr override
    amount: Optional[float] = Field(default=None, ge=0)        # salary $ per period override
    commission: float = Field(default=0, ge=0)                 # extra commission $ this period
    fed_withholding_pct: Optional[float] = Field(default=None, ge=0, le=60)
    booth_rent: Optional[float] = Field(default=None, ge=0)    # $ flat override
    deductions: List[DeductionIn] = []                         # manual extras


class RunIn(BaseModel):
    schedule_id: Optional[str] = None
    period_start: str = Field(min_length=10, max_length=10)
    period_end: str = Field(min_length=10, max_length=10)
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


def _build_item(member, frequency, inp: RunItemIn, default_fed_pct):
    member = member or {}
    name = inp.name or member.get("name") or "Unnamed"
    pay_type = inp.pay_type or ("salary" if (member.get("salary_monthly_cents") or 0) > 0 else "hourly")
    rate_cents = to_cents(inp.rate) if inp.rate is not None else (member.get("hourly_rate_cents") or 0)
    if pay_type == "hourly":
        wage_cents = int((Decimal(rate_cents) * Decimal(str(inp.hours)))
                         .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    else:
        if inp.amount is not None:
            wage_cents = to_cents(inp.amount)
        else:
            wage_cents = _salary_period_cents(member.get("salary_monthly_cents") or 0, frequency)
    commission_cents = to_cents(inp.commission)
    gross = wage_cents + commission_cents

    fed_pct = inp.fed_withholding_pct if inp.fed_withholding_pct is not None \
        else (member.get("fed_withholding_pct") or default_fed_pct)
    booth_cents = to_cents(inp.booth_rent) if inp.booth_rent is not None else (member.get("booth_rent_cents") or 0)

    deductions = []
    fed = _pct_cents(gross, fed_pct)
    if fed:
        deductions.append({"label": "Federal withholding (est.)", "kind": "percent",
                           "value": fed_pct, "amount_cents": fed})
    fica = _pct_cents(gross, FICA_PCT)
    if fica:
        deductions.append({"label": "FICA — Social Security & Medicare (est.)", "kind": "percent",
                           "value": FICA_PCT, "amount_cents": fica})
    if booth_cents:
        deductions.append({"label": "Booth rent", "kind": "amount",
                           "value": cents_to_dollars(booth_cents), "amount_cents": booth_cents})
    for d in inp.deductions:
        amt = _pct_cents(gross, d.value) if d.kind == "percent" else to_cents(d.value)
        if amt:
            deductions.append({"label": d.label, "kind": d.kind, "value": d.value, "amount_cents": amt})

    total_ded = sum(d["amount_cents"] for d in deductions)
    if total_ded > gross:
        raise HTTPException(status_code=422, detail=f"Deductions exceed gross pay for {name}")
    net = gross - total_ded

    earnings = []
    if pay_type == "hourly":
        earnings.append({"label": f"Hourly — {inp.hours}h × ${cents_to_dollars(rate_cents):.2f}/hr",
                         "amount_cents": wage_cents})
    else:
        earnings.append({"label": f"Salary ({frequency})", "amount_cents": wage_cents})
    if commission_cents:
        earnings.append({"label": "Commission", "amount_cents": commission_cents})

    return {
        "id": new_id(), "user_id": inp.user_id, "name": name, "pay_type": pay_type,
        "hours": inp.hours, "rate_cents": rate_cents, "commission_cents": commission_cents,
        "earnings": earnings, "gross_cents": gross, "deductions": deductions,
        "total_deductions_cents": total_ded, "net_cents": net,
    }


def _totals(items):
    gross = sum(i["gross_cents"] for i in items)
    tax = sum(d["amount_cents"] for i in items for d in i["deductions"]
              if d["label"] in ("Federal withholding (est.)", "FICA — Social Security & Medicare (est.)"))
    booth = sum(d["amount_cents"] for i in items for d in i["deductions"] if d["label"] == "Booth rent")
    other = sum(d["amount_cents"] for i in items for d in i["deductions"]
                if d["label"] not in ("Federal withholding (est.)",
                                      "FICA — Social Security & Medicare (est.)", "Booth rent"))
    net = sum(i["net_cents"] for i in items)
    return {"gross_cents": gross, "tax_deductions_cents": tax, "booth_rent_cents": booth,
            "other_deductions_cents": other, "net_cents": net}


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

    raw_items = body.items
    if not raw_items:
        # auto-build: team members that have any pay settings configured
        raw_items = [RunItemIn(user_id=m["id"]) for m in members.values()
                     if (m["hourly_rate_cents"] or 0) > 0 or (m["salary_monthly_cents"] or 0) > 0]
    if not raw_items:
        raise HTTPException(status_code=422, detail="No pay items — add pay settings for team members first")

    items = []
    for inp in raw_items:
        if inp.user_id and inp.user_id not in members and not inp.name:
            raise HTTPException(status_code=404, detail=f"Team member {inp.user_id} not found")
        items.append(_build_item(members.get(inp.user_id), frequency, inp, default_fed))

    run_id = new_id()
    doc = {
        "id": run_id, "schedule_id": body.schedule_id, "schedule_name": schedule_name,
        "frequency": frequency, "period_start": body.period_start, "period_end": body.period_end,
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
    """6110/2120 are new COA accounts — upsert them for existing tenants."""
    s = Scoped(db.accounts, user["tenant_id"])
    for code, name, atype, normal in (
            ("6110", "Wages & Salaries", "expense", "debit"),
            ("2120", "Payroll Liabilities", "liability", "credit")):
        if not await s.find_one({"code": code}):
            await s.insert_one({
                "id": new_id(), "code": code, "name": name, "type": atype,
                "normal_balance": normal, "parent_code": None, "is_system": False,
                "archived": False, "description": "Auto-created by Payroll",
                "created_at": now_iso(),
            })


@router.post("/runs/{rid}/post")
async def post_run(rid: str, user: dict = Depends(require("payroll:write"))):
    """Post the run's journal entry (posted status). Disbursement still happens
    outside the app — net pay lands in Wages Payable (2320), not in the bank."""
    doc = await runs(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(status_code=404, detail="Pay run not found")
    if doc["status"] != "approved":
        raise HTTPException(status_code=409, detail="Approve the run before posting")
    t = doc["totals"]
    if t["gross_cents"] <= 0:
        raise HTTPException(status_code=422, detail="Run has no gross pay to post")

    await _ensure_payroll_accounts(user)
    jl = [P.dr("6110", t["gross_cents"], "Gross wages")]
    if t["tax_deductions_cents"]:
        jl.append(P.cr("2120", t["tax_deductions_cents"], "Payroll liabilities (est. withholding + FICA)"))
    if t["booth_rent_cents"]:
        jl.append(P.cr("4200", t["booth_rent_cents"], "Booth rent collected"))
    if t["other_deductions_cents"]:
        jl.append(P.cr("2120", t["other_deductions_cents"], "Other payroll deductions"))
    jl.append(P.cr("2320", t["net_cents"], "Net pay — Wages Payable"))

    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": doc["period_end"] or today_str(), "source": "payroll", "source_ref": rid,
        "memo": f"Payroll {doc['period_start']}–{doc['period_end']}",
        "lines": jl,
    }, status="posted")
    await runs(user).update_one({"id": rid}, {"$set": {
        "status": "posted", "entry_number": entry["number"],
        "posted_by": user.get("email"), "posted_at": now_iso()}})
    await L.audit(db, user["tenant_id"], user, "payroll.run.post",
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
    return {
        "company_name": (tenant or {}).get("name", "Shop"),
        "run_id": rid, "status": doc["status"],
        "period_start": doc["period_start"], "period_end": doc["period_end"],
        "pay_date": doc["period_end"],
        "employee": {"name": item["name"], "user_id": item.get("user_id"),
                     "pay_type": item["pay_type"], "hours": item.get("hours", 0),
                     "rate_cents": item.get("rate_cents", 0)},
        "earnings": item.get("earnings", []),
        "gross_cents": item["gross_cents"],
        "deductions": item.get("deductions", []),
        "total_deductions_cents": item["total_deductions_cents"],
        "net_cents": item["net_cents"],
        "entry_number": doc.get("entry_number"),
        "disclaimer": DISCLAIMER,
    }
