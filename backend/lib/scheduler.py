"""In-app scheduler for background jobs (APScheduler).

Currently runs two jobs: the daily collections autopilot and the daily
FX-rate refresh. The scheduler is guarded three ways:

1. It only starts when ``ENABLE_SCHEDULER=true`` (default off).
2. A MongoDB distributed lock (``scheduler_locks``) means only one app
   instance runs the schedule; the lock self-heals via TTL if an
   instance dies.
3. Each tenant gets at most one autopilot run per calendar day
   (``scheduler_runs``), so overlapping or retried schedules can't
   double-process a workspace. The autopilot itself is also
   stage-idempotent (it records ``autopilot_stages`` per invoice and
   caps late fees to one per month). FX-rate storage is upsert-based,
   so re-runs are no-ops.

Env vars:
    ENABLE_SCHEDULER      "true" to start the scheduler (default "false")
    COLLECTIONS_RUN_TIME  "HH:MM" 24h daily run time (default "06:00")
    FX_RATES_ENABLED      "true" to run the FX job (default "true")
    FX_RATES_RUN_TIME     "HH:MM" 24h daily run time (default "07:00")
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

_state = {
    "running": False,
    "instance_id": None,
    "next_run": None,
    "last_outcome": None,
    "fx_next_run": None,
    "fx_last_outcome": None,
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
def _system_user(tenant_id: str) -> dict:
    return {"id": "scheduler", "tenant_id": tenant_id,
            "email": "scheduler@system", "name": "Collections scheduler"}


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
    _state.update(running=False, instance_id=None, next_run=None)


def status() -> dict:
    return dict(_state)
