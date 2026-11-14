"""Job budget watch — cost-overrun alerts on top of the jobs module
(owner-approved 2026-11-14, Top-20 walk item #9).

Watches each job's recorded costs against its estimate (estimated costs
incl. approved change orders, exactly as jobs._profit computes them):

  * WARNING  — costs cross the warning threshold (default 90% of the
    estimate) while the job is still ACTIVE (open).
  * OVERRUN  — costs cross the overrun threshold (default 100%) while
    the job is active or completed (not closed).

Thresholds are configurable per tenant (job_alert_config collection)
and per job (alert_warning_pct / alert_overrun_pct on the job doc; the
per-job value wins). The tenant config also carries an `enabled`
switch — the tenant's notification setting for this lane. When it is
off, no notifications are written (the budget state is still computed
for the UI flag).

Alerts land in the in-app `app_notifications` store, re-implementing
the shape the fold-in branch built for the Invoice Assistant (kind /
title / body / link / read / dedupe_key) scoped per tenant. Job alerts
are tenant-wide (user_id None + tenant_wide True): everyone on the
team sees the same budget warning. A true phone-push transport can
subscribe to the same inserts later — labeled seam, not faked here.

Dedupe: a per-job state doc (job_alert_state) remembers which
thresholds have fired. A threshold fires ONCE per crossing; when
costs are corrected back under it (a cost removed / estimate raised
via change order), the fired mark clears and a later re-crossing may
fire again. No nagging on every new cost line.

Honest states: a job with no estimate (estimated cost 0) has NO
budget machinery — pct_used is None, level is None, nothing fires.
"""
from db import db
from lib.base import Scoped, new_id, now_iso

DEFAULT_WARNING_PCT = 90
DEFAULT_OVERRUN_PCT = 100

# Job statuses in which each alert level may fire. "estimate" jobs
# cannot carry costs (the API refuses), so they never cross in
# practice; "closed" jobs are done and stay silent.
WARNING_STATUSES = ("active",)
OVERRUN_STATUSES = ("active", "completed")


def _cfg_coll(tenant_id):
    return Scoped(db.job_alert_config, tenant_id)


def _state_coll(tenant_id):
    return Scoped(db.job_alert_state, tenant_id)


def _notif_coll(tenant_id):
    return Scoped(db.app_notifications, tenant_id)


# ---------------- config ----------------

async def get_config(tenant_id: str) -> dict:
    """Effective tenant thresholds: stored config over the defaults."""
    doc = await _cfg_coll(tenant_id).find_one({"key": "job_alerts"})
    cfg = {"warning_pct": DEFAULT_WARNING_PCT,
           "overrun_pct": DEFAULT_OVERRUN_PCT,
           "enabled": True}
    if doc:
        if isinstance(doc.get("warning_pct"), (int, float)):
            cfg["warning_pct"] = doc["warning_pct"]
        if isinstance(doc.get("overrun_pct"), (int, float)):
            cfg["overrun_pct"] = doc["overrun_pct"]
        if isinstance(doc.get("enabled"), bool):
            cfg["enabled"] = doc["enabled"]
    cfg["defaults"] = {"warning_pct": DEFAULT_WARNING_PCT,
                       "overrun_pct": DEFAULT_OVERRUN_PCT}
    return cfg


async def save_config(tenant_id: str, warning_pct, overrun_pct,
                      enabled) -> dict:
    doc = {"key": "job_alerts", "warning_pct": warning_pct,
           "overrun_pct": overrun_pct, "enabled": enabled,
           "updated_at": now_iso()}
    existing = await _cfg_coll(tenant_id).find_one({"key": "job_alerts"})
    if existing:
        await _cfg_coll(tenant_id).update_one(
            {"key": "job_alerts"}, {"$set": doc})
    else:
        await _cfg_coll(tenant_id).insert_one(doc)
    return await get_config(tenant_id)


def effective_thresholds(job: dict, cfg: dict) -> tuple:
    """(warning_pct, overrun_pct) — per-job override wins over tenant."""
    warn = job.get("alert_warning_pct")
    over = job.get("alert_overrun_pct")
    warn = warn if isinstance(warn, (int, float)) else cfg["warning_pct"]
    over = over if isinstance(over, (int, float)) else cfg["overrun_pct"]
    return warn, over


# ---------------- budget state (pure compute, no firing) ----------------

