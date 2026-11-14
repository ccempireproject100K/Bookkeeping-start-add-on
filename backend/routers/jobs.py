"""HVAC Job Costing engine — jobs, estimates, cost tracking, change orders, profitability.
Every cost/revenue posts exactly ONE journal entry into the shared double-entry engine (no duplicates).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P
from lib import timeutil as T
from lib import jobalerts as JA
from lib import jobcosting as JC
from lib import jobphases as JP
from lib import retainage as R

router = APIRouter(prefix="/jobs", tags=["jobs"])

CATEGORY_ACCOUNT = {
    "material": "5000", "labor": "5005", "equipment": "5006",
    "subcontractor": "5007", "other": "5008",
}
CATEGORIES = list(CATEGORY_ACCOUNT.keys())


def jobs(user):
    return Scoped(db.jobs, user["tenant_id"])


def jobcosts(user):
    return Scoped(db.job_costs, user["tenant_id"])


# ---------------- Change orders ----------------
# Workflow (owner-approved 2026-11-14, job-costing requests item #6):
# a change order is born PENDING and never touches the job budget until
# it is APPROVED — approval revises the estimate the same day, by
# category. REJECTED keeps the record, budget untouched. Legacy change
# orders written before the workflow have no "state" field: they are
# treated as approved iff their legacy "approved" flag is truthy
# (exactly how the profit math read them before), so existing data
# behaves identically.

CO_STATES = ("pending", "approved", "rejected")


def co_state(co: dict) -> str:
    """Effective workflow state of a change order (legacy-safe)."""
    st = co.get("state")
    if st in CO_STATES:
        return st
    return "approved" if co.get("approved") else "pending"


def co_cost_delta_cents(co: dict) -> int:
    """Total cost delta: the per-category impact when one was recorded,
    otherwise the legacy single lump delta."""
    impact = co.get("cost_impact_cents") or {}
    if impact:
        return sum(impact.get(c, 0) for c in CATEGORIES)
    return co.get("cost_delta_cents", 0)


def public_co(co: dict) -> dict:
    """Change order as the API serves it: state always explicit."""
    out = {k: v for k, v in co.items()}
    out["state"] = co_state(co)
    out["approved"] = out["state"] == "approved"
    out["cost_delta_cents"] = co_cost_delta_cents(co)
    out.setdefault("reason", "")
    out.setdefault("cost_impact_cents", {})
    out.setdefault("delay_days", 0)
    out.setdefault("history", [])
    return out


def co_summary(job: dict) -> dict:
    """Pending/approved/rejected roll-up for the job screen block."""
    s = {"pending_count": 0, "pending_revenue_cents": 0,
         "pending_cost_cents": 0, "approved_count": 0, "rejected_count": 0}
    for co in job.get("change_orders", []):
        st = co_state(co)
        s[f"{st}_count"] += 1
        if st == "pending":
            s["pending_revenue_cents"] += co.get("revenue_delta_cents", 0)
            s["pending_cost_cents"] += co_cost_delta_cents(co)
    return s


def co_history(job: dict) -> list:
    """Flattened per-job change-order history: every creation and state
    change with actor + timestamp + amounts, oldest first."""
    entries = []
    for co in job.get("change_orders", []):
        for h in co.get("history", []):
            entries.append({
                "co_id": co["id"], "description": co.get("description", ""),
                "event": h.get("event"), "by_id": h.get("by_id"),
                "by_name": h.get("by_name"), "at": h.get("at"),
                "revenue_delta_cents": co.get("revenue_delta_cents", 0),
                "cost_delta_cents": co_cost_delta_cents(co),
            })
        if not co.get("history"):
            # Legacy CO: synthesize its creation entry from what exists.
            entries.append({
                "co_id": co["id"], "description": co.get("description", ""),
                "event": "created", "by_id": None, "by_name": None,
                "at": co.get("created_at"),
                "revenue_delta_cents": co.get("revenue_delta_cents", 0),
                "cost_delta_cents": co_cost_delta_cents(co),
            })
    entries.sort(key=lambda e: e.get("at") or "")
    return entries


async def _notify_co_approved(user, job: dict, co: dict):
    """In-app notification (app_notifications store, same shape as the
    sibling lanes): the team learns the budget moved. The approve
    endpoint's state guard makes this fire exactly once per CO."""
    rev = co.get("revenue_delta_cents", 0)
    cost = co_cost_delta_cents(co)
    await Scoped(db.app_notifications, user["tenant_id"]).insert_one({
        "id": new_id(), "user_id": None, "tenant_wide": True,
        "kind": "change_order_approved",
        "title": f"{job['job_number']} change order approved",
        "body": (f"“{co.get('description', '')}” approved by "
                 f"{user.get('name') or user.get('email', '')} — job "
                 f"estimate revised: revenue {cents_to_dollars(rev):+}, "
                 f"cost {cents_to_dollars(cost):+}."),
        "link": "/jobs", "read": False,
        "dedupe_key": f"co-approved:{co['id']}",
        "job_id": job["id"], "job_number": job["job_number"],
        "co_id": co["id"], "created_at": now_iso(),
    })


# ---------------- Profitability ----------------
async def _team_rates(user):
    """{user_id: hourly_rate_cents} for valuing time entries as labor."""
    rows = await db.users.find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0, "id": 1, "pay_settings": 1}
    ).to_list(500)
    return {r["id"]: ((r.get("pay_settings") or {}).get("hourly_rate_cents") or 0)
            for r in rows}


