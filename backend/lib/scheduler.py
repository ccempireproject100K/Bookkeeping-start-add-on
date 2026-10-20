"""In-app scheduler for background jobs (APScheduler).

Runs four jobs: the daily collections autopilot, the daily FX-rate
refresh, an hourly recurring-invoice materializer, and a periodic Plaid
bank-transaction sync. The scheduler is guarded three ways:

1. It only starts when ``ENABLE_SCHEDULER=true`` (default off).
2. A MongoDB distributed lock (``scheduler_locks``) means only one app
   instance runs the schedule; the lock self-heals via TTL if an
   instance dies.
3. Each tenant gets at most one run per job window (``scheduler_runs``),
   so overlapping or retried schedules can't double-process a workspace.
   The jobs themselves are also idempotent: the autopilot records
   ``autopilot_stages`` per invoice and caps late fees to one per month;
   recurring invoices only generate when ``next_run`` has arrived and
   advance it when they do; the bank sync resumes from the stored Plaid
   cursor and dedupes on the Plaid transaction id. FX-rate storage is
   upsert-based, so re-runs are no-ops.

Env vars:
    ENABLE_SCHEDULER      "true" to start the scheduler (default "false")
    COLLECTIONS_RUN_TIME  "HH:MM" 24h daily run time (default "06:00")
    FX_RATES_ENABLED      "true" to run the FX job (default "true")
    FX_RATES_RUN_TIME     "HH:MM" 24h daily run time (default "07:00")
    RECURRING_INVOICES_ENABLED  "true" to run the recurring-invoice job
                          (default "true")
    BANK_SYNC_ENABLED     "true" to run the bank sync job (default "true")
    BANK_SYNC_INTERVAL_HOURS  hours between bank sync sweeps (default 6)
    SCHEDULER_TZ          IANA timezone for the schedule
                          (default "America/Chicago")
"""
import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from db import db
from lib.base import now_iso, today_str
from lib.fx import refresh_rates
from routers.collections import autopilot_run, get_config

logger = logging.getLogger("mindatease.scheduler")

LOCK_ID = "collections-scheduler"
LOCK_TTL_SECONDS = 900  # 15 min; the heartbeat renews every 5
JOB_ID = "collections-autopilot"
FX_JOB_ID = "fx-rates-daily"
RECURRING_JOB_ID = "recurring-invoices"
BANK_SYNC_JOB_ID = "bank-sync"

_state = {
    "running": False,
    "instance_id": None,
    "next_run": None,
    "last_outcome": None,
    "fx_next_run": None,
    "fx_last_outcome": None,
    "recurring_next_run": None,
    "recurring_last_outcome": None,
    "bank_next_run": None,
    "bank_last_outcome": None,
}

_scheduler: AsyncIOScheduler | None = None
_heartbeat_task: asyncio.Task | None = None


def scheduler_enabled() -> bool:
    return os.environ.get("ENABLE_SCHEDULER", "false").lower() == "true"


def _run_time() -> tuple:
    raw = os.environ.get("COLLECTIONS_RUN_TIME", "06:00")
    return _parse_hhmm(raw, "COLLECTIONS_RUN_TIME", (6, 0))


def _parse_hhmm(raw: str, name: str, default: tuple) -> tuple:
    try:
        hour, minute = raw.split(":")
        hour, minute = int(hour), int(minute)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    logger.warning("Bad %s=%r, using %02d:%02d", name, raw, *default)
    return default


def fx_rates_enabled() -> bool:
    return os.environ.get("FX_RATES_ENABLED", "true").lower() == "true"


def _fx_run_time() -> tuple:
    raw = os.environ.get("FX_RATES_RUN_TIME", "07:00")
    return _parse_hhmm(raw, "FX_RATES_RUN_TIME", (7, 0))


def _tz() -> str:
    return os.environ.get("SCHEDULER_TZ", "America/Chicago")


def recurring_invoices_enabled() -> bool:
    return os.environ.get("RECURRING_INVOICES_ENABLED", "true").lower() == "true"


def bank_sync_enabled() -> bool:
    return os.environ.get("BANK_SYNC_ENABLED", "true").lower() == "true"


def bank_sync_interval_hours() -> int:
    try:
        hours = int(os.environ.get("BANK_SYNC_INTERVAL_HOURS", "6"))
        if hours >= 1:
            return hours
    except (TypeError, ValueError):
        pass
    logger.warning("Bad BANK_SYNC_INTERVAL_HOURS=%r, using 6",
                   os.environ.get("BANK_SYNC_INTERVAL_HOURS"))
    return 6