def budget_state(job: dict, profit: dict, cfg: dict) -> dict:
    """Budget view for API/UI. has_estimate False => no machinery:
    pct_used and level are None and the UI shows no flag."""
    warn, over = effective_thresholds(job, cfg)
    est = profit.get("estimated_cost_cents", 0) or 0
    actual = profit.get("actual_cost_cents", 0) or 0
    if est <= 0:
        return {"has_estimate": False, "estimated_cost_cents": est,
                "actual_cost_cents": actual, "pct_used": None,
                "level": None, "warning_pct": warn, "overrun_pct": over}
    pct = round(actual / est * 100, 2)
    level = None
    if pct >= over:
        level = "overrun"
    elif pct >= warn:
        level = "warning"
    return {"has_estimate": True, "estimated_cost_cents": est,
            "actual_cost_cents": actual, "pct_used": pct,
            "level": level, "warning_pct": warn, "overrun_pct": over}


# ---------------- evaluation + firing ----------------

async def _get_fired(tenant_id: str, job_id: str) -> dict:
    doc = await _state_coll(tenant_id).find_one({"job_id": job_id})
    return dict((doc or {}).get("fired") or {})


async def _save_fired(tenant_id: str, job_id: str, fired: dict):
    coll = _state_coll(tenant_id)
    existing = await coll.find_one({"job_id": job_id})
    doc = {"job_id": job_id, "fired": fired, "updated_at": now_iso()}
    if existing:
        await coll.update_one({"job_id": job_id}, {"$set": doc})
    else:
        await coll.insert_one(doc)


async def evaluate_alerts(tenant_id: str, job: dict, profit: dict,
                          actor_id: str = None) -> dict:
    """Reconcile the job's crossings against the fired-state and write
    a notification for each NEW crossing. Returns
    {"budget": <state>, "fired": [ {level, threshold_pct, pct_used} ]}.

    Safe to call after every cost / change-order / status mutation:
    already-fired thresholds stay silent until costs drop back under
    them. Writes nothing at all when the tenant lane is disabled or
    the job has no estimate.
    """
    cfg = await get_config(tenant_id)
    budget = budget_state(job, profit, cfg)
    fired_now = []
    if not budget["has_estimate"]:
        # No estimate: honest silence. Clear any stale fired marks so
        # a later estimate starts from a clean slate.
        existing = await _state_coll(tenant_id).find_one(
            {"job_id": job["id"]})
        if existing and existing.get("fired"):
            await _save_fired(tenant_id, job["id"], {})
        return {"budget": budget, "fired": fired_now}

    warn, over = effective_thresholds(job, cfg)
    pct = budget["pct_used"]
    status = job.get("status")
    fired = await _get_fired(tenant_id, job["id"])

    levels = [(warn, "warning", WARNING_STATUSES),
              (over, "overrun", OVERRUN_STATUSES)]
    changed = False
    for threshold, level, statuses in levels:
        key = f"{level}:{threshold}"
        if pct < threshold:
            # Back under (correction / estimate raised): clear the
            # mark so a genuine re-crossing may fire again.
            if key in fired:
                fired.pop(key)
                changed = True
            continue
        if key in fired:
            continue  # already fired for this crossing — dedupe
        if status not in statuses:
            continue  # e.g. warning only fires while the job is open
        if not cfg["enabled"]:
            # Lane off: stay silent AND leave the crossing unmarked, so
            # turning the lane on while still crossed fires it once.
            continue
        fired[key] = {"level": level, "threshold_pct": threshold,
                      "pct_used": pct, "fired_at": now_iso()}
        changed = True
        verb = ("is over its estimate" if level == "overrun"
                else "is closing in on its estimate")
        doc = {
            "id": new_id(), "user_id": None, "tenant_wide": True,
            "kind": "job_budget_alert", "level": level,
            "job_id": job["id"], "job_number": job.get("job_number"),
            "title": (f"{job.get('job_number')} budget "
                      f"{'overrun' if level == 'overrun' else 'warning'}"),
            "body": (f"{job.get('job_number')} ({job.get('customer_name')}) "
                     f"{verb}: costs are at {pct}% of the "
                     f"${budget['estimated_cost_cents'] / 100:,.2f} estimate "
                     f"(${budget['actual_cost_cents'] / 100:,.2f} so far)."),
            "link": "/jobs", "read": False,
            "dedupe_key": f"job:{job['id']}:{key}:{fired[key]['fired_at']}",
            "threshold_pct": threshold, "pct_used": pct,
            "created_by": actor_id, "created_at": now_iso(),
        }
        await _notif_coll(tenant_id).insert_one(doc)
        fired_now.append({"level": level, "threshold_pct": threshold,
                          "pct_used": pct, "notification_id": doc["id"]})
    if changed:
        await _save_fired(tenant_id, job["id"], fired)
    return {"budget": budget, "fired": fired_now}