async def _job_time_block(user, job, team_rates):
    """Analytical labor from approved billable time entries linked to this job.

    NOT posted to the ledger — shown alongside posted costs so shops can see
    true labor without double-counting wages already posted via payroll or
    manual labor cost entries.
    """
    entries = await Scoped(db.time_entries, user["tenant_id"]).find_list(
        {"job_id": job["id"]}, sort=[("date", 1)], limit=5000)
    tl = T.job_time_labor(entries, job["id"], team_rates or {})
    return {
        "time_labor_hours": tl["hours"],
        "time_labor_cents": tl["cost_cents"],
        "time_labor": cents_to_dollars(tl["cost_cents"]),
        "time_entry_count": tl["entry_count"],
        "time_labor_note": "Approved billable time entries linked to this job (analytical — not posted)",
    }


async def _profit(user, job, team_rates=None):
    tenant_id = user["tenant_id"]
    costs = await jobcosts(user).find_list({"job_id": job["id"]})
    actual_by_cat = {c: 0 for c in CATEGORIES}
    burden_by_cat = {c: 0 for c in CATEGORIES}
    for c in costs:
        actual_by_cat[c["category"]] = actual_by_cat.get(c["category"], 0) + c["amount_cents"]
        burden_by_cat[c["category"]] = burden_by_cat.get(c["category"], 0) + (c.get("burden_cents") or 0)
    actual_cost = sum(actual_by_cat.values())
    labor_burden = burden_by_cat.get("labor", 0)
    burdened_by_cat = {c: actual_by_cat[c] + burden_by_cat[c] for c in CATEGORIES}

    invoices = await Scoped(db.invoices, tenant_id).find_list({"job_id": job["id"]})
    actual_revenue = sum(i.get("subtotal_cents", 0) for i in invoices)

    # estimate incl. APPROVED change orders only (pending/rejected
    # never move the budget — the overrun-alert lane reads this math)
    est_rev = job.get("estimated_revenue_cents", 0)
    est_by_cat = {c: (job.get("estimated_costs_cents", {}) or {}).get(c, 0)
                  for c in CATEGORIES}
    est_cost = sum(est_by_cat.values())
    for co in job.get("change_orders", []):
        if co_state(co) == "approved":
            est_rev += co.get("revenue_delta_cents", 0)
            impact = co.get("cost_impact_cents") or {}
            if impact:
                for c in CATEGORIES:
                    est_by_cat[c] += impact.get(c, 0)
                est_cost += sum(impact.get(c, 0) for c in CATEGORIES)
            else:
                est_cost += co.get("cost_delta_cents", 0)

    gp = actual_revenue - actual_cost
    margin = round(gp / actual_revenue * 100, 2) if actual_revenue else 0.0
    est_gp = est_rev - est_cost
    est_margin = round(est_gp / est_rev * 100, 2) if est_rev else 0.0
    time_block = await _job_time_block(user, job, team_rates)
    return {
        "actual_revenue_cents": actual_revenue, "actual_cost_cents": actual_cost,
        "actual_by_category": {k: cents_to_dollars(v) for k, v in actual_by_cat.items()},
        "actual_by_category_cents": actual_by_cat,
        **time_block,
        "labor_burden_cents": labor_burden,
        "burdened_cost_cents": actual_cost + labor_burden,
        "burdened_by_category_cents": burdened_by_cat,
        "gross_profit_cents": gp, "gross_margin_pct": margin,
        "estimated_revenue_cents": est_rev, "estimated_cost_cents": est_cost,
        "estimated_by_category_cents": est_by_cat,
        "estimated_by_category": {k: cents_to_dollars(v) for k, v in est_by_cat.items()},
        "estimated_gp_cents": est_gp, "estimated_margin_pct": est_margin,
        "cost_variance_cents": actual_cost - est_cost,
        "revenue": cents_to_dollars(actual_revenue), "cost": cents_to_dollars(actual_cost),
        "gross_profit": cents_to_dollars(gp),
        "estimated_revenue": cents_to_dollars(est_rev), "estimated_cost": cents_to_dollars(est_cost),
        "is_unprofitable": gp < 0,
        "invoice_count": len(invoices),
    }


# ---------------- Budget watch (overrun alerts, walk #9) ----------------
def _validate_thresholds(warning_pct, overrun_pct):
    for name, v in (("warning_pct", warning_pct), ("overrun_pct", overrun_pct)):
        if v is not None and not (0 < v <= 200):
            raise HTTPException(status_code=422,
                                detail=f"{name} must be between 1 and 200")
    if warning_pct is not None and overrun_pct is not None \
            and warning_pct >= overrun_pct:
        raise HTTPException(status_code=422,
                            detail="warning_pct must be below overrun_pct")


async def _budget_for(user, job, profit):
    cfg = await JA.get_config(user["tenant_id"])
    return JA.budget_state(job, profit, cfg)


async def _evaluate(user, job):
    """Recompute profit and run the budget watch after a mutation."""
    profit = await _profit(user, job)
    res = await JA.evaluate_alerts(user["tenant_id"], job, profit,
                                   actor_id=user.get("id"))
    return profit, res


class AlertSettingsBody(BaseModel):
    warning_pct: float = JA.DEFAULT_WARNING_PCT
    overrun_pct: float = JA.DEFAULT_OVERRUN_PCT
    enabled: bool = True


@router.get("/alert-settings")
async def get_alert_settings(user: dict = Depends(get_current_user)):
    return await JA.get_config(user["tenant_id"])


@router.put("/alert-settings")
async def update_alert_settings(body: AlertSettingsBody,
                                user: dict = Depends(require("ops:manage"))):
    _validate_thresholds(body.warning_pct, body.overrun_pct)
    cfg = await JA.save_config(user["tenant_id"], body.warning_pct,
                               body.overrun_pct, body.enabled)
    await L.audit(db, user["tenant_id"], user, "job.alert_settings",
                  {"warning_pct": body.warning_pct,
                   "overrun_pct": body.overrun_pct, "enabled": body.enabled})
    return cfg


