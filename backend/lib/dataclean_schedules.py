"""DataClean continuous mode (P3): scheduled re-scans + drift reports
for EXTERNAL stored lists.

A schedule belongs to one list (daily or weekly). Each due run rides
the Phase B job pipeline (kind=dedupe_scan, trigger="scheduled") —
never a second engine path — and produces a DRIFT REPORT: the diff of
the list's pending-pair set against the previous run's:

  new       pairs that were not pending after the previous run
  resolved  pairs pending after the previous run, gone now (merged,
            dismissed, or no longer suggested by fresh data)
  changed   pairs pending in both runs whose score moved

Reports are stored (dataclean_list_drift) and returned by
GET /lists/{id}/drift. An optional completion webhook POSTs the drift
counts, HMAC-SHA256-signed with the hash of the key that created the
schedule (the plaintext key is never stored anywhere, so its hash is
the shared secret both sides can compute). Delivery failures are
recorded on the drift report, never raised into the sweep.

Internal books records (customers/vendors) keep their own existing
schedule in routers/dedupe.py — this module is for external lists
only, per the design.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from db import db
from lib import dataclean_jobs as jobs_lib
from lib import dataclean_lists as lists_lib
from lib.base import Scoped, new_id, now_iso

CADENCES = ("daily", "weekly")
_INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}


def http_post_json(url, payload, headers):
    """Module-level HTTP seam (tests monkeypatch this; production
    uses requests, already a backend dependency)."""
    import requests
    return requests.post(url, json=payload, headers=headers, timeout=15)


def _schedules(actor):
    return Scoped(db.dataclean_list_schedules, actor["tenant_id"])


def _drift(actor):
    return Scoped(db.dataclean_list_drift, actor["tenant_id"])


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_run(cadence, base=None):
    base = base or datetime.now(timezone.utc)
    return _iso(base + _INTERVALS[cadence])


async def set_schedule(actor, list_id, cadence, webhook_url=None):
    if cadence not in CADENCES:
        return None, "invalid_cadence"
    existing = await _schedules(actor).find_one({"list_id": list_id})
    doc = {"id": (existing or {}).get("id") or new_id(),
           "list_id": list_id, "cadence": cadence,
           "webhook_url": webhook_url,
           "enabled": True,
           "next_run_at": _next_run(cadence),
           "last_run_at": (existing or {}).get("last_run_at"),
           "last_job_id": (existing or {}).get("last_job_id"),
           "signing_key_hash": (actor.get("_key_doc") or {}).get(
               "key_hash"),
           "created_at": (existing or {}).get("created_at") or now_iso(),
           "created_by": actor.get("id"), "key_id": actor.get("api_key_id")}
    if existing:
        await _schedules(actor).update_one(
            {"id": doc["id"]}, {"$set": {k: v for k, v in doc.items()
                                         if k != "id"}})
    else:
        await _schedules(actor).insert_one(doc)
    return doc, None


async def get_schedule(actor, list_id):
    return await _schedules(actor).find_one({"list_id": list_id})


async def delete_schedule(actor, list_id):
    doc = await get_schedule(actor, list_id)
    if doc:
        await _schedules(actor).c.delete_one(
            {"tenant_id": actor["tenant_id"], "id": doc["id"]})
    return doc


def _pair_key(s):
    a, b = s["a_id"], s["b_id"]
    return (min(a, b), max(a, b), s.get("band", "suggested"))


async def _pending_pairs(actor, list_id):
    rows = await lists_lib._suggestions(actor).find_list(
        {"list_id": list_id, "status": "pending"}, limit=1000000)
    return {_pair_key(r): {"a_id": r["a_id"], "b_id": r["b_id"],
                           "band": r.get("band", "suggested"),
                           "score": r.get("score"),
                           "evidence": r.get("evidence")}
            for r in rows}


def diff_pairs(before, after):
    new = [after[k] for k in sorted(set(after) - set(before))]
    resolved = [before[k] for k in sorted(set(before) - set(after))]
    changed = []
    for k in sorted(set(before) & set(after)):
        if abs((before[k].get("score") or 0)
               - (after[k].get("score") or 0)) > 0.001:
            changed.append({"a_id": after[k]["a_id"],
                            "b_id": after[k]["b_id"],
                            "band": after[k]["band"],
                            "score_before": before[k].get("score"),
                            "score_after": after[k].get("score")})
    return {"new_pairs": new, "resolved_pairs": resolved,
            "changed_pairs": changed,
            "counts": {"new": len(new), "resolved": len(resolved),
                       "changed": len(changed)}}


async def run_scheduled_scan(actor, schedule):
    """One continuous-mode run for one schedule. Returns the drift
    report doc."""
    list_id = schedule["list_id"]
    before = await _pending_pairs(actor, list_id)
    job, err = await jobs_lib.create_job(
        actor, "dedupe_scan", list_id,
        idempotency_key=f"sched:{schedule['id']}:{schedule['next_run_at']}",
        options={"trigger": "scheduled"})
    if err:
        raise RuntimeError(f"scheduled job creation failed: {err}")
    job = await jobs_lib.process_job(actor, job["id"])
    after = await _pending_pairs(actor, list_id)
    # The drift baseline is the PREVIOUS run's after-state, so merges
    # and dismissals a human made between runs read as "resolved".
    # The first run has no predecessor: its baseline is the pending
    # set as it stood just before this scan.
    history = await _drift(actor).find_list({"list_id": list_id},
                                            limit=100000)
    history.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    prev_after = history[0].get("pairs_after") if history else None
    baseline = ({_pair_key(p): p for p in prev_after}
                if prev_after is not None else before)
    drift = diff_pairs(baseline, after)
    report = {"id": new_id(), "list_id": list_id,
              "schedule_id": schedule["id"], "job_id": job["id"],
              "job_status": job["status"],
              "created_at": now_iso(), **drift,
              "pairs_after": [after[k] for k in sorted(after)],
              "webhook_status": None, "webhook_error": None}
    if schedule.get("webhook_url"):
        payload = {"event": "dataclean.list_scan_complete",
                   "list_id": list_id, "job_id": job["id"],
                   "drift_counts": drift["counts"],
                   "at": report["created_at"]}
        body = json.dumps(payload, sort_keys=True).encode()
        secret = (schedule.get("signing_key_hash") or "").encode()
        sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
        try:
            resp = http_post_json(
                schedule["webhook_url"], payload,
                {"X-DataClean-Signature": f"sha256={sig}",
                 "Content-Type": "application/json"})
            report["webhook_status"] = int(getattr(resp, "status_code",
                                                   200))
        except Exception as exc:
            report["webhook_status"] = "failed"
            report["webhook_error"] = str(exc)[:200]
    await _drift(actor).insert_one(report)
    await _schedules(actor).update_one(
        {"id": schedule["id"]},
        {"$set": {"last_run_at": now_iso(), "last_job_id": job["id"],
                  "next_run_at": _next_run(schedule["cadence"])}})
    return report


async def run_dataclean_list_scans() -> dict:
    """Sweep (lib/scheduler.py, hourly): every enabled schedule whose
    next_run_at has passed, across all tenants. One bad schedule never
    stops the sweep."""
    now = now_iso()
    docs = await db.dataclean_list_schedules.find(
        {"enabled": True}).to_list(length=1000)
    ran, failed = 0, 0
    for doc in docs:
        if (doc.get("next_run_at") or "") > now:
            continue
        actor = {"id": doc.get("created_by"),
                 "tenant_id": doc["tenant_id"],
                 "api_key_id": doc.get("key_id"),
                 "auth_kind": "api_key", "role": "dataclean_api_key",
                 "_key_doc": {"key_hash": doc.get("signing_key_hash")}}
        try:
            await run_scheduled_scan(actor, doc)
            ran += 1
        except Exception:
            failed += 1
    return {"schedules_ran": ran, "schedules_failed": failed}


async def latest_drift(actor, list_id, limit=20):
    rows = await _drift(actor).find_list({"list_id": list_id},
                                         limit=100000)
    rows.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    clean = [{k: v for k, v in r.items() if k != "tenant_id"}
             for r in rows[:limit]]
    return {"list_id": list_id, "latest": clean[0] if clean else None,
            "history": clean}
