"""Job phase / cost-code costing (owner-approved 2026-11-14, job-costing
ask #4 — the owner's better-package: per-phase budgeted-vs-actual tied to
the estimate, drift flags, and field entries taggable to a phase).

A job may be broken into named phases / cost codes (tenant-defined per
job: rough-in, trim-out; framing, electrical …). Phases live on the job
doc as `phases: [{id, name, budget_cents: {category: cents}}]`. The five
flat cost categories stay as the cost-TYPE dimension — every cost line
is job x phase x category. Jobs without phases behave exactly as before:
this module returns an empty view and fires nothing.

Phase budgets are drawn from the job estimate, so they are sum-checked
against it (estimated costs incl. approved change orders, exactly as
jobs._profit computes the total). A mismatch is a loud WARNING in the
API payload (`budget_check.warning`), never a silent one, and never a
hard block — phased jobs are often budgeted incrementally.

Drift flags mirror the overrun lane (lib/jobalerts on
feat/job-overrun-alerts — same defaults, same tenant config collection
`job_alert_config`, same `app_notifications` shape kind/title/body/link/
read/dedupe_key, tenant-wide). A phase is flagged when it is:
  * OVERRUN — actual >= overrun threshold (default 100% of phase budget)
  * WARNING — actual >= warning threshold (default 90%)
  * DRIFT   — burning disproportionately fast: phase >= 50% used while
              leading the job's overall % used by >= 25 points
Flags fire once per level per crossing: a state doc
(`job_phase_alert_state`) remembers the fired level; it clears when the
phase cools, so a genuine re-crossing may fire again. "closed" jobs and
budget-less phases stay silent.

Committed column: the committed-costs lane (feat/job-costing-committed-
burden, lib/jobcosting.budget_view) computes committed at JOB level from
open purchase orders + approved subcontract commitments. Its docs carry
no phase dimension today; this module reads the same sources and
attributes an amount to a phase the moment a PO / commitment doc
carries `phase_id` — the column slots in with no change needed here.

Field flow: time entries and receipts that link to a job may also carry
`phase_id` (field name fixed by the field-capture lane's convention).
`record_time_cost` / `record_receipt_cost` below are the seam where a
tagged field entry lands in the phase's actuals.
"""
from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib import ledger as L
from lib import posting as P

DEFAULT_WARNING_PCT = 90
DEFAULT_OVERRUN_PCT = 100
DRIFT_GAP_PCT = 25        # phase may lead the job's overall burn by this
DRIFT_MIN_PCT = 50        # drift only counts once a phase is this far in
# Same committed semantics as the committed-costs lane (lib/jobcosting).
PO_COMMITTED_STATUSES = ("approved", "partially_received", "received")
LABOR_DEBIT = "5005"      # mirrors jobs.CATEGORY_ACCOUNT["labor"]
WAGES_PAYABLE = "2320"    # mirrors jobs.add_cost labour credit


def _notif_coll(tenant_id):
    return Scoped(db.app_notifications, tenant_id)


def _state_coll(tenant_id):
    return Scoped(db.job_phase_alert_state, tenant_id)


# ---------------- thresholds ----------------

async def get_thresholds(tenant_id: str) -> tuple:
    """(warning_pct, overrun_pct) — the overrun lane's tenant config when
    present, else the shared defaults."""
    warn, over = DEFAULT_WARNING_PCT, DEFAULT_OVERRUN_PCT
    try:
        doc = await Scoped(db.job_alert_config, tenant_id).find_one(
            {"key": "job_alerts"})
    except Exception:
        doc = None
    if doc:
        if isinstance(doc.get("warning_pct"), (int, float)):
            warn = doc["warning_pct"]
        if isinstance(doc.get("overrun_pct"), (int, float)):
            over = doc["overrun_pct"]
    return warn, over


# ---------------- phases on the job doc ----------------

def phase_of(job: dict, phase_id: str):
    for ph in job.get("phases") or []:
        if ph.get("id") == phase_id:
            return ph
    return None


def phase_budget_total(phase: dict) -> int:
    return sum((phase.get("budget_cents") or {}).values())


def budget_check(job: dict, est_cost_cents: int) -> dict:
    """Sum-check phase budgets against the job estimate total."""
    phases = job.get("phases") or []
    total = sum(phase_budget_total(p) for p in phases)
    diff = total - (est_cost_cents or 0)
    warning = None
    if phases and diff != 0:
        direction = "over" if diff > 0 else "under"
        warning = (
            f"Phase budgets total ${total / 100:,.2f} but the job "
            f"estimate is ${(est_cost_cents or 0) / 100:,.2f} — "
            f"{direction} by ${abs(diff) / 100:,.2f}. Phase budgets "
            f"should add up to the job estimate.")
    return {
        "has_phases": bool(phases),
        "phases_budget_cents": total,
        "job_estimate_cents": est_cost_cents or 0,
        "difference_cents": diff,
        "reconciled": not phases or diff == 0,
        "warning": warning,
    }