# Job-related notification kinds served by this one feed: budget alerts
# (overrun lane), change-order approvals (CO lane), phase drift (phase lane).
JOB_NOTIFICATION_KINDS = ("job_budget_alert", "change_order_approved",
                          "job_phase_drift")


@router.get("/notifications")
async def list_job_notifications(user: dict = Depends(get_current_user)):
    rows = await JA._notif_coll(user["tenant_id"]).find_list(
        {"kind": {"$in": list(JOB_NOTIFICATION_KINDS)}},
        sort=[("created_at", -1)], limit=50)
    return {"notifications": rows,
            "unread": sum(1 for r in rows if not r.get("read"))}


# Alias: the change-order lane's tests/clients call the feed by this name.
list_notifications = list_job_notifications


@router.post("/notifications/{notif_id}/read")
async def mark_job_notification_read(notif_id: str,
                                     user: dict = Depends(get_current_user)):
    await JA._notif_coll(user["tenant_id"]).update_one(
        {"id": notif_id, "kind": {"$in": list(JOB_NOTIFICATION_KINDS)}},
        {"$set": {"read": True}})
    return {"ok": True}


mark_notification_read = mark_job_notification_read


# ---------------- CRUD ----------------
class JobBody(BaseModel):
    customer_id: Optional[str] = None
    customer_name: str
    service_location: str = ""
    job_type: str = "installation"        # installation | maintenance | repair
    description: str = ""
    estimated_revenue: float = 0
    estimated_costs: dict = {}            # {material, labor, equipment, subcontractor, other} dollars
    alert_warning_pct: Optional[float] = None   # per-job budget-watch override
    alert_overrun_pct: Optional[float] = None
    retainage_pct: float = 0              # % the customer holds back from us (0 = no retainage)
    retainage_payable_pct: Optional[float] = None   # % we hold back from subs (None = same as above)
    retainage_release_days: Optional[int] = None    # release due N days after substantial completion
    retainage_release_due_date: Optional[str] = None


@router.get("")
async def list_jobs(user: dict = Depends(get_current_user)):
    rows = await jobs(user).find_list(sort=[("created_at", -1)], limit=500)
    rates = await _team_rates(user)
    cfg = await JA.get_config(user["tenant_id"])
    out = []
    for j in rows:
        p = await _profit(user, j, rates)
        out.append({**j, "profit": p, "budget": JA.budget_state(j, p, cfg), "co_summary": co_summary(j)})
    return out


@router.get("/reports/dashboard")
async def job_dashboard(user: dict = Depends(require("ledger:read"))):
    rows = await jobs(user).find_list(sort=[("created_at", -1)], limit=1000)
    rates = await _team_rates(user)
    enriched = [{**j, "profit": await _profit(user, j, rates)} for j in rows]

    def card(j):
        p = j["profit"]
        return {"job_number": j["job_number"], "customer": j["customer_name"], "status": j["status"],
                "job_type": j["job_type"], "revenue": p["revenue"], "cost": p["cost"],
                "gross_profit": p["gross_profit"], "margin": p["gross_margin_pct"]}

    total_rev = sum(j["profit"]["actual_revenue_cents"] for j in enriched)
    total_cost = sum(j["profit"]["actual_cost_cents"] for j in enriched)
    total_gp = total_rev - total_cost
    billed = [j for j in enriched if j["profit"]["actual_revenue_cents"] > 0]
    profitable = [j for j in billed if j["profit"]["gross_profit_cents"] > 0]
    unprofitable = [j for j in billed if j["profit"]["gross_profit_cents"] < 0]
    # at-risk: active jobs with costs logged but margin under 20% (or revenue not yet billed but cost > est)
    at_risk = []
    for j in enriched:
        p = j["profit"]
        if j["status"] in ("active", "completed") and p["actual_revenue_cents"] > 0 and 0 <= p["gross_margin_pct"] < 20:
            at_risk.append(card(j))
    avg_margin = round(sum(j["profit"]["gross_margin_pct"] for j in billed) / len(billed), 2) if billed else 0.0

    return {
        "kpis": {
            "total_jobs": len(enriched),
            "active": len([j for j in enriched if j["status"] == "active"]),
            "estimates": len([j for j in enriched if j["status"] == "estimate"]),
            "completed": len([j for j in enriched if j["status"] in ("completed", "closed")]),
            "revenue": cents_to_dollars(total_rev),
            "cost": cents_to_dollars(total_cost),
            "gross_profit": cents_to_dollars(total_gp),
            "avg_margin": avg_margin,
            "profitable_count": len(profitable),
            "unprofitable_count": len(unprofitable),
        },
        "unprofitable": sorted([card(j) for j in unprofitable], key=lambda x: x["gross_profit"]),
        "at_risk": at_risk,
        "top_jobs": sorted([card(j) for j in profitable], key=lambda x: x["gross_profit"], reverse=True)[:5],
    }


