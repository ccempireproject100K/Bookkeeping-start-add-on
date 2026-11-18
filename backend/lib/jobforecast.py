"""Estimate -> job budget + cost-to-complete forecast
(owner-approved 2026-11-14, job-costing requests report item #10).

The owner's six better-features, as built here:

A. BOTH NUMBERS, ALWAYS — a job carries an immutable BASELINE (the
   estimate snapshot taken at conversion, `baseline_*` fields on the
   job doc) and a live FORECAST. `compute_forecast` returns both side
   by side; the baseline is never rewritten by anything in this module.
B. HISTORY-SMART SUGGESTIONS — `budget_suggestion` looks at the shop's
   OWN completed jobs (>= MIN_HISTORY_JOBS per category, else silent —
   never fabricate a pattern) and offers per-category adjusted figures
   with the evidence (N jobs, average variance %). Suggestion only;
   acceptance is recorded on the job when the user accepts.
C. LIVE FORECAST — `compute_forecast` reads actuals/committed fresh on
   every call. No batch, no cached totals.
D. DRIFT DRIVERS — the payload names the categories (and phases) that
   drive the baseline -> forecast gap, largest first, with amounts.
E. WHAT-IF — `compute_forecast(..., overrides=...)` replays the math
   with caller-supplied remaining-cost figures. Pure read: the router
   endpoint writes nothing.
F. FORECAST HISTORY — snapshots in `job_forecast_snapshots` whenever
   the forecast moves materially (checked on cost posting and on
   forecast reads), on demand, and a final snapshot at close.

Forecast math per category:
    forecast = actual + committed + remaining
    remaining = max(budget - actual - committed, 0)
where budget = the job's current estimated costs (approved change
orders included at job-total level, exactly as jobs._profit computes
them — on main a change order carries one total cost delta, not a
per-category split, so the delta lands in the totals row and is named
in `change_order_delta_cents`).

COMMITTED SEAM: the committed-costs lane (feat/job-costing-committed-
burden, lib/jobcosting) is not on main. This module reads the same
sources with the same semantics — open purchase orders carrying
`job_id` (statuses approved / partially_received / received) and
approved unfulfilled `job_commitments` — gated by the same tenant
toggle (`job_costing_config` doc {key: "job_costing",
committed_costs_enabled}). Toggle off or sources absent -> committed
is reported as not-enabled with zeros, never guessed. Open PO value is
attributed to the `material` category (POs in this app buy products /
materials); subcontract commitments attribute to `subcontractor`.
When the jobcost lane merges, its budget_view() totals agree with
this module's committed total by construction (same sources, same
status sets, same billed-exclusion: a PO whose value has been billed
into actuals is counted once — see `billed_commitment_cents` note in
lib/jobcosting; here the equivalent guard is that `remaining` floors
at 0 so committed can never double-count past the budget).

PHASES SEAM: phases live on the job doc as
`phases: [{id, name, budget_cents: {category: cents}}]` (shape fixed
by feat/job-phase-costing). Costs may carry `phase_id`. This module
reads both when present and ignores them when absent — unphased jobs
behave exactly as jobs always have.
"""
from db import db
from lib.base import Scoped, new_id, now_iso

CATEGORIES = ["material", "labor", "equipment", "subcontractor", "other"]
COMPLETED_STATUSES = ("completed", "closed")
MIN_HISTORY_JOBS = 3

# A forecast move is "material" (feature F) when the total moves at
# least $50 OR at least 2% of the baseline total.
MATERIAL_CENTS = 5000
MATERIAL_PCT = 2.0

PO_COMMITTED_STATUSES = ("approved", "partially_received", "received")


def _zeros():
    return {c: 0 for c in CATEGORIES}


