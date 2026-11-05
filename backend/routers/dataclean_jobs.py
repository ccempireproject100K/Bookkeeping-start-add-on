"""DataClean power-up surface — /api/v1/dataclean/lists + /jobs.

Phase B (P1 + P7): stored lists, the merge-approval queue over them,
and async batch jobs (5k -> 500k rows). Phase C adds list schedules +
drift; Phase D adds verify jobs. Same auth as the rest of the
surface: a DataClean API key; the tenant comes from the key record.

Queue law (mirrors the internal console, routers/dedupe.py): scans
store SUGGESTIONS only; merges are separate human-invoked calls,
fill-blanks-only, snapshot-backed, reversible; dismissals are
restorable. Nothing here merges automatically, ever.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from lib import dataclean_api as dca
from lib import dataclean_jobs as jobs_lib
from lib import dataclean_lists as lists_lib
from lib.dataclean import RESULT_SHAPE
from routers.dataclean_api import _err

router = APIRouter(prefix="/v1/dataclean", tags=["dataclean-jobs"])

MAX_CHUNK_RECORDS = 5000     # one records-append call
SYNC_SCAN_MAX = 5000         # bigger scans go through jobs


def _list_view(doc):
    return {k: doc.get(k) for k in (
        "id", "name", "kind", "source", "record_count", "retention",
        "created_at", "created_by", "last_scan_at", "last_job_id")}


def _job_view(doc):
    return {k: doc.get(k) for k in (
        "id", "kind", "list_id", "list_b_id", "status", "rows_total",
        "chunk_size", "chunks_total", "chunks_done", "pairs_scored",
        "output_list_id", "result_counts", "estimate", "error",
        "created_at", "started_at", "finished_at")}


# ------------------------------------------------------------------ lists

class CreateListIn(BaseModel):
    name: str
    kind: Optional[str] = "records"
    records: Optional[list] = None


@router.post("/lists")
async def create_list(body: CreateListIn,
                      actor: dict = Depends(dca.get_dataclean_actor)):
    doc = await lists_lib.create_list(actor, body.name, body.kind or
                                      "records")
    added = 0
    if body.records:
        if len(body.records) > MAX_CHUNK_RECORDS:
            raise _err(413, "payload_too_large",
                       f"at most {MAX_CHUNK_RECORDS} records per call — "
                       "append the rest in chunks",
                       max_records=MAX_CHUNK_RECORDS)
        if not all(isinstance(r, dict) for r in body.records):
            raise _err(422, "invalid_payload",
                       "records must be a list of objects")
        added = await lists_lib.append_records(actor, doc, body.records)
        doc = await lists_lib.get_list(actor, doc["id"])
    await dca.record_usage(actor, "lists/create", added, "ok")
    return {"shape": RESULT_SHAPE, "api_version": "v1",
            "list": _list_view(doc), "records_added": added,
            "retention": doc.get("retention")}


@router.get("/lists")
async def get_lists(actor: dict = Depends(dca.get_dataclean_actor)):
    rows = await lists_lib.list_lists(actor)
    return {"lists": [_list_view(r) for r in rows]}


@router.get("/lists/{list_id}")
async def get_list(list_id: str,
                   actor: dict = Depends(dca.get_dataclean_actor)):
    doc = await lists_lib.get_list(actor, list_id)
    if not doc:
        raise _err(404, "list_not_found", "no such list")
    return {"list": _list_view(doc)}


@router.delete("/lists/{list_id}")
async def delete_list(list_id: str,
                      actor: dict = Depends(dca.get_dataclean_actor)):
    doc = await lists_lib.get_list(actor, list_id)
    if not doc:
        raise _err(404, "list_not_found", "no such list")
    from db import db
    from lib import dataclean_schedules as sched_lib
    await sched_lib.delete_schedule(actor, list_id)
    counts = await lists_lib.delete_list(actor, list_id)
    drift = await db.dataclean_list_drift.delete_many(
        {"tenant_id": actor["tenant_id"], "list_id": list_id})
    counts["drift_reports"] = drift.deleted_count
    return {"deleted": True, "removed": counts}


class AppendIn(BaseModel):
    records: list


@router.post("/lists/{list_id}/records")
async def append_records(list_id: str, body: AppendIn,
                         actor: dict = Depends(dca.get_dataclean_actor)):
    doc = await lists_lib.get_list(actor, list_id)
    if not doc:
        raise _err(404, "list_not_found", "no such list")
    if len(body.records) > MAX_CHUNK_RECORDS:
        raise _err(413, "payload_too_large",
                   f"at most {MAX_CHUNK_RECORDS} records per call",
                   max_records=MAX_CHUNK_RECORDS)
    if not all(isinstance(r, dict) for r in body.records):
        raise _err(422, "invalid_payload",
                   "records must be a list of objects")
    if doc.get("record_count", 0) + len(body.records) \
            > lists_lib.MAX_RECORDS_PER_LIST:
        raise _err(413, "list_too_large",
                   f"a list holds at most "
                   f"{lists_lib.MAX_RECORDS_PER_LIST} records")
    added = await lists_lib.append_records(actor, doc, body.records)
    doc = await lists_lib.get_list(actor, list_id)
    await dca.record_usage(actor, "lists/append", added, "ok")
    return {"records_added": added, "record_count": doc["record_count"]}


@router.get("/lists/{list_id}/records")
async def get_records(list_id: str, offset: int = 0,
                      limit: int = 100,
                      actor: dict = Depends(dca.get_dataclean_actor)):
    doc = await lists_lib.get_list(actor, list_id)
    if not doc:
        raise _err(404, "list_not_found", "no such list")
    rows = await lists_lib.list_records(actor, list_id, limit=1000000)
    rows.sort(key=lambda d: d.get("id") or "")
    page = [{k: v for k, v in r.items() if k != "tenant_id"}
            for r in rows[offset:offset + limit]]
    return {"records": page, "total": len(rows)}


# ------------------------------------------------------------------ scans

class ScanIn(BaseModel):
    other_list_id: Optional[str] = None


@router.post("/lists/{list_id}/scan")
async def scan_list(list_id: str, body: ScanIn,
                    actor: dict = Depends(dca.get_dataclean_actor)):
    """Synchronous scan for small lists; stores pending suggestions
    and returns the counts. Larger lists: POST /jobs (kind
    dedupe_scan) — the response says so rather than timing out."""
    doc = await lists_lib.get_list(actor, list_id)
    if not doc:
        raise _err(404, "list_not_found", "no such list")
    other = None
    total = doc.get("record_count", 0)
    if body.other_list_id:
        other = await lists_lib.get_list(actor, body.other_list_id)
        if not other:
            raise _err(404, "list_not_found", "no such other list")
        total += other.get("record_count", 0)
    if total > SYNC_SCAN_MAX:
        raise _err(413, "scan_too_large",
                   f"sync scans cover at most {SYNC_SCAN_MAX} records; "
                   "create a dedupe_scan job instead",
                   max_records=SYNC_SCAN_MAX)
    await dca.enforce_limits(actor, "lists/scan", total)
    result = await lists_lib.scan_list(actor, doc, other)
    await dca.record_usage(actor, "lists/scan", total, "ok")
    return {"shape": RESULT_SHAPE, "api_version": "v1",
            "engine": result["engine"],
            "engine_note": result.get("engine_note"),
            "mode": "cross" if other else "within",
            "suggested": len(result["pairs"]),
            "review": len(result.get("review_pairs", []))}


@router.get("/lists/{list_id}/suggestions")
async def get_suggestions(list_id: str,
                          status: str = "pending",
                          band: Optional[str] = None,
                          offset: int = 0,
                          limit: int = 100,
                          actor: dict = Depends(dca.get_dataclean_actor)):
    if not await lists_lib.get_list(actor, list_id):
        raise _err(404, "list_not_found", "no such list")
    rows, total = await lists_lib.list_suggestions(
        actor, list_id, status=status, band=band, offset=offset,
        limit=limit)
    clean = [{k: v for k, v in r.items() if k != "tenant_id"}
             for r in rows]
    return {"suggestions": clean, "total": total}


@router.post("/lists/{list_id}/suggestions/{sid}/dismiss")
async def dismiss(list_id: str, sid: str,
                  actor: dict = Depends(dca.get_dataclean_actor)):
    out, err = await lists_lib.dismiss_suggestion(actor, list_id, sid)
    if err == "not_found":
        raise _err(404, "suggestion_not_found", "no such suggestion")
    if err:
        raise _err(409, "conflict", err)
    return out


@router.post("/lists/{list_id}/suggestions/{sid}/restore")
async def restore(list_id: str, sid: str,
                  actor: dict = Depends(dca.get_dataclean_actor)):
    out, err = await lists_lib.restore_suggestion(actor, list_id, sid)
    if err == "not_found":
        raise _err(404, "suggestion_not_found", "no such suggestion")
    if err:
        raise _err(409, "conflict", err)
    return out


class MergeIn(BaseModel):
    survivor_id: Optional[str] = None


@router.post("/lists/{list_id}/suggestions/{sid}/merge")
async def merge(list_id: str, sid: str, body: MergeIn,
                actor: dict = Depends(dca.get_dataclean_actor)):
    """The human-approved merge. Fill-blanks only; absorbed flagged,
    never deleted; snapshots stored for reversal."""
    out, err = await lists_lib.merge_suggestion(
        actor, list_id, sid, survivor_id=body.survivor_id)
    if err == "not_found":
        raise _err(404, "suggestion_not_found", "no such suggestion")
    if err:
        raise _err(409, "conflict", err)
    return out


@router.get("/lists/{list_id}/merges")
async def get_merges(list_id: str,
                     actor: dict = Depends(dca.get_dataclean_actor)):
    if not await lists_lib.get_list(actor, list_id):
        raise _err(404, "list_not_found", "no such list")
    rows = await lists_lib.list_merges(actor, list_id)
    clean = [{k: v for k, v in r.items() if k != "tenant_id"}
             for r in rows]
    return {"merges": clean}


@router.post("/lists/{list_id}/merges/{mid}/reverse")
async def reverse_merge(list_id: str, mid: str,
                        actor: dict = Depends(dca.get_dataclean_actor)):
    out, err = await lists_lib.reverse_merge(actor, list_id, mid)
    if err == "not_found":
        raise _err(404, "merge_not_found", "no such merge")
    if err:
        raise _err(409, "conflict", err)
    return out


@router.get("/lists/{list_id}/merges/export")
async def export_merges(list_id: str,
                        actor: dict = Depends(dca.get_dataclean_actor)):
    """Merge-decision export: every merge + dismissal on this list
    with its evidence, for replay in the customer's own system."""
    if not await lists_lib.get_list(actor, list_id):
        raise _err(404, "list_not_found", "no such list")
    return await lists_lib.export_decisions(actor, list_id)


