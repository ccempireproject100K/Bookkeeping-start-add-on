"""Job costing additions — committed costs + burdened labor
(owner-approved 2026-11-14, from the job-costing requests report #1/#2).

ITEM 1 — COMMITTED COSTS (optional, owner-ruled; default OFF):
  Tenant toggle `committed_costs_enabled`. When ON, a job's budget view
  counts money the shop has PROMISED to spend before the bills land:
    * purchase orders linked to the job (job_id on the PO) in an open
      state (approved / partially_received / received), and
    * approved subcontract commitments recorded against the job.
  When a PO converts to a bill, its amount leaves `committed` and
  joins the actual side as `billed_commitment_cents` — a PO is always
  in exactly one of the two buckets, so conversion never double
  counts. Subcontract commitments fulfil down as subcontractor job
  costs are posted against them. When the toggle is OFF, none of this
  is computed or attached — budget behaviour is exactly as before.

ITEM 2 — BURDENED LABOR (owner's "make it better" package):
  * Burden % (employer taxes, workers comp, insurance, benefits) is
    tenant-configurable: a default rate plus named TEMPLATES
    (e.g. "Employees", "Subs") assignable per worker.
  * Labor job costs record base / burden / burdened amounts. The
    LEDGER entry still posts the base wage exactly as before; burden
    is a costing overlay stored on the cost line.
  * Actual-cost learning: when payroll runs exist, the shop's real
    burden is computed from them (employer FICA match over gross
    wages) and OFFERED as a suggestion — never silently applied.
  * If burdened labor is the thing that pushes a job over its
    estimate, a notification fires in the shared `app_notifications`
    store (same shape the job-alerts lane uses), once per crossing.
  * The side-by-side labour VIEW (base / burdened / both) is a
    display option only — default "base" (today's behaviour); it
    never changes stored numbers.

Money math rounds half-up to whole cents (same as payroll).
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from db import db
from lib.base import Scoped, new_id, now_iso, today_str

LABOR_VIEWS = ("base", "burdened", "both")
PO_COMMITTED_STATUSES = ("approved", "partially_received", "received")
FICA_LABEL = "FICA — Social Security & Medicare (est.)"


def pct_cents(cents: int, pct: float) -> int:
    """pct% of cents, rounded half-up to whole cents."""
    if not cents or not pct:
        return 0
    return int((Decimal(cents) * Decimal(str(pct)) / 100).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------- tenant config ----------------

def _cfg_coll(tenant_id):
    return Scoped(db.job_costing_config, tenant_id)


async def get_config(tenant_id: str) -> dict:
    doc = await _cfg_coll(tenant_id).find_one({"key": "job_costing"})
    cfg = {"committed_costs_enabled": False, "labor_view": "base",
           "default_burden_pct": 0.0}
    if doc:
        if isinstance(doc.get("committed_costs_enabled"), bool):
            cfg["committed_costs_enabled"] = doc["committed_costs_enabled"]
        if doc.get("labor_view") in LABOR_VIEWS:
            cfg["labor_view"] = doc["labor_view"]
        if isinstance(doc.get("default_burden_pct"), (int, float)):
            cfg["default_burden_pct"] = float(doc["default_burden_pct"])
    return cfg


async def save_config(tenant_id: str, patch: dict) -> dict:
    coll = _cfg_coll(tenant_id)
    existing = await coll.find_one({"key": "job_costing"})
    doc = {"key": "job_costing", **patch, "updated_at": now_iso()}
    if existing:
        await coll.update_one({"key": "job_costing"}, {"$set": doc})
    else:
        await coll.insert_one(doc)
    return await get_config(tenant_id)


# ---------------- burden templates ----------------

def _tpl_coll(tenant_id):
    return Scoped(db.burden_templates, tenant_id)


async def list_templates(tenant_id: str) -> list:
    rows = await _tpl_coll(tenant_id).find_list(sort=[("created_at", 1)], limit=200)
    return [{k: v for k, v in r.items() if k not in ("_id", "tenant_id")}
            for r in rows]


async def resolve_burden(tenant_id: str, technician_id=None, cfg: dict = None):
    """(pct, template|None) for a worker: an assigned template wins,
    then the tenant default rate, then 0."""
    if technician_id:
        for t in await list_templates(tenant_id):
            if technician_id in (t.get("member_user_ids") or []):
                return float(t.get("pct") or 0), t
    cfg = cfg if cfg is not None else await get_config(tenant_id)
    return float(cfg.get("default_burden_pct") or 0), None


def labor_amounts(base_cents: int, pct: float) -> dict:
    burden = pct_cents(base_cents, pct)
    return {"base_cents": base_cents, "burden_pct": pct,
            "burden_cents": burden, "burdened_cents": base_cents + burden}


async def user_labor_basis(tenant_id: str, user_id: str):
    """(hourly_rate_cents, burden_pct, template_name) for a worker, or
    None when there is nothing honest to preview (no payroll hourly
    rate on file, or no burden configured for them)."""
    if not user_id:
        return None
    u = await db.users.find_one({"tenant_id": tenant_id, "id": user_id},
                                {"_id": 0, "pay_settings": 1})
    rate = ((u or {}).get("pay_settings") or {}).get("hourly_rate_cents") or 0
    if not rate:
        return None
    pct, tpl = await resolve_burden(tenant_id, user_id)
    if not pct:
        return None
    return rate, pct, (tpl["name"] if tpl else None)


async def entry_labor_preview(tenant_id: str, user_id: str, hours,
                              basis=None) -> dict | None:
    """Burdened-cost preview for a time entry: hours x the worker's
    payroll hourly rate, burden applied — the true cost is visible on
    the timesheet before anything posts to a job. None when there is
    nothing honest to show; the timesheet then renders exactly as it
    did before. `basis` may carry a cached user_labor_basis() result."""
    if not user_id or not hours:
        return None
    basis = basis if basis is not None else await user_labor_basis(
        tenant_id, user_id)
    if not basis:
        return None
    rate, pct, tpl_name = basis
    base = int((Decimal(rate) * Decimal(str(hours))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    out = labor_amounts(base, pct)
    out["hourly_rate_cents"] = rate
    out["template_name"] = tpl_name
    return out


# ---------------- actual burden from payroll ----------------

async def burden_suggestion(tenant_id: str) -> dict:
    """The shop's ACTUAL burden, learned from its own payroll runs:
    employer-side payroll taxes recorded in the runs (the employer
    FICA match, equal to the employee FICA estimate the runs carry)
    over gross wages. Prefers runs whose period ended in the last
    quarter; falls back to the most recent runs. Suggestion only —
    nothing here is ever applied without the owner choosing it."""
    rows = await Scoped(db.payroll_runs, tenant_id).find_list(
        {"status": {"$in": ["approved", "posted"]}},
        sort=[("period_end", -1)], limit=200)
    if not rows:
        return {"suggested_burden_pct": None,
                "note": "No payroll runs yet — run payroll and the "
                        "suggestion learns from your real numbers."}
    cutoff = (date.fromisoformat(today_str()) - timedelta(days=92)).isoformat()
    window = [r for r in rows if (r.get("period_end") or "") >= cutoff]
    basis = "payroll runs from the last quarter"
    if not window:
        window = rows[:3]
        basis = "the most recent payroll runs"
    gross = sum(i.get("gross_cents", 0) for r in window for i in r.get("items", []))
    fica = sum(d.get("amount_cents", 0)
               for r in window for i in r.get("items", [])
               for d in i.get("deductions", []) if d.get("label") == FICA_LABEL)
    if not gross:
        return {"suggested_burden_pct": None,
                "note": "Payroll runs carry no gross wages yet."}
    pct = round(fica / gross * 100, 2)
    return {
        "suggested_burden_pct": pct,
        "basis": basis,
        "runs_count": len(window),
        "gross_cents": gross,
        "employer_tax_cents": fica,
        "note": ("Employer payroll taxes (FICA match) recorded in your "
                 "own pay runs, over gross wages. Does not include "
                 "workers comp or benefits paid outside payroll — "
                 "raise the rate if you carry those separately."),
    }


# ---------------- committed costs ----------------

def _po_totals(po: dict):
    total = sum(l["quantity"] * l["unit_cost_cents"] for l in po.get("lines", []))
    received = sum(l.get("received_qty", 0) * l["unit_cost_cents"]
                   for l in po.get("lines", []))
    return total, received


async def job_commitments(tenant_id: str, job_id: str) -> list:
    rows = await Scoped(db.job_commitments, tenant_id).find_list(
        {"job_id": job_id}, sort=[("created_at", -1)], limit=500)
    return [{k: v for k, v in r.items() if k not in ("_id", "tenant_id")}
            for r in rows]


async def budget_view(tenant_id: str, job: dict, profit: dict,
                      cfg: dict = None) -> dict:
    """estimated / committed / actual / remaining for one job.
    Returns {"enabled": False} when the tenant toggle is off — the
    caller then attaches nothing and behaviour is exactly as before.

    No double count: a PO's money sits in `committed` while the PO is
    open, and in `billed_commitment_cents` (actual side) once billed —
    never both. Subcontract commitments count amount-minus-fulfilled
    as committed; the fulfilled part already lives in actual job
    costs, so it is not added again."""
    cfg = cfg if cfg is not None else await get_config(tenant_id)
    if not cfg["committed_costs_enabled"]:
        return {"enabled": False}
    pos = await Scoped(db.purchase_orders, tenant_id).find_list(
        {"job_id": job["id"]}, limit=500)
    committed_po = 0
    billed_po = 0
    po_lines = []
    for po in pos:
        total, received = _po_totals(po)
        status = po.get("status")
        if status in PO_COMMITTED_STATUSES:
            committed_po += total
            po_lines.append({"po_id": po["id"], "number": po.get("number"),
                             "vendor": po.get("vendor"), "status": status,
                             "bucket": "committed", "amount_cents": total})
        elif status == "billed":
            amt = po.get("billed_cents")
            if not isinstance(amt, int):
                amt = received  # billed before billed_cents was recorded
            billed_po += amt
            po_lines.append({"po_id": po["id"], "number": po.get("number"),
                             "vendor": po.get("vendor"), "status": status,
                             "bucket": "actual", "amount_cents": amt})
    commitments = await job_commitments(tenant_id, job["id"])
    committed_sub = sum(
        max(0, c["amount_cents"] - (c.get("fulfilled_cents") or 0))
        for c in commitments if c.get("status") == "approved")
    committed = committed_po + committed_sub
    actual_posted = profit.get("actual_cost_cents", 0) or 0
    actual_total = actual_posted + billed_po
    estimated = profit.get("estimated_cost_cents", 0) or 0
    return {
        "enabled": True,
        "estimated_cost_cents": estimated,
        "committed_cents": committed,
        "committed_po_cents": committed_po,
        "committed_subcontract_cents": committed_sub,
        "actual_cost_cents": actual_posted,
        "billed_commitment_cents": billed_po,
        "actual_total_cents": actual_total,
        "remaining_cents": estimated - actual_total - committed,
        "purchase_orders": po_lines,
        "commitments": commitments,
    }


# ---------------- burden overrun flag ----------------

def _state_coll(tenant_id):
    return Scoped(db.job_costing_state, tenant_id)


async def evaluate_burden_overrun(tenant_id: str, job: dict, profit: dict,
                                  actor_id: str = None) -> dict:
    """Fire ONE notification when burdened labor is the tipping point
    that puts a job over its estimate (base actual still at/under,
    burdened actual over). Same app_notifications shape the job-alerts
    lane writes. The fired mark clears when the job is no longer in
    that state, so a genuine re-tip fires again. Jobs with no
    estimate stay silent."""
    est = profit.get("estimated_cost_cents", 0) or 0
    actual = profit.get("actual_cost_cents", 0) or 0
    burden_total = profit.get("labor_burden_cents", 0) or 0
    burdened = actual + burden_total
    tipped = est > 0 and burden_total > 0 and actual <= est < burdened

    coll = _state_coll(tenant_id)
    state = await coll.find_one({"job_id": job["id"]}) or {}
    fired = bool(state.get("burden_over_fired"))
    out = {"tipped": tipped, "fired_now": False,
           "burdened_cost_cents": burdened}
    if tipped and not fired:
        doc = {
            "id": new_id(), "user_id": None, "tenant_wide": True,
            "kind": "job_budget_alert", "level": "overrun",
            "source": "burden",
            "job_id": job["id"], "job_number": job.get("job_number"),
            "title": f"{job.get('job_number')} over estimate with labor burden",
            "body": (f"{job.get('job_number')} ({job.get('customer_name')}) "
                     f"fits its ${est / 100:,.2f} estimate on wages alone "
                     f"(${actual / 100:,.2f}), but fully burdened labor "
                     f"brings true cost to ${burdened / 100:,.2f}. "
                     f"Flagged while there is still time to adjust."),
            "link": "/jobs", "read": False,
            "dedupe_key": f"job:{job['id']}:burden-overrun:{now_iso()}",
            "created_by": actor_id, "created_at": now_iso(),
        }
        await Scoped(db.app_notifications, tenant_id).insert_one(doc)
        out["fired_now"] = True
        out["notification_id"] = doc["id"]
        fired = True
    elif not tipped and fired:
        fired = False
    if fired != bool(state.get("burden_over_fired")) or not state:
        patch = {"job_id": job["id"], "burden_over_fired": fired,
                 "updated_at": now_iso()}
        if state:
            await coll.update_one({"job_id": job["id"]}, {"$set": patch})
        else:
            await coll.insert_one(patch)
    return out