# ------------------------------------------------------------------
# Baseline / budget
# ------------------------------------------------------------------
def baseline_of(job):
    """The immutable original estimate snapshot. Jobs created from an
    estimate carry baseline_* fields; for any other job the baseline
    IS its original estimate (estimated_costs at creation), reported
    with source 'job' so the UI can label it honestly."""
    if job.get("baseline_costs_cents") is not None:
        return {
            "source": "estimate",
            "estimate_id": job.get("baseline_estimate_id"),
            "revenue_cents": job.get("baseline_revenue_cents",
                                     job.get("estimated_revenue_cents", 0)),
            "costs_cents": {c: int((job.get("baseline_costs_cents") or {})
                                   .get(c, 0)) for c in CATEGORIES},
            "phases": job.get("baseline_phases") or [],
            "taken_at": job.get("baseline_created_at"),
        }
    return {
        "source": "job",
        "estimate_id": job.get("source_estimate_id"),
        "revenue_cents": job.get("estimated_revenue_cents", 0),
        "costs_cents": {c: int((job.get("estimated_costs_cents") or {})
                               .get(c, 0)) for c in CATEGORIES},
        "phases": [],
        "taken_at": job.get("created_at"),
    }


def change_order_delta_cents(job):
    return sum(int(co.get("cost_delta_cents", 0))
               for co in job.get("change_orders", [])
               if co.get("approved", True))


def current_budget_of(job):
    """Current working budget per category (approved change orders are
    a single total on main, so they surface separately)."""
    return {c: int((job.get("estimated_costs_cents") or {}).get(c, 0))
            for c in CATEGORIES}


# ------------------------------------------------------------------
# Committed (seam — see module docstring)
# ------------------------------------------------------------------
async def committed_for_job(tenant_id, job_id):
    cfg = await Scoped(db.job_costing_config, tenant_id).find_one(
        {"key": "job_costing"})
    if not cfg or not cfg.get("committed_costs_enabled"):
        return {"enabled": False, "by_category": _zeros(), "total_cents": 0}
    by_cat = _zeros()
    pos = await Scoped(db.purchase_orders, tenant_id).find_list(
        {"job_id": job_id, "status": {"$in": list(PO_COMMITTED_STATUSES)}})
    for po in pos:
        lines = po.get("lines") or []
        if lines:
            amt = sum(int(l.get("quantity", 0)) * int(l.get("unit_cost_cents", 0))
                      for l in lines)
        else:
            amt = int(po.get("total_cents", 0))
        by_cat["material"] += amt
    subs = await Scoped(db.job_commitments, tenant_id).find_list(
        {"job_id": job_id, "status": "approved"})
    for s in subs:
        by_cat["subcontractor"] += max(
            int(s.get("amount_cents", 0)) - int(s.get("fulfilled_cents", 0)), 0)
    return {"enabled": True, "by_category": by_cat,
            "total_cents": sum(by_cat.values())}