# ------------------------------------------------- continuous mode (P3)

class ScheduleIn(BaseModel):
    cadence: str
    webhook_url: Optional[str] = None


@router.put("/lists/{list_id}/schedule")
async def put_schedule(list_id: str, body: ScheduleIn,
                       actor: dict = Depends(dca.get_dataclean_actor)):
    """Continuous mode for an external list: scheduled re-scans with
    drift reports. Cadence daily | weekly."""
    from lib import dataclean_schedules as sched_lib
    if not await lists_lib.get_list(actor, list_id):
        raise _err(404, "list_not_found", "no such list")
    doc, err = await sched_lib.set_schedule(
        actor, list_id, body.cadence, body.webhook_url)
    if err:
        raise _err(422, "invalid_cadence",
                   "cadence must be one of ['daily', 'weekly']")
    return {"schedule": {k: doc.get(k) for k in (
        "id", "list_id", "cadence", "webhook_url", "enabled",
        "next_run_at", "last_run_at", "last_job_id")},
        "retention": lists_lib.RETENTION_WORDING}


@router.get("/lists/{list_id}/schedule")
async def get_schedule(list_id: str,
                       actor: dict = Depends(dca.get_dataclean_actor)):
    from lib import dataclean_schedules as sched_lib
    doc = await sched_lib.get_schedule(actor, list_id)
    if not doc:
        raise _err(404, "schedule_not_found", "no schedule for this list")
    return {"schedule": {k: doc.get(k) for k in (
        "id", "list_id", "cadence", "webhook_url", "enabled",
        "next_run_at", "last_run_at", "last_job_id")},
        "retention": lists_lib.RETENTION_WORDING}