@router.get("/reports/profitability")
async def profitability_report(group_by: str = "job", user: dict = Depends(require("ledger:read"))):
    rows = await jobs(user).find_list(sort=[("created_at", -1)], limit=1000)
    rates = await _team_rates(user)
    enriched = [{**j, "profit": await _profit(user, j, rates)} for j in rows]

    if group_by == "job":
        items = [{
            "key": j["job_number"], "label": f"{j['job_number']} — {j['customer_name']}",
            "revenue": j["profit"]["revenue"], "cost": j["profit"]["cost"],
            "gross_profit": j["profit"]["gross_profit"], "margin": j["profit"]["gross_margin_pct"],
            "unprofitable": j["profit"]["is_unprofitable"], "status": j["status"],
        } for j in enriched]
        return {"group_by": group_by, "items": items}

    buckets = {}
    tech_names = {}
    if group_by == "technician":
        team = await db.users.find({"tenant_id": user["tenant_id"]}, {"_id": 0, "id": 1, "name": 1}).to_list(200)
        tech_names = {t["id"]: t["name"] for t in team}
        costs = await jobcosts(user).find_list({"category": "labor"})
        for c in costs:
            tid = c.get("technician_id") or "unassigned"
            b = buckets.setdefault(tid, {"label": tech_names.get(tid, "Unassigned"), "cost_cents": 0, "hours": 0, "jobs": set()})
            b["cost_cents"] += c["amount_cents"]
            b["hours"] += c.get("hours", 0) or 0
            b["jobs"].add(c["job_id"])
        items = [{"key": k, "label": v["label"], "labor_cost": cents_to_dollars(v["cost_cents"]),
                  "hours": round(v["hours"], 1), "jobs": len(v["jobs"])} for k, v in buckets.items()]
        return {"group_by": group_by, "items": items}

    # customer or service_type
    field = "customer_name" if group_by == "customer" else "job_type"
    for j in enriched:
        key = j.get(field, "—")
        b = buckets.setdefault(key, {"revenue": 0, "cost": 0, "gp": 0, "count": 0})
        b["revenue"] += j["profit"]["actual_revenue_cents"]
        b["cost"] += j["profit"]["actual_cost_cents"]
        b["gp"] += j["profit"]["gross_profit_cents"]
        b["count"] += 1
    items = [{"key": k, "label": k, "revenue": cents_to_dollars(v["revenue"]),
              "cost": cents_to_dollars(v["cost"]), "gross_profit": cents_to_dollars(v["gp"]),
              "margin": round(v["gp"] / v["revenue"] * 100, 2) if v["revenue"] else 0.0,
              "count": v["count"]} for k, v in buckets.items()]
    return {"group_by": group_by, "items": items}


async def _phase_view(user, job, profit):
    """Per-phase variance for a job (empty view when it has no phases —
    an unphased job's payload is exactly what it was before phases)."""
    if not job.get("phases"):
        costs = await jobcosts(user).find_list({"job_id": job["id"]})
        warn, over = await JP.get_thresholds(user["tenant_id"])
        return JP.phase_variance(job, costs,
                                 profit["estimated_cost_cents"], warn, over)
    costs = await jobcosts(user).find_list({"job_id": job["id"]})
    warn, over = await JP.get_thresholds(user["tenant_id"])
    committed = await JP.committed_by_phase(user["tenant_id"], job)
    view = JP.phase_variance(job, costs, profit["estimated_cost_cents"],
                             warn, over, committed)
    await JP.evaluate_phase_drift(user["tenant_id"], job, view)
    return view


@router.get("/{job_id}")
async def get_job(job_id: str, user: dict = Depends(get_current_user)):
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    j = {k: v for k, v in j.items() if k != "_id"}
    j["change_orders"] = [public_co(co) for co in j.get("change_orders", [])]
    j["co_summary"] = co_summary(j)
    j["co_history"] = co_history(j)
    j["profit"] = await _profit(user, j, await _team_rates(user))
    j["budget"] = await _budget_for(user, j, j["profit"])
    j["phase_variance"] = await _phase_view(user, j, j["profit"])
    j["retainage"] = await R.job_summary(user["tenant_id"], j)
    j["costs"] = await jobcosts(user).find_list({"job_id": job_id}, sort=[("created_at", -1)])
    j["invoices"] = await Scoped(db.invoices, user["tenant_id"]).find_list({"job_id": job_id}, sort=[("created_at", -1)])
    # Committed-costs budget view — shares the "budget" key with the
    # job-alerts budget_state set above (both lanes named it "budget";
    # merged 2026-11-14): when the tenant has the (optional) committed-
    # costs feature on, its fields merge INTO the alert view — the two
    # agree on estimated/actual cents, and JC adds committed / remaining
    # / commitments / purchase_orders. Off = alert view exactly as before.
    budget = await JC.budget_view(user["tenant_id"], j, j["profit"])
    if budget.get("enabled"):
        j["budget"] = {**j["budget"], **budget}

    # Field-capture freshness: actuals update the moment a time entry is
    # approved or a receipt posts (no batch step), so the newest field cost's
    # timestamp IS the freshness of the field portion of this job's numbers.
    field_costs = [c for c in j["costs"] if c.get("source") in ("time_entry", "receipt")]
    if field_costs:
        last = max(c["created_at"] for c in field_costs)
        j["field_capture"] = {
            "last_field_cost_at": last,
            "includes_today": last[:10] == today_str(),
            "field_cost_count": len(field_costs),
        }
    else:
        j["field_capture"] = None
    return j


# ---------------- Phases / cost codes ----------------
class PhaseBody(BaseModel):
    name: str
    budget: dict = {}          # {material, labor, equipment, subcontractor, other} dollars


def _phase_budget_cents(raw: dict) -> dict:
    bad = set(raw or {}) - set(CATEGORIES)
    if bad:
        raise HTTPException(status_code=422,
                            detail=f"Unknown budget categories: {sorted(bad)}")
    return {c: to_cents((raw or {}).get(c, 0)) for c in CATEGORIES}


async def _job_or_404(user, job_id):
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    return j


@router.get("/{job_id}/phases")
async def get_phases(job_id: str, user: dict = Depends(get_current_user)):
    j = await _job_or_404(user, job_id)
    profit = await _profit(user, j)
    return await _phase_view(user, j, profit)