# ---------------- single-instance lock ----------------
async def _acquire_lock(instance_id: str) -> bool:
    now_ts = datetime.now(timezone.utc).timestamp()
    doc = await db.scheduler_locks.find_one({"_id": LOCK_ID})
    if (doc and doc.get("expires_at_ts", 0) > now_ts
            and doc.get("instance_id") != instance_id):
        return False
    await db.scheduler_locks.update_one(
        {"_id": LOCK_ID},
        {"$set": {"instance_id": instance_id,
                  "acquired_at": now_iso(),
                  "expires_at_ts": now_ts + LOCK_TTL_SECONDS}},
        upsert=True)
    # Re-read: if two instances raced, only the winner's id survives.
    doc = await db.scheduler_locks.find_one({"_id": LOCK_ID})
    return bool(doc) and doc.get("instance_id") == instance_id


async def _renew_lock(instance_id: str) -> bool:
    now_ts = datetime.now(timezone.utc).timestamp()
    res = await db.scheduler_locks.update_one(
        {"_id": LOCK_ID, "instance_id": instance_id},
        {"$set": {"expires_at_ts": now_ts + LOCK_TTL_SECONDS}})
    return res.modified_count == 1


async def _heartbeat(instance_id: str):
    try:
        while True:
            await asyncio.sleep(LOCK_TTL_SECONDS // 3)
            if not await _renew_lock(instance_id):
                logger.error("Scheduler lock lost; stopping the scheduler.")
                await stop()
                return
    except asyncio.CancelledError:
        pass


# ---------------- the daily job ----------------
def _system_user(tenant_id: str, name: str = "Collections scheduler") -> dict:
    return {"id": "scheduler", "tenant_id": tenant_id,
            "email": "scheduler@system", "name": name}


async def run_collections_autopilot() -> dict:
    """Run the collections autopilot for every workspace that enabled it.

    Calls the same code path as the owner's manual Run button
    (``routers.collections.autopilot_run``). Safe to re-run: tenants
    already processed today are skipped, and the autopilot records
    per-invoice stages so nothing fires twice.
    """
    run_date = today_str()
    outcome = {"date": run_date, "tenants": 0, "actions": 0,
               "errors": 0, "skipped": []}
    tenants = await db.tenants.find({}, {"id": 1}).to_list(1000)
    for t in tenants:
        tid = t["id"]
        user = _system_user(tid)
        try:
            cfg = await get_config(user)
        except Exception as exc:
            logger.warning("Scheduler: config read failed for tenant %s: %s",
                           tid, exc)
            outcome["skipped"].append({"tenant_id": tid,
                                      "reason": "config_error"})
            continue
        if not cfg.get("enabled"):
            continue
        # Idempotency: claim today's run for this tenant atomically.
        run_id = f"{JOB_ID}:{tid}:{run_date}"
        try:
            await db.scheduler_runs.insert_one({
                "id": run_id, "job": JOB_ID, "tenant_id": tid,
                "run_date": run_date, "started_at": now_iso(),
                "status": "running"})
        except DuplicateKeyError:
            outcome["skipped"].append({"tenant_id": tid,
                                      "reason": "already_ran_today"})
            continue
        try:
            res = await autopilot_run(user)
            actions = len(res.get("actions", []))
            errors = len(res.get("errors", []))
            outcome["tenants"] += 1
            outcome["actions"] += actions
            outcome["errors"] += errors
            await db.scheduler_runs.update_one(
                {"id": run_id},
                {"$set": {"status": "done", "finished_at": now_iso(),
                          "actions": actions, "errors": errors}})
            logger.info("Collections autopilot: tenant %s actions=%d errors=%d",
                        tid, actions, errors)
        except HTTPException as exc:
            await db.scheduler_runs.update_one(
                {"id": run_id},
                {"$set": {"status": "skipped", "finished_at": now_iso(),
                          "reason": f"http_{exc.status_code}"}})
            outcome["skipped"].append({"tenant_id": tid,
                                      "reason": f"http_{exc.status_code}"})
        except Exception as exc:
            await db.scheduler_runs.update_one(
                {"id": run_id},
                {"$set": {"status": "failed", "finished_at": now_iso(),
                          "reason": str(exc)[:300]}})
            outcome["errors"] += 1
            logger.exception("Collections autopilot failed for tenant %s", tid)
    _state["last_outcome"] = outcome
    logger.info("Collections autopilot run finished: %s", outcome)
    return outcome


# ---------------- the daily FX job ----------------
async def run_fx_rates() -> dict:
    """Refresh exchange rates: seed history on first run, then fetch today.

    Storage is upsert-based, so re-runs are safe no-ops. Network
    failures are logged by lib.fx and recorded here, never raised.
    """
    try:
        report = await refresh_rates()
    except Exception as exc:  # belt and suspenders; fx already degrades gracefully
        logger.exception("FX rate refresh failed")
        report = {"error": str(exc)[:300]}
    _state["fx_last_outcome"] = {"at": now_iso(), **report}
    logger.info("FX rate refresh finished: %s", report)
    return report


# ---------------- the recurring-invoice job ----------------
async def _claim_run(job_id: str, tenant_id: str, window: str) -> str | None:
    """Atomically claim this tenant's slot for a job window via
    ``scheduler_runs`` (same idiom as the collections autopilot).
    Returns the run id, or None when the slot was already claimed."""
    run_id = f"{job_id}:{tenant_id}:{window}"
    try:
        await db.scheduler_runs.insert_one({
            "id": run_id, "job": job_id, "tenant_id": tenant_id,
            "run_date": window, "started_at": now_iso(), "status": "running"})
    except DuplicateKeyError:
        return None
    return run_id


async def _finish_run(run_id: str, status: str, **fields):
    await db.scheduler_runs.update_one(
        {"id": run_id},
        {"$set": {"status": status, "finished_at": now_iso(), **fields}})


async def run_recurring_invoices() -> dict:
    """Materialize due recurring invoices for every tenant.

    Calls the same code path as the manual "generate due" endpoint
    (``routers.recurring_invoices.run_due_for_user``). Safe to re-run:
    each tenant is claimed at most once per hour, and a template only
    generates when its ``next_run`` has arrived (it advances on generate,
    so a duplicate run is a no-op)."""
    from routers.recurring_invoices import run_due_for_user

    window = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    outcome = {"window": window, "tenants": 0, "generated": 0,
               "errors": 0, "skipped": []}
    tenants = await db.tenants.find({}, {"id": 1}).to_list(1000)
    for t in tenants:
        tid = t["id"]
        run_id = await _claim_run(RECURRING_JOB_ID, tid, window)
        if run_id is None:
            outcome["skipped"].append({"tenant_id": tid,
                                       "reason": "already_ran_this_hour"})
            continue
        try:
            res = await run_due_for_user(_system_user(tid, "Recurring invoice scheduler"))
            made = int(res.get("generated", 0))
            outcome["tenants"] += 1
            outcome["generated"] += made
            await _finish_run(run_id, "done", generated=made)
            if made:
                logger.info("Recurring invoices: tenant %s generated=%d", tid, made)
        except Exception as exc:
            await _finish_run(run_id, "failed", reason=str(exc)[:300])
            outcome["errors"] += 1
            logger.exception("Recurring invoice run failed for tenant %s", tid)
    _state["recurring_last_outcome"] = outcome
    logger.info("Recurring invoice run finished: %s", outcome)
    return outcome


# ---------------- the bank-sync job ----------------
async def run_bank_sync() -> dict:
    """Sync Plaid transactions for every tenant with a bank connection.

    Respects the bank_feeds plan gate (same check as the bankfeed router
    and webhook path): tenants whose plan doesn't include bank feeds are
    skipped. The sync itself is the manual endpoint's code path
    (``routers.bankfeed.sync_connections``) — cursor-based and deduped,
    so overlapping webhook-triggered syncs can't double-post. Each tenant
    is claimed at most once per interval window."""
    from lib.plans import effective_plan, plan_includes
    from routers.bankfeed import sync_connections

    interval = bank_sync_interval_hours()
    window = str(int(datetime.now(timezone.utc).timestamp() // (interval * 3600)))
    outcome = {"window": window, "tenants": 0, "added": 0, "duplicates": 0,
               "errors": 0, "skipped": []}
    tenants = await db.tenants.find({}, {"id": 1}).to_list(1000)
    for t in tenants:
        tid = t["id"]
        tenant = await db.tenants.find_one({"id": tid}, {"_id": 0})
        if not plan_includes(effective_plan(tenant or {}), "bank_feeds"):
            outcome["skipped"].append({"tenant_id": tid, "reason": "plan_gate"})
            continue
        if not await db.bank_connections.count_documents({"tenant_id": tid}):
            continue  # nothing connected — nothing to claim or sync
        run_id = await _claim_run(BANK_SYNC_JOB_ID, tid, window)
        if run_id is None:
            outcome["skipped"].append({"tenant_id": tid,
                                       "reason": "already_synced_this_window"})
            continue
        try:
            res = await sync_connections(_system_user(tid, "Plaid sync scheduler"))
            outcome["tenants"] += 1
            outcome["added"] += int(res.get("added", 0))
            outcome["duplicates"] += int(res.get("duplicates", 0))
            await _finish_run(run_id, "done", added=res.get("added", 0),
                              duplicates=res.get("duplicates", 0))
            logger.info("Bank sync: tenant %s added=%s duplicates=%s",
                        tid, res.get("added"), res.get("duplicates"))
        except HTTPException as exc:
            await _finish_run(run_id, "skipped", reason=f"http_{exc.status_code}")
            outcome["skipped"].append({"tenant_id": tid,
                                       "reason": f"http_{exc.status_code}"})
        except Exception as exc:
            await _finish_run(run_id, "failed", reason=str(exc)[:300])
            outcome["errors"] += 1
            logger.exception("Bank sync failed for tenant %s", tid)
    _state["bank_last_outcome"] = outcome
    logger.info("Bank sync run finished: %s", outcome)
    return outcome


# ---------------- lifecycle ----------------
async def start() -> bool:
    """Start the scheduler if enabled and the instance lock is acquired."""
    if not scheduler_enabled():
        logger.info("Scheduler disabled (ENABLE_SCHEDULER != true).")
        return False
    instance_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    if not await _acquire_lock(instance_id):
        logger.warning("Scheduler lock held by another instance; "
                       "not starting the scheduler here.")
        return False
    global _scheduler, _heartbeat_task
    _scheduler = AsyncIOScheduler(timezone=_tz())
    hour, minute = _run_time()
    _scheduler.add_job(
        run_collections_autopilot,
        CronTrigger(hour=hour, minute=minute),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    if fx_rates_enabled():
        fx_hour, fx_minute = _fx_run_time()
        _scheduler.add_job(
            run_fx_rates,
            CronTrigger(hour=fx_hour, minute=fx_minute),
            id=FX_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        fx_job = _scheduler.get_job(FX_JOB_ID)
        _state["fx_next_run"] = (
            fx_job.next_run_time.isoformat()
            if fx_job and fx_job.next_run_time else None)
        logger.info("Scheduler: FX rates daily at %02d:%02d %s",
                    fx_hour, fx_minute, _tz())
    else:
        logger.info("Scheduler: FX rate refresh disabled "
                    "(FX_RATES_ENABLED != true).")
    if recurring_invoices_enabled():
        _scheduler.add_job(
            run_recurring_invoices,
            IntervalTrigger(hours=1),
            id=RECURRING_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=900,
        )
        rec_job = _scheduler.get_job(RECURRING_JOB_ID)
        _state["recurring_next_run"] = (
            rec_job.next_run_time.isoformat()
            if rec_job and rec_job.next_run_time else None)
        logger.info("Scheduler: recurring invoices hourly")
    else:
        logger.info("Scheduler: recurring invoices disabled "
                    "(RECURRING_INVOICES_ENABLED != true).")
    if bank_sync_enabled():
        interval = bank_sync_interval_hours()
        _scheduler.add_job(
            run_bank_sync,
            IntervalTrigger(hours=interval),
            id=BANK_SYNC_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        bank_job = _scheduler.get_job(BANK_SYNC_JOB_ID)
        _state["bank_next_run"] = (
            bank_job.next_run_time.isoformat()
            if bank_job and bank_job.next_run_time else None)
        logger.info("Scheduler: bank sync every %d hour(s)", interval)
    else:
        logger.info("Scheduler: bank sync disabled (BANK_SYNC_ENABLED != true).")
    _scheduler.start()
    job = _scheduler.get_job(JOB_ID)
    _state.update(
        running=True,
        instance_id=instance_id,
        next_run=(job.next_run_time.isoformat()
                  if job and job.next_run_time else None),
    )
    _heartbeat_task = asyncio.create_task(_heartbeat(instance_id))
    logger.info("Scheduler started: collections autopilot daily at "
                "%02d:%02d %s", hour, minute, _tz())
    return True


async def stop():
    global _scheduler, _heartbeat_task
    if _heartbeat_task is not None:
        _heartbeat_task.cancel()
        _heartbeat_task = None
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    _state.update(running=False, instance_id=None, next_run=None,
                  fx_next_run=None, recurring_next_run=None, bank_next_run=None)


def status() -> dict:
    return dict(_state)