@router.delete("/lists/{list_id}/schedule")
async def delete_schedule(list_id: str,
                          actor: dict = Depends(dca.get_dataclean_actor)):
    from lib import dataclean_schedules as sched_lib
    doc = await sched_lib.delete_schedule(actor, list_id)
    if not doc:
        raise _err(404, "schedule_not_found", "no schedule for this list")
    return {"deleted": True}


@router.get("/lists/{list_id}/drift")
async def get_drift(list_id: str,
                    actor: dict = Depends(dca.get_dataclean_actor)):
    """Drift reports for this list: how its pending-pair set changed
    between scheduled runs (new / resolved / evidence-changed)."""
    from lib import dataclean_schedules as sched_lib
    if not await lists_lib.get_list(actor, list_id):
        raise _err(404, "list_not_found", "no such list")
    return await sched_lib.latest_drift(actor, list_id)


# ------------------------------------------------------------------- jobs

class CreateJobIn(BaseModel):
    kind: str
    list_id: str
    list_b_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    options: Optional[dict] = None


@router.post("/jobs")
async def create_job(body: CreateJobIn,
                     actor: dict = Depends(dca.get_dataclean_actor)):
    doc, err = await jobs_lib.create_job(
        actor, body.kind, body.list_id, body.list_b_id,
        idempotency_key=body.idempotency_key, options=body.options)
    if err == "unknown_kind":
        raise _err(422, "unknown_kind",
                   "kind must be one of ['dedupe_scan', 'clean', "
                   "'verify']")
    if err == "list_not_found":
        raise _err(404, "list_not_found", "no such list")
    if err == "job_too_large":
        raise _err(413, "job_too_large",
                   f"a job covers at most {jobs_lib.MAX_JOB_ROWS} rows",
                   max_rows=jobs_lib.MAX_JOB_ROWS)
    if doc.get("status") == "queued" and not doc.get("started_at"):
        # Quota gate at creation (the plan quota is the job ceiling);
        # a fresh doc returned by an idempotency hit skips the gate.
        # On refusal the job doc is removed again — a refused job must
        # not linger in the queue for the sweep to run ungated.
        from fastapi import HTTPException
        try:
            await dca.enforce_limits(actor, "jobs/create",
                                     doc.get("rows_total", 0))
        except HTTPException:
            await jobs_lib.delete_job(actor, doc["id"])
            raise
        jobs_lib.kick(actor, doc["id"])
    return {"shape": RESULT_SHAPE, "api_version": "v1",
            "job": _job_view(doc)}


@router.get("/jobs")
async def get_jobs(actor: dict = Depends(dca.get_dataclean_actor)):
    rows = await jobs_lib.list_jobs(actor)
    return {"jobs": [_job_view(r) for r in rows]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str,
                  actor: dict = Depends(dca.get_dataclean_actor)):
    doc = await jobs_lib.get_job(actor, job_id)
    if not doc:
        raise _err(404, "job_not_found", "no such job")
    return {"job": _job_view(doc)}


@router.get("/jobs/{job_id}/result")
async def get_job_result(job_id: str, kind: str = "pair",
                         band: Optional[str] = None,
                         offset: int = 0,
                         limit: int = 100,
                         actor: dict = Depends(dca.get_dataclean_actor)):
    doc = await jobs_lib.get_job(actor, job_id)
    if not doc:
        raise _err(404, "job_not_found", "no such job")
    rows, total = await jobs_lib.job_results(
        actor, job_id, kind=kind, band=band, offset=offset, limit=limit)
    return {"job_id": job_id, "kind": kind, "band": band,
            "results": rows, "total": total}