# ------------------------------------------------------------------
# Live forecast (features A, C, D; E via overrides)
# ------------------------------------------------------------------
def compute_forecast(job, costs, committed, overrides=None):
    """Pure math — no I/O. `costs` are the job's job_costs docs;
    `committed` is committed_for_job()'s payload; `overrides` maps
    category -> remaining cents for what-if (feature E)."""
    overrides = overrides or {}
    baseline = baseline_of(job)
    budget = current_budget_of(job)
    co_delta = change_order_delta_cents(job)

    actual = _zeros()
    phase_actual = {}
    for c in costs:
        cat = c.get("category", "other")
        if cat not in actual:
            cat = "other"
        actual[cat] += int(c.get("amount_cents", 0))
        pid = c.get("phase_id")
        if pid:
            phase_actual[pid] = phase_actual.get(pid, 0) + int(c.get("amount_cents", 0))

    rows = []
    for cat in CATEGORIES:
        committed_c = int(committed["by_category"].get(cat, 0))
        if cat in overrides:
            remaining = max(int(overrides[cat]), 0)
        else:
            remaining = max(budget[cat] - actual[cat] - committed_c, 0)
        rows.append({
            "category": cat,
            "baseline_cents": baseline["costs_cents"][cat],
            "budget_cents": budget[cat],
            "actual_cents": actual[cat],
            "committed_cents": committed_c,
            "remaining_cents": remaining,
            "forecast_cents": actual[cat] + committed_c + remaining,
            "drift_cents": (actual[cat] + committed_c + remaining)
                           - baseline["costs_cents"][cat],
        })

    total_actual = sum(r["actual_cents"] for r in rows)
    total_committed = committed["total_cents"]
    total_budget = sum(budget.values()) + co_delta
    total_baseline = sum(baseline["costs_cents"].values())
    # Totals MUST equal the sum of the category rows (plus the
    # total-level change-order delta): computing remaining at total
    # level instead would let one category's overrun silently eat
    # another category's remaining budget.
    total_remaining = max(sum(r["remaining_cents"] for r in rows) + co_delta, 0)
    total_forecast = total_actual + total_committed + total_remaining

    drivers = sorted(
        ({"category": r["category"], "baseline_cents": r["baseline_cents"],
          "forecast_cents": r["forecast_cents"], "drift_cents": r["drift_cents"]}
         for r in rows if r["drift_cents"] != 0),
        key=lambda d: abs(d["drift_cents"]), reverse=True)

    # Phases (only when the job carries them — phases-lane shape).
    phase_rows = []
    baseline_phases = {p["id"]: p for p in baseline["phases"]}
    for p in job.get("phases") or []:
        p_budget = sum(int((p.get("budget_cents") or {}).get(c, 0))
                       for c in CATEGORIES)
        p_base = baseline_phases.get(p["id"])
        p_baseline = (sum(int((p_base.get("budget_cents") or {}).get(c, 0))
                          for c in CATEGORIES) if p_base else p_budget)
        p_actual = phase_actual.get(p["id"], 0)
        p_remaining = max(p_budget - p_actual, 0)
        phase_rows.append({
            "phase_id": p["id"], "name": p.get("name", ""),
            "baseline_cents": p_baseline, "budget_cents": p_budget,
            "actual_cents": p_actual, "remaining_cents": p_remaining,
            "forecast_cents": p_actual + p_remaining,
            "drift_cents": (p_actual + p_remaining) - p_baseline,
        })

    return {
        "job_id": job["id"],
        "job_number": job.get("job_number"),
        "status": job.get("status"),
        "baseline": baseline,
        "baseline_total_cents": total_baseline,
        "rows": rows,
        "totals": {
            "baseline_cents": total_baseline,
            "budget_cents": total_budget,
            "actual_cents": total_actual,
            "committed_cents": total_committed,
            "remaining_cents": total_remaining,
            "forecast_cents": total_forecast,
            "drift_cents": total_forecast - total_baseline,
        },
        "change_order_delta_cents": co_delta,
        "committed_enabled": committed["enabled"],
        "drift_drivers": drivers,
        "phases": phase_rows,
        "what_if": bool(overrides),
    }


async def load_forecast(tenant_id, job, overrides=None):
    costs = await Scoped(db.job_costs, tenant_id).find_list({"job_id": job["id"]})
    committed = await committed_for_job(tenant_id, job["id"])
    return compute_forecast(job, costs, committed, overrides)