@router.post("/{job_id}/phases")
async def add_phase(job_id: str, body: PhaseBody,
                    user: dict = Depends(require("ops:manage"))):
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Phase name is required")
    j = await _job_or_404(user, job_id)
    phase = {"id": new_id(), "name": body.name.strip(),
             "budget_cents": _phase_budget_cents(body.budget),
             "created_at": now_iso()}
    await jobs(user).update_one({"id": job_id},
                                {"$push": {"phases": phase}})
    j = await _job_or_404(user, job_id)
    profit = await _profit(user, j)
    check = JP.budget_check(j, profit["estimated_cost_cents"])
    await L.audit(db, user["tenant_id"], user, "job.phase_add",
                  {"job": j["job_number"], "phase": phase["name"]})
    return {"phase": phase, "budget_check": check}


@router.patch("/{job_id}/phases/{phase_id}")
async def update_phase(job_id: str, phase_id: str, body: PhaseBody,
                       user: dict = Depends(require("ops:manage"))):
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Phase name is required")
    j = await _job_or_404(user, job_id)
    phase = JP.phase_of(j, phase_id)
    if not phase:
        raise HTTPException(status_code=404, detail="Phase not found")
    phases = [dict(p) for p in j.get("phases") or []]
    for p in phases:
        if p["id"] == phase_id:
            p["name"] = body.name.strip()
            p["budget_cents"] = _phase_budget_cents(body.budget)
    await jobs(user).update_one({"id": job_id}, {"$set": {"phases": phases}})
    j = await _job_or_404(user, job_id)
    profit = await _profit(user, j)
    check = JP.budget_check(j, profit["estimated_cost_cents"])
    return {"phase": JP.phase_of(j, phase_id), "budget_check": check}


@router.delete("/{job_id}/phases/{phase_id}")
async def delete_phase(job_id: str, phase_id: str,
                       user: dict = Depends(require("ops:manage"))):
    j = await _job_or_404(user, job_id)
    if not JP.phase_of(j, phase_id):
        raise HTTPException(status_code=404, detail="Phase not found")
    tagged = await jobcosts(user).find_list({"job_id": job_id,
                                             "phase_id": phase_id}, limit=1)
    if tagged:
        raise HTTPException(
            status_code=409,
            detail="Phase has costs tagged to it — re-tag them before deleting")
    phases = [p for p in j.get("phases") or [] if p["id"] != phase_id]
    await jobs(user).update_one({"id": job_id}, {"$set": {"phases": phases}})
    j = await _job_or_404(user, job_id)
    profit = await _profit(user, j)
    return {"deleted": phase_id,
            "budget_check": JP.budget_check(j, profit["estimated_cost_cents"])}