# ---------------- committed (slots in from the committed lane) ----------

def _po_total(po: dict) -> int:
    return sum(l["quantity"] * l["unit_cost_cents"]
               for l in po.get("lines", []))


async def committed_by_phase(tenant_id: str, job: dict) -> dict:
    """{phase_id: committed_cents} from the committed lane's sources.
    Job-level commitments without a phase_id stay out of the per-phase
    columns (they are already in that lane's job-level budget view)."""
    out = {}
    pos = await Scoped(db.purchase_orders, tenant_id).find_list(
        {"job_id": job["id"]}, limit=500)
    for po in pos:
        pid = po.get("phase_id")
        if pid and po.get("status") in PO_COMMITTED_STATUSES:
            out[pid] = out.get(pid, 0) + _po_total(po)
    comms = await Scoped(db.job_commitments, tenant_id).find_list(
        {"job_id": job["id"]}, limit=500)
    for c in comms:
        pid = c.get("phase_id")
        if pid and c.get("status") == "approved":
            open_amt = max(0, c["amount_cents"] - (c.get("fulfilled_cents") or 0))
            out[pid] = out.get(pid, 0) + open_amt
    return out


# ---------------- variance view (pure compute) ----------------

def phase_variance(job: dict, costs: list, est_cost_cents: int,
                   warn: float, over: float,
                   committed_map: dict = None) -> dict:
    """Per-phase budgeted vs actual vs committed, % used, drift level.
    Pure: no I/O, no firing — evaluate_phase_drift does the firing."""
    committed_map = committed_map or {}
    phases = job.get("phases") or []
    total_actual = sum(c["amount_cents"] for c in costs)
    job_pct = (round(total_actual / est_cost_cents * 100, 2)
               if est_cost_cents and est_cost_cents > 0 else None)
    closed = job.get("status") == "closed"

    out = []
    for ph in phases:
        pcosts = [c for c in costs if c.get("phase_id") == ph["id"]]
        actual = sum(c["amount_cents"] for c in pcosts)
        by_cat, budget_by_cat = {}, dict(ph.get("budget_cents") or {})
        for c in pcosts:
            by_cat[c["category"]] = by_cat.get(c["category"], 0) + c["amount_cents"]
        budget = phase_budget_total(ph)
        pct = round(actual / budget * 100, 2) if budget > 0 else None
        level = None
        if budget > 0 and not closed and pct is not None:
            if pct >= over:
                level = "overrun"
            elif pct >= warn:
                level = "warning"
            elif (pct >= DRIFT_MIN_PCT and job_pct is not None
                    and pct - job_pct >= DRIFT_GAP_PCT):
                level = "drift"
        out.append({
            "phase_id": ph["id"], "name": ph.get("name", ""),
            "budget_cents": budget, "budget_by_category_cents": budget_by_cat,
            "actual_cents": actual, "actual_by_category_cents": by_cat,
            "committed_cents": committed_map.get(ph["id"], 0),
            "variance_cents": actual - budget,
            "pct_used": pct,
            "drift": level is not None, "level": level,
        })

    unphased = [c for c in costs if not c.get("phase_id")]
    return {
        "phases": out,
        "unphased": {
            "actual_cents": sum(c["amount_cents"] for c in unphased),
            "cost_count": len(unphased),
        },
        "job_pct_used": job_pct,
        "budget_check": budget_check(job, est_cost_cents),
    }


# ---------------- drift notifications ----------------

async def evaluate_phase_drift(tenant_id: str, job: dict,
                               variance: dict) -> list:
    """Fire in-app notifications for phases whose drift level changed.
    Mirrors the overrun lane: once per level per crossing, state clears
    when the phase cools so a re-crossing may fire again."""
    fired_now = []
    for pv in variance.get("phases", []):
        state = await _state_coll(tenant_id).find_one(
            {"job_id": job["id"], "phase_id": pv["phase_id"]})
        prev = state.get("fired_level") if state else None
        level = pv["level"]
        if level == prev:
            continue
        if state:
            await _state_coll(tenant_id).update_one(
                {"job_id": job["id"], "phase_id": pv["phase_id"]},
                {"$set": {"fired_level": level, "updated_at": now_iso()}})
        else:
            await _state_coll(tenant_id).insert_one(
                {"job_id": job["id"], "phase_id": pv["phase_id"],
                 "fired_level": level, "updated_at": now_iso()})
        if not level:
            continue
        if level == "overrun":
            title = f"{job.get('job_number')} phase over budget"
            body = (f"Phase '{pv['name']}' on {job.get('job_number')} "
                    f"({job.get('customer_name')}) is over its budget: "
                    f"${pv['actual_cents'] / 100:,.2f} against "
                    f"${pv['budget_cents'] / 100:,.2f} "
                    f"({pv['pct_used']}% used).")
        elif level == "warning":
            title = f"{job.get('job_number')} phase closing in on budget"
            body = (f"Phase '{pv['name']}' on {job.get('job_number')} "
                    f"({job.get('customer_name')}) is at "
                    f"{pv['pct_used']}% of its budget "
                    f"(${pv['actual_cents'] / 100:,.2f} of "
                    f"${pv['budget_cents'] / 100:,.2f}).")
        else:
            title = f"{job.get('job_number')} phase burning fast"
            body = (f"Phase '{pv['name']}' on {job.get('job_number')} "
                    f"({job.get('customer_name')}) is at "
                    f"{pv['pct_used']}% of its budget while the job "
                    f"overall is at {variance.get('job_pct_used')}% — "
                    f"this phase is running hot.")
        doc = {
            "id": new_id(), "user_id": None, "tenant_wide": True,
            "kind": "job_phase_drift", "level": level,
            "job_id": job["id"], "job_number": job.get("job_number"),
            "phase_id": pv["phase_id"], "phase_name": pv["name"],
            "title": title, "body": body,
            "link": "/jobs", "read": False,
            "dedupe_key": f"phase:{job['id']}:{pv['phase_id']}:{level}",
            "pct_used": pv["pct_used"],
            "created_by": None, "created_at": now_iso(),
        }
        await _notif_coll(tenant_id).insert_one(doc)
        fired_now.append({"phase_id": pv["phase_id"], "level": level,
                          "notification_id": doc["id"]})
    return fired_now