# ------------------------------------------------------------------
# History-smart suggestions (feature B)
# ------------------------------------------------------------------
async def budget_suggestion(tenant_id, est_costs_cents, exclude_job_id=None):
    """Per category: across the tenant's completed jobs, average
    (actual - original estimate) / original estimate. Fewer than
    MIN_HISTORY_JOBS comparable jobs in a category -> that category is
    silent. Never invents a number."""
    jobs = await Scoped(db.jobs, tenant_id).find_list(
        {"status": {"$in": list(COMPLETED_STATUSES)}})
    per_cat_samples = {c: [] for c in CATEGORIES}
    for j in jobs:
        if exclude_job_id and j["id"] == exclude_job_id:
            continue
        base = baseline_of(j)["costs_cents"]
        costs = await Scoped(db.job_costs, tenant_id).find_list(
            {"job_id": j["id"]})
        actual = _zeros()
        for c in costs:
            cat = c.get("category", "other")
            if cat not in actual:
                cat = "other"
            actual[cat] += int(c.get("amount_cents", 0))
        for cat in CATEGORIES:
            if base[cat] > 0:
                per_cat_samples[cat].append((actual[cat] - base[cat]) / base[cat])

    out = {}
    for cat in CATEGORIES:
        samples = per_cat_samples[cat]
        est = int((est_costs_cents or {}).get(cat, 0))
        if len(samples) < MIN_HISTORY_JOBS or est <= 0:
            continue
        avg = sum(samples) / len(samples)
        out[cat] = {
            "jobs": len(samples),
            "avg_variance_pct": round(avg * 100, 1),
            "estimate_cents": est,
            "suggested_cents": int(round(est * (1 + avg))),
        }
    return {"available": bool(out), "per_category": out,
            "min_history_jobs": MIN_HISTORY_JOBS}


# ------------------------------------------------------------------
# Forecast history (feature F)
# ------------------------------------------------------------------
def _snapshots(tenant_id):
    return Scoped(db.job_forecast_snapshots, tenant_id)


async def take_snapshot(tenant_id, user, job, forecast, reason):
    doc = {
        "id": new_id(),
        "job_id": job["id"],
        "job_number": job.get("job_number"),
        "reason": reason,  # material | manual | final | created
        "baseline_total_cents": forecast["baseline_total_cents"],
        "forecast_total_cents": forecast["totals"]["forecast_cents"],
        "actual_total_cents": forecast["totals"]["actual_cents"],
        "forecast_by_category": {r["category"]: r["forecast_cents"]
                                 for r in forecast["rows"]},
        "taken_by": (user or {}).get("id"),
        "taken_at": now_iso(),
    }
    await _snapshots(tenant_id).insert_one(doc)
    return doc


def _is_material(forecast, last):
    """Material = the forecast total moved, OR real money landed.
    (Under budget, a cost is absorbed by `remaining` and the forecast
    total does not move — the trail would go silent until overrun if
    it watched the total alone, so actuals movement counts too.)"""
    if last is None:
        return True
    base = forecast["baseline_total_cents"]
    threshold = max(MATERIAL_CENTS,
                    int(base * MATERIAL_PCT / 100) if base > 0 else 0)
    d_fc = abs(forecast["totals"]["forecast_cents"]
               - last["forecast_total_cents"])
    d_act = abs(forecast["totals"]["actual_cents"]
                - last["actual_total_cents"])
    return d_fc >= threshold or d_act >= threshold


async def maybe_snapshot(tenant_id, user, job, forecast):
    snaps = await _snapshots(tenant_id).find_list(
        {"job_id": job["id"]}, sort=[("taken_at", 1)])
    last = snaps[-1] if snaps else None
    if last is None:
        return await take_snapshot(tenant_id, user, job, forecast, "created")
    if _is_material(forecast, last):
        return await take_snapshot(tenant_id, user, job, forecast, "material")
    return None


async def forecast_history(tenant_id, job):
    snaps = await _snapshots(tenant_id).find_list(
        {"job_id": job["id"]}, sort=[("taken_at", 1)])
    final_actual = None
    if job.get("status") in COMPLETED_STATUSES:
        costs = await Scoped(db.job_costs, tenant_id).find_list(
            {"job_id": job["id"]})
        final_actual = sum(int(c.get("amount_cents", 0)) for c in costs)
    return {"job_id": job["id"],
            "baseline_total_cents":
                sum(baseline_of(job)["costs_cents"].values()),
            "snapshots": snaps,
            "final_actual_cents": final_actual}