@router.post("")
async def create_job(body: JobBody, user: dict = Depends(require("ops:manage"))):
    _validate_thresholds(body.alert_warning_pct, body.alert_overrun_pct)
    seq = await Scoped(db.jobs, user["tenant_id"]).next_seq(db.counters, "job")
    est_costs = {c: to_cents(body.estimated_costs.get(c, 0)) for c in CATEGORIES}
    doc = {
        "id": new_id(), "job_number": f"JOB-{seq:04d}",
        "customer_id": body.customer_id, "customer_name": body.customer_name,
        "service_location": body.service_location, "job_type": body.job_type,
        "description": body.description, "status": "estimate",
        "estimated_revenue_cents": to_cents(body.estimated_revenue),
        "estimated_costs_cents": est_costs, "change_orders": [],
        "phases": [],
        "retainage_pct": body.retainage_pct,
        "retainage_payable_pct": body.retainage_payable_pct,
        "retainage_phase_pcts": {},
        "retainage_release_days": body.retainage_release_days,
        "retainage_release_due_date": body.retainage_release_due_date,
        "substantial_completion_date": None,
        "created_at": now_iso(),
    }
    if body.alert_warning_pct is not None:
        doc["alert_warning_pct"] = body.alert_warning_pct
    if body.alert_overrun_pct is not None:
        doc["alert_overrun_pct"] = body.alert_overrun_pct
    await jobs(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


class JobThresholdsBody(BaseModel):
    alert_warning_pct: Optional[float] = None
    alert_overrun_pct: Optional[float] = None


@router.patch("/{job_id}/alert-thresholds")
async def update_job_thresholds(job_id: str, body: JobThresholdsBody,
                                user: dict = Depends(require("ops:manage"))):
    """Per-job budget-watch override. Passing null for a field clears
    that override (the tenant threshold applies again)."""
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    eff_warn = body.alert_warning_pct if body.alert_warning_pct is not None \
        else j.get("alert_warning_pct")
    eff_over = body.alert_overrun_pct if body.alert_overrun_pct is not None \
        else j.get("alert_overrun_pct")
    _validate_thresholds(eff_warn, eff_over)
    await jobs(user).update_one({"id": job_id}, {"$set": {
        "alert_warning_pct": body.alert_warning_pct,
        "alert_overrun_pct": body.alert_overrun_pct}})
    j = await jobs(user).find_one({"id": job_id})
    _, res = await _evaluate(user, j)
    return {"job_id": job_id,
            "alert_warning_pct": body.alert_warning_pct,
            "alert_overrun_pct": body.alert_overrun_pct,
            "budget": res["budget"], "alerts_fired": res["fired"]}


class StatusBody(BaseModel):
    status: str   # estimate | active | completed | closed


@router.patch("/{job_id}")
async def update_status(job_id: str, body: StatusBody, user: dict = Depends(require("ops:manage"))):
    if body.status not in ("estimate", "active", "completed", "closed"):
        raise HTTPException(status_code=422, detail="Invalid status")
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    await jobs(user).update_one({"id": job_id}, {"$set": {"status": body.status}})
    await L.audit(db, user["tenant_id"], user, "job.status", {"job": j["job_number"], "status": body.status})
    j = await jobs(user).find_one({"id": job_id})
    _, res = await _evaluate(user, j)
    return {"status": body.status, "budget": res["budget"],
            "alerts_fired": res["fired"]}


# ---------------- Costs ----------------
async def record_job_cost(user, job, *, category, description="", amount_cents=None,
                          amount=None, hours=0.0, rate_cents=0, product_id=None, qty=1,
                          technician_id=None, paid=False, source="manual", source_ref=None,
                          burden_percent=None, extra=None, commitment_id=None,
                          phase_id=None):
    """THE job cost-posting path: exactly one journal entry + one job_costs row.

    The manual POST /jobs/{id}/costs endpoint and field capture (approved time
    entries, posted receipts) all land job actuals through here, so a cost
    behaves identically however it arrived. The GL always posts the BASE
    amount; when `burden_percent` is given for a labor line, the fully-loaded
    figure is stored alongside (`burdened_amount_cents`) for the burden lane —
    never overwriting `amount_cents` (see lib/burden.py).

    Amount resolution when `amount_cents` is None mirrors the manual endpoint:
    material+product draws down inventory at unit cost; labor falls back to
    hours x rate_cents; otherwise `amount` (dollars) is used.
    """
    tenant_id = user["tenant_id"]
    if category not in CATEGORY_ACCOUNT:
        raise HTTPException(status_code=422, detail="Invalid category")
    debit_code = CATEGORY_ACCOUNT[category]
    credit_code = "1020" if paid else "2010"

    if category == "material" and product_id:
        prod = await Scoped(db.products, tenant_id).find_one({"id": product_id})
        if not prod:
            raise HTTPException(status_code=404, detail="Product not found")
        res = await Scoped(db.products, tenant_id).update_one(
            {"id": product_id, "quantity_on_hand": {"$gte": qty}},
            {"$inc": {"quantity_on_hand": -qty}})
        if res.matched_count == 0:
            raise HTTPException(status_code=409, detail=f"{prod['name']} is out of stock")
        amount_cents = prod["unit_cost_cents"] * qty
        credit_code = "1300"   # relieve inventory
    elif category == "labor":
        credit_code = "1020" if paid else "2320"   # wages payable
        if amount_cents is None:
            amount_cents = int(round(hours * rate_cents)) if hours else (
                to_cents(amount) if amount is not None else None)
    elif amount_cents is None:
        amount_cents = to_cents(amount) if amount is not None else None

    if not amount_cents or amount_cents <= 0:
        raise HTTPException(status_code=422, detail="Cost amount must be positive")

    # Job-costing overlay (manual endpoint only): burdened labor via the
    # tenant's burden templates. Field-capture paths carry their own
    # burden_percent snapshot and never resolve templates here.
    jc_burden = None
    if source == "manual" and category == "labor" and burden_percent is None:
        pct, tpl = await JC.resolve_burden(tenant_id, technician_id)
        amts = JC.labor_amounts(amount_cents, pct)
        jc_burden = {"burden_pct": amts["burden_pct"],
                     "burden_cents": amts["burden_cents"],
                     "burdened_cents": amts["burdened_cents"],
                     "burden_template_id": tpl["id"] if tpl else None,
                     "burden_template_name": tpl["name"] if tpl else None}

    cost_id = new_id()
    entry = await L.post_journal_entry(db, tenant_id, user, {
        "entry_date": today_str(), "source": "job_cost", "source_ref": cost_id,
        "memo": f"{job['job_number']} {category}: {description}".strip(),
        "lines": [P.dr(debit_code, amount_cents, description or category),
                  P.cr(credit_code, amount_cents, "Job cost")],
    }, status="posted")

    if category == "labor" and burden_percent is not None:
        from lib.burden import burdened_cents
        burden_pct = float(burden_percent)
        burdened = burdened_cents(amount_cents, burden_pct)
    else:
        burden_pct = None
        burdened = None

    doc = {
        "id": cost_id, "job_id": job["id"], "job_number": job["job_number"],
        "category": category, "description": description,
        "amount_cents": amount_cents, "hours": hours, "rate": rate_cents,
        "product_id": product_id, "qty": qty, "technician_id": technician_id,
        "phase_id": phase_id,
        "entry_number": entry["number"], "journal_entry_id": entry.get("id"),
        "source": source, "source_ref": source_ref,
        "burden_percent": burden_pct, "burdened_amount_cents": burdened,
        "created_at": now_iso(),
        **(jc_burden or {}),
    }
    if extra:
        doc.update(extra)
    await jobcosts(user).insert_one(doc)

    if source == "manual":
        # Subcontract commitment fulfilment (job-costing lane): posting a
        # subcontractor cost against a commitment fulfils it down.
        if category == "subcontractor" and commitment_id:
            cfg = await JC.get_config(tenant_id)
            if cfg["committed_costs_enabled"]:
                coll = Scoped(db.job_commitments, tenant_id)
                com = await coll.find_one({"id": commitment_id,
                                           "job_id": job["id"]})
                if com and com.get("status") == "approved":
                    fulfilled = min(com["amount_cents"],
                                    (com.get("fulfilled_cents") or 0) + amount_cents)
                    await coll.update_one({"id": com["id"]}, {"$set": {
                        "fulfilled_cents": fulfilled,
                        "status": "fulfilled" if fulfilled >= com["amount_cents"]
                                  else "approved"}})
                    doc["commitment_fulfilled_cents"] = fulfilled
        # Burden overrun flag: burdened labor tipping the job over its
        # estimate raises the shared budget-alert notification once.
        if category == "labor" and jc_burden and jc_burden["burden_cents"] > 0:
            j2 = await jobs(user).find_one({"id": job["id"]})
            profit = await _profit(user, j2)
            doc["burden_overrun"] = await JC.evaluate_burden_overrun(
                tenant_id, j2, profit, actor_id=user.get("id"))

    doc.pop("_id", None)
    return doc


class CostBody(BaseModel):
    category: str
    description: str = ""
    amount: float = 0
    hours: float = 0
    rate: float = 0
    product_id: Optional[str] = None
    qty: int = 1
    technician_id: Optional[str] = None
    phase_id: Optional[str] = None   # optional phase / cost-code tag
    paid: bool = False    # True -> cash; False -> payable
    commitment_id: Optional[str] = None   # subcontract commitment this cost fulfils


@router.post("/{job_id}/costs")
async def add_cost(job_id: str, body: CostBody, user: dict = Depends(require("ops:manage"))):
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    if j["status"] == "estimate":
        raise HTTPException(status_code=409, detail="Convert the estimate to an active job before logging costs")
    if body.category not in CATEGORY_ACCOUNT:
        raise HTTPException(status_code=422, detail="Invalid category")
    if body.phase_id and not JP.phase_of(j, body.phase_id):
        raise HTTPException(status_code=422, detail="Phase not found on this job")
    # Manual path keeps its historical amount math exactly (Decimal dollars).
    amount_cents = None
    if body.category == "labor":
        amount_cents = to_cents(body.hours * body.rate) if body.hours else to_cents(body.amount)
    elif not (body.category == "material" and body.product_id):
        amount_cents = to_cents(body.amount)
    doc = await record_job_cost(
        user, j, category=body.category, description=body.description,
        amount_cents=amount_cents, hours=body.hours, rate_cents=to_cents(body.rate),
        product_id=body.product_id, qty=body.qty, technician_id=body.technician_id,
        paid=body.paid, source="manual", source_ref=None,
        commitment_id=body.commitment_id, phase_id=body.phase_id)
    _, res = await _evaluate(user, j)
    doc["budget"] = res["budget"]
    doc["alerts_fired"] = res["fired"]
    if body.phase_id:
        view = await _phase_view(user, j, await _profit(user, j))
        doc["phase_drift"] = [pv for pv in view["phases"]
                              if pv["phase_id"] == body.phase_id and pv["drift"]]
    return doc


@router.post("/{job_id}/costs/preview")
async def preview_cost(job_id: str, body: CostBody, user: dict = Depends(require("ops:manage"))):
    """Pre-post preview for the cost form: for labor, the burdened
    (true) cost of the line BEFORE it posts, and whether burdened
    labor would tip the job over its estimate. Writes nothing."""
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    if body.category not in CATEGORY_ACCOUNT:
        raise HTTPException(status_code=422, detail="Invalid category")
    if body.category == "labor":
        base = to_cents(body.hours * body.rate) if body.hours else to_cents(body.amount)
        pct, tpl = await JC.resolve_burden(user["tenant_id"], body.technician_id)
        amts = JC.labor_amounts(base, pct)
    else:
        amts = {"base_cents": to_cents(body.amount), "burden_pct": 0.0,
                "burden_cents": 0, "burdened_cents": to_cents(body.amount)}
        tpl = None
    profit = await _profit(user, j)
    est = profit["estimated_cost_cents"]
    burdened_after = profit["burdened_cost_cents"] + amts["burdened_cents"] \
        if body.category == "labor" else profit["burdened_cost_cents"] + amts["base_cents"]
    actual_after = profit["actual_cost_cents"] + amts["base_cents"]
    return {
        **amts,
        "burden_template_name": tpl["name"] if tpl else None,
        "estimated_cost_cents": est,
        "actual_cost_cents": profit["actual_cost_cents"],
        "actual_after_cents": actual_after,
        "burdened_after_cents": burdened_after,
        "would_cross_estimate": bool(
            est > 0 and profit["actual_cost_cents"] <= est < actual_after),
        "burden_would_tip_over": bool(
            est > 0 and actual_after <= est < burdened_after),
    }


# ---------------- Change orders ----------------
class ChangeOrderBody(BaseModel):
    description: str
    reason: str = ""
    revenue_delta: float = 0
    cost_delta: float = 0                 # legacy lump (used only when no breakdown sent)
    cost_impact: dict = {}                # {material, labor, equipment, subcontractor, other} dollars
    delay_days: int = 0


@router.post("/{job_id}/change-orders")
async def add_change_order(job_id: str, body: ChangeOrderBody, user: dict = Depends(require("ops:manage"))):
    """Create a change order as PENDING — it never touches the job
    budget until approved. Any client-sent approval is ignored."""
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    impact = {c: to_cents(body.cost_impact.get(c, 0)) for c in CATEGORIES
              if body.cost_impact.get(c)}
    actor = {"by_id": user.get("id"), "by_name": user.get("name") or user.get("email")}
    co = {
        "id": new_id(), "description": body.description,
        "reason": body.reason,
        "revenue_delta_cents": to_cents(body.revenue_delta),
        "cost_delta_cents": (sum(impact.values()) if impact
                             else to_cents(body.cost_delta)),
        "cost_impact_cents": impact,
        "delay_days": int(body.delay_days or 0),
        "state": "pending", "approved": False,
        "created_at": now_iso(), "created_by": actor,
        "decided_by": None, "decided_at": None,
        "history": [{"event": "created", **actor, "at": now_iso()}],
    }
    await jobs(user).update_one({"id": job_id}, {"$push": {"change_orders": co}})
    await L.audit(db, user["tenant_id"], user, "job.change_order.create",
                  {"job": j["job_number"], "co": co["id"],
                   "revenue_delta_cents": co["revenue_delta_cents"],
                   "cost_delta_cents": co["cost_delta_cents"]})
    return public_co(co)


async def _decide_change_order(job_id: str, co_id: str, user, approve: bool):
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    cos = j.get("change_orders", [])
    idx = next((i for i, c in enumerate(cos) if c.get("id") == co_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Change order not found")
    co = cos[idx]
    if co_state(co) != "pending":
        raise HTTPException(status_code=409,
                            detail=f"Change order already {co_state(co)}")
    actor = {"by_id": user.get("id"), "by_name": user.get("name") or user.get("email")}
    now = now_iso()
    co["state"] = "approved" if approve else "rejected"
    co["approved"] = approve
    co["decided_by"] = actor
    co["decided_at"] = now
    co.setdefault("history", []).append(
        {"event": co["state"], **actor, "at": now})
    cos[idx] = co
    await jobs(user).update_one({"id": job_id}, {"$set": {"change_orders": cos}})
    await L.audit(db, user["tenant_id"], user,
                  "job.change_order.approve" if approve else "job.change_order.reject",
                  {"job": j["job_number"], "co": co_id})
    if approve:
        # Approval revises the budget in this same request (the stored
        # job now carries an approved CO, and _profit reads it
        # immediately) and notifies the team — exactly once, guarded
        # by the pending-state check above.
        await _notify_co_approved(user, j, co)
        # An approved change order moves the estimate, so the budget watch
        # re-evaluates: an increase can clear a fired threshold (re-crossing
        # may fire again); a decrease can cross one right now.
        j = await jobs(user).find_one({"id": job_id})
        _, res = await _evaluate(user, j)
        return {**public_co(co), "budget": res["budget"],
                "alerts_fired": res["fired"]}
    return public_co(co)


@router.post("/{job_id}/change-orders/{co_id}/approve")
async def approve_change_order(job_id: str, co_id: str, user: dict = Depends(require("ops:manage"))):
    return await _decide_change_order(job_id, co_id, user, approve=True)


@router.post("/{job_id}/change-orders/{co_id}/reject")
async def reject_change_order(job_id: str, co_id: str, user: dict = Depends(require("ops:manage"))):
    return await _decide_change_order(job_id, co_id, user, approve=False)


# ---------------- Job invoice (revenue) ----------------
class JobInvoiceBody(BaseModel):
    amount: float
    tax: float = 0
    phase_id: Optional[str] = None    # bill against one phase (retainage % may differ by phase)


@router.post("/{job_id}/invoice")
async def invoice_job(job_id: str, body: JobInvoiceBody, user: dict = Depends(require("ops:manage"))):
    tenant_id = user["tenant_id"]
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    if body.phase_id and j.get("phases"):
        if body.phase_id not in {p.get("id") for p in j["phases"]}:
            raise HTTPException(status_code=422, detail="Unknown phase for this job")
    subtotal = to_cents(body.amount)
    tax_cents = to_cents(body.tax)
    gross = subtotal + tax_cents

    # Retainage: the customer's holdback is RECORDED, never collected by
    # us. Net due rides regular AR (1200); the held slice rides the
    # retainage receivable account (1210) and the sub-ledger, and the
    # invoice carries gross / held / net due for the customer to see.
    pct = R.pct_for_job(j, body.phase_id, "receivable")
    held = R.held_cents_for(subtotal, pct)
    inv_id = new_id()
    if held > 0:
        await R.ensure_accounts(tenant_id)
        jl = [P.dr("1200", gross - held, "Invoice issued — net of retainage"),
              P.dr(R.RET_RECEIVABLE, held, f"Retainage held ({pct}%)"),
              P.cr("4000", subtotal, "Revenue")]
        if tax_cents:
            jl.append(P.cr("2100", tax_cents, "Sales tax"))
        entry = await L.post_journal_entry(db, tenant_id, user, {
            "entry_date": today_str(), "source": "invoice_issue", "source_ref": inv_id,
            "memo": f"{j['job_number']} — {j['customer_name']}",
            "lines": jl,
        }, status="posted")
    else:
        entry = await L.post_journal_entry(db, tenant_id, user, P.build_invoice_issue(
            revenue_lines=[{"revenue_code": "4000", "amount_cents": subtotal}],
            tax_cents=tax_cents, cogs_cents=0, gross_cents=gross,
            entry_date=today_str(), source_ref=inv_id, memo=f"{j['job_number']} — {j['customer_name']}",
        ), status="posted")
    doc = {
        "id": inv_id, "job_id": job_id, "client_name": j["customer_name"],
        "lines": [{"kind": "job", "name": f"{j['job_number']} contract", "revenue_code": "4000", "amount": body.amount}],
        "subtotal_cents": subtotal, "tax_cents": tax_cents, "total_cents": gross,
        "retainage_pct": pct if held > 0 else 0,
        "retainage_held_cents": held,
        "net_due_cents": gross - held,
        "phase_id": body.phase_id,
        "status": "issued", "due_date": None, "issue_entry": entry["number"],
        "paid_entry": None, "created_at": now_iso(),
    }
    await Scoped(db.invoices, tenant_id).insert_one(doc)
    if held > 0:
        hold = await R.record_hold(
            tenant_id, side="receivable", job=j, phase_id=body.phase_id,
            source_type="job_invoice", source_id=inv_id,
            gross_cents=subtotal, held_cents=held, pct=pct,
            counterparty=j["customer_name"])
        doc["retainage_entry_id"] = hold["id"]
    doc.pop("_id", None)
    return doc