# ---------------- field-entry flow into phase actuals ----------------

async def record_time_cost(tenant_id: str, user: dict, entry: dict,
                           job: dict) -> dict:
    """An approved time entry tagged to a job lands as a labour job cost
    (hours x the tech's pay rate), carrying the entry's phase_id.
    Idempotent per entry. Returns the cost doc, or None when there is
    nothing to post (no hours / no pay rate configured — the tag still
    lives on the entry itself). The journal entry mirrors the jobs
    module's own labour posting (Dr labour / Cr wages payable)."""
    existing = await Scoped(db.job_costs, tenant_id).find_one(
        {"source": "time_entry", "source_ref": entry["id"]})
    if existing:
        return existing
    hours = entry.get("hours") or 0
    member = await db.users.find_one(
        {"id": entry["user_id"], "tenant_id": tenant_id},
        {"_id": 0, "pay_settings": 1, "hourly_rate_cents": 1})
    rate_cents = 0
    if member:
        rate_cents = ((member.get("pay_settings") or {}).get(
            "hourly_rate_cents") or member.get("hourly_rate_cents") or 0)
    amount_cents = to_cents(hours * (rate_cents / 100.0)) if rate_cents else 0
    if hours <= 0 or amount_cents <= 0:
        return None
    cost_id = new_id()
    entry_doc = await L.post_journal_entry(db, tenant_id, user, {
        "entry_date": entry.get("date") or today_str(),
        "source": "job_cost", "source_ref": cost_id,
        "memo": f"{job['job_number']} labor: time entry {entry['id']}".strip(),
        "lines": [P.dr(LABOR_DEBIT, amount_cents, "Labour — time entry"),
                  P.cr(WAGES_PAYABLE, amount_cents, "Job cost")],
    }, status="posted")
    doc = {
        "id": cost_id, "job_id": job["id"], "job_number": job["job_number"],
        "category": "labor",
        "description": entry.get("notes") or "Time entry",
        "amount_cents": amount_cents, "hours": hours, "rate": rate_cents,
        "product_id": None, "qty": 1,
        "technician_id": entry["user_id"],
        "phase_id": entry.get("phase_id"),
        "source": "time_entry", "source_ref": entry["id"],
        "entry_number": entry_doc["number"], "created_at": now_iso(),
    }
    await Scoped(db.job_costs, tenant_id).insert_one(doc)
    doc.pop("_id", None)
    return doc


async def record_receipt_cost(tenant_id: str, user: dict, expense_doc: dict,
                              job: dict, phase_id, category: str) -> dict:
    """A posted receipt tagged to a job lands in the job's actuals,
    carrying its phase_id. The receipt's own expense journal entry is
    the GL posting (no second entry); this cost doc references it, so
    the amount counts exactly once in the ledger and once in job
    costing. Idempotent per expense."""
    existing = await Scoped(db.job_costs, tenant_id).find_one(
        {"source": "receipt", "source_ref": expense_doc["id"]})
    if existing:
        return existing
    doc = {
        "id": new_id(), "job_id": job["id"], "job_number": job["job_number"],
        "category": category,
        "description": expense_doc.get("description")
                       or expense_doc.get("vendor") or "Receipt",
        "amount_cents": expense_doc["amount_cents"],
        "hours": 0, "rate": 0, "product_id": None, "qty": 1,
        "technician_id": None,
        "phase_id": phase_id,
        "source": "receipt", "source_ref": expense_doc["id"],
        "entry_number": expense_doc["entry_number"],
        "created_at": now_iso(),
    }
    await Scoped(db.job_costs, tenant_id).insert_one(doc)
    doc.pop("_id", None)
    return doc
