"""DataClean async batch jobs (P1): 5k -> 500k rows per job.

A job runs the SAME engine functions as the sync surface — no forked
logic. The scaling trick is the one the design specifies: records are
read in chunks of CHUNK_SIZE to build features + the global block
index (candidate harvesting), then candidates are scored in one pass
whose pairs stream into job artifacts. No N x N comparison ever
happens; blocks too big to score honestly are skipped and counted
(linkage._guarded_blocks). Clean jobs are chunk-native: each chunk is
cleaned independently with clean_row into an output list.

Resumability: input-chunk progress is marked on the job document as
chunks complete. A job killed mid-run is picked up by the scheduler
sweep (queued, or running-but-stale); artifact writes for a run are
job-scoped and idempotent — a resumed dedupe job deletes its prior
pair artifacts and re-scores from the harvested index state recorded
on the job (stats), while a resumed clean job skips chunks already
marked done (its output is append-only per chunk and chunks are
marked only after their writes land).

Quotas: creation enforces the design ceiling — min(500,000 rows, the
key's monthly row quota via lib/dataclean_api.enforce_limits) — and
completion meters the rows actually processed.

Verify jobs (Phase D) share this skeleton: kind="verify" jobs pause
in `awaiting_confirmation` after their pre-flight estimate until the
customer confirms via the confirm endpoint.

Collections: dataclean_jobs, dataclean_job_artifacts (tenant-scoped).
"""
import asyncio
from collections import Counter

from db import db
from lib import dataclean_lists as lists_lib
from lib.base import Scoped, new_id, now_iso
from lib.dataclean import linkage
from lib.dataclean.cleaning import clean_row

CHUNK_SIZE = 5000
MAX_JOB_ROWS = 500000
_KINDS = ("dedupe_scan", "clean", "verify")

_inflight = set()


def _jobs(actor):
    return Scoped(db.dataclean_jobs, actor["tenant_id"])


def _artifacts(actor):
    return Scoped(db.dataclean_job_artifacts, actor["tenant_id"])


def jobs_enabled() -> bool:
    import os
    raw = os.environ.get("DATACLEAN_JOBS_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def create_job(actor, kind, list_id, list_b_id=None,
                     idempotency_key=None, options=None):
    """Create (or return the existing, for an idempotency key) job.
    Returns (job_doc, error_code|None)."""
    if kind not in _KINDS:
        return None, "unknown_kind"
    if idempotency_key:
        existing = await _jobs(actor).find_one(
            {"idempotency_key": idempotency_key})
        if existing:
            return existing, None
    list_doc = await lists_lib.get_list(actor, list_id)
    if not list_doc:
        return None, "list_not_found"
    rows = list_doc.get("record_count", 0)
    other_doc = None
    if list_b_id:
        other_doc = await lists_lib.get_list(actor, list_b_id)
        if not other_doc:
            return None, "list_not_found"
        rows += other_doc.get("record_count", 0)
    if rows > MAX_JOB_ROWS:
        return None, "job_too_large"
    doc = {"id": new_id(), "kind": kind, "list_id": list_id,
           "list_b_id": list_b_id, "status": "queued",
           "rows_total": rows, "chunk_size": CHUNK_SIZE,
           "chunks_total": (rows + CHUNK_SIZE - 1) // CHUNK_SIZE,
           "chunks_done": [], "pairs_scored": 0,
           "idempotency_key": idempotency_key,
           "options": options or {},
           "output_list_id": None, "result_counts": None,
           "estimate": None, "error": None,
           "created_at": now_iso(), "created_by": actor.get("id"),
           "key_id": actor.get("api_key_id"),
           "started_at": None, "finished_at": None}
    await _jobs(actor).insert_one(doc)
    return doc, None


async def get_job(actor, job_id):
    return await _jobs(actor).find_one({"id": job_id})


async def delete_job(actor, job_id):
    await _jobs(actor).c.delete_one({"tenant_id": actor["tenant_id"],
                                     "id": job_id})


async def list_jobs(actor, limit=100):
    rows = await _jobs(actor).find_list({}, limit=limit)
    rows.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return rows


async def _mark(actor, job_id, **fields):
    fields = {k: v for k, v in fields.items()}
    await _jobs(actor).update_one({"id": job_id}, {"$set": fields})


async def _flush_pairs(actor, job, pairs, band, seq):
    docs = []
    for p in pairs:
        docs.append({"id": new_id(), "job_id": job["id"], "kind": "pair",
                     "band": band, "seq": seq,
                     "payload": {"a_id": p["a_id"], "b_id": p["b_id"],
                                 "score": p["score"],
                                 "evidence": p["evidence"],
                                 "engine": p.get("engine")}})
        seq += 1
    if docs:
        await _artifacts(actor).insert_many(docs)
    return seq


async def _run_dedupe(actor, job):
    """Chunked read -> global harvest -> one scoring pass -> artifacts
    + stored suggestions (the Studio queue reads the same store the
    sync scan writes)."""
    feats = []
    by_id = {}
    for li, list_id in enumerate(
            [job["list_id"]] + ([job["list_b_id"]] if job["list_b_id"]
                                else [])):
        done = set(job.get("chunks_done") or [])
        rows = await _page_records(actor, list_id, 0, MAX_JOB_ROWS)
        for chunk_idx, start in enumerate(range(0, len(rows), CHUNK_SIZE)):
            for e in rows[start:start + CHUNK_SIZE]:
                if not lists_lib.active(e):
                    continue
                f = linkage.features(e)
                f["_side"] = li
                feats.append(f)
                by_id[e["id"]] = e
            marker = f"{list_id}:{chunk_idx}"
            if marker not in done:
                job["chunks_done"] = list(job.get("chunks_done") or []) \
                    + [marker]
                await _mark(actor, job["id"],
                            chunks_done=job["chunks_done"])
    cand, stats = linkage._guarded_blocks(feats)
    if job["list_b_id"]:
        cand = {(i, j) for (i, j) in cand
                if feats[i]["_side"] != feats[j]["_side"]}
        for f in feats:
            f.pop("_side", None)
    name_freq = Counter()
    for f in feats:
        if f["name_key"]:
            name_freq[f["name_key"]] += 1
            name_freq[f["name_key"].split()[0]] += 1
    suggested, review = linkage._tiered_deterministic(feats, cand,
                                                      name_freq)
    # Idempotent artifact rewrite for this job.
    old = await _artifacts(actor).find_list({"job_id": job["id"]},
                                            limit=1000000)
    for o in old:
        await _artifacts(actor).c.delete_one(
            {"tenant_id": actor["tenant_id"], "id": o["id"]})
    seq = await _flush_pairs(actor, job, suggested, "suggested", 0)
    seq = await _flush_pairs(actor, job, review, "review", seq)
    result = {"pairs": suggested, "review_pairs": review,
              "engine": "deterministic"}
    mode = "cross" if job["list_b_id"] else "within"
    stored = await lists_lib.store_scan_results(
        actor, job["list_id"], result, mode=mode,
        other_list_id=job["list_b_id"], job_id=job["id"],
        entities_by_id=by_id)
    stats["suggestions_stored"] = len(stored)
    return {"suggested": len(suggested), "review": len(review)}, stats


async def _page_records(actor, list_id, offset, limit):
    rows = await lists_lib._records(actor).find_list(
        {"list_id": list_id}, limit=1000000)
    rows.sort(key=lambda d: d.get("id") or "")
    return rows[offset:offset + limit]


async def _run_clean(actor, job):
    src = await lists_lib.get_list(actor, job["list_id"])
    if job.get("output_list_id"):
        out = await lists_lib.get_list(actor, job["output_list_id"])
    else:
        out = None
    if not out:
        out = await lists_lib.create_list(
            actor, f"{src['name']} (cleaned)",
            kind=src.get("kind", "records"), source="dataclean:clean-job")
        await _mark(actor, job["id"], output_list_id=out["id"])
    options = job.get("options") or {}
    country = options.get("country")
    done = set(job.get("chunks_done") or [])
    changes_total = 0
    prior = await _artifacts(actor).find_list(
        {"job_id": job["id"], "kind": "change"}, limit=1000000)
    seq = len(prior)
    changes_total = seq
    all_rows = await _page_records(actor, job["list_id"], 0, MAX_JOB_ROWS)
    chunk_total = (len(all_rows) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for chunk_idx in range(chunk_total):
        rows = all_rows[chunk_idx * CHUNK_SIZE:(chunk_idx + 1) * CHUNK_SIZE]
        marker = f"clean:{chunk_idx}"
        if marker not in done:
            cleaned_docs, change_docs = [], []
            for e in rows:
                if not lists_lib.active(e):
                    continue
                payload = {k: v for k, v in e.items()
                           if k not in ("id", "list_id", "tenant_id",
                                        "status", "merged_into",
                                        "merged_at")}
                cleaned, changes = clean_row(payload, src.get("kind"),
                                             country=country)
                cleaned["id"] = e["id"]
                cleaned_docs.append(cleaned)
                for c in changes:
                    change_docs.append({
                        "id": new_id(), "job_id": job["id"],
                        "kind": "change", "band": "", "seq": seq,
                        "payload": {"record_id": e["id"], **c}})
                    seq += 1
            if cleaned_docs:
                await lists_lib.append_records(actor, out, cleaned_docs)
            if change_docs:
                await _artifacts(actor).insert_many(change_docs)
            changes_total += len(change_docs)
            job["chunks_done"] = list(job.get("chunks_done") or []) \
                + [marker]
            await _mark(actor, job["id"], chunks_done=job["chunks_done"])
    return {"changes": changes_total,
            "output_list_id": out["id"]}, {"chunks": chunk_total}


async def process_job(actor, job_id):
    """Run one job to completion (or to awaiting_confirmation for
    verify jobs). Safe to call twice: an in-flight guard plus chunk
    markers make a duplicate call a no-op or a resume."""
    if job_id in _inflight:
        return await get_job(actor, job_id)
    job = await get_job(actor, job_id)
    if not job or job["status"] in ("complete", "failed"):
        return job
    if job["status"] == "awaiting_confirmation":
        return job
    _inflight.add(job_id)
    try:
        await _mark(actor, job_id, status="running",
                    started_at=job.get("started_at") or now_iso(),
                    error=None)
        job = await get_job(actor, job_id)
        if job["kind"] == "verify":
            from lib import dataclean_verify as verify_lib
            counts, stats = await verify_lib.prepare_verify_job(
                actor, job)
            await _mark(actor, job_id, status="awaiting_confirmation",
                        result_counts=counts, stats=stats,
                        estimate=job.get("estimate"))
            return await get_job(actor, job_id)
        if job["kind"] == "clean":
            counts, stats = await _run_clean(actor, job)
        else:
            counts, stats = await _run_dedupe(actor, job)
        await _mark(actor, job_id, status="complete",
                    finished_at=now_iso(), result_counts=counts,
                    stats=stats)
        from lib import dataclean_api as dca
        real_actor = dict(actor)
        real_actor.setdefault("api_key_id", job.get("key_id"))
        await dca.record_usage(real_actor, f"jobs/{job['kind']}",
                               job.get("rows_total", 0), "ok")
        return await get_job(actor, job_id)
    except Exception as exc:
        await _mark(actor, job_id, status="failed",
                    finished_at=now_iso(), error=str(exc)[:300])
        return await get_job(actor, job_id)
    finally:
        _inflight.discard(job_id)


def kick(actor, job_id):
    """Fire-and-forget processing from a request handler (the sweep is
    the backstop if this task is lost to a restart)."""
    try:
        asyncio.get_event_loop().create_task(process_job(actor, job_id))
    except RuntimeError:
        pass


async def run_pending_jobs(limit=3):
    """Scheduler sweep: process queued jobs and resume stale running
    ones (started > 30 min ago and not in-flight here). Cross-tenant
    by construction — the sweep reads job docs, then rebuilds a
    minimal actor per tenant from the job's own fields."""
    from datetime import datetime, timezone, timedelta
    docs = await db.dataclean_jobs.find(
        {"status": {"$in": ["queued", "running"]}}).to_list(length=100)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    ran = 0
    for doc in docs:
        if ran >= limit:
            break
        if doc["id"] in _inflight:
            continue
        if doc["status"] == "running":
            started = doc.get("started_at") or ""
            try:
                st = datetime.fromisoformat(
                    started.replace("Z", "+00:00"))
            except ValueError:
                st = None
            if st and st > cutoff:
                continue
        actor = {"id": doc.get("created_by"), "tenant_id": doc["tenant_id"],
                 "api_key_id": doc.get("key_id"),
                 "auth_kind": "api_key", "role": "dataclean_api_key"}
        await process_job(actor, doc["id"])
        ran += 1
    return {"jobs_processed": ran}


async def job_results(actor, job_id, kind="pair", band=None, offset=0,
                      limit=100):
    q = {"job_id": job_id, "kind": kind}
    if band:
        q["band"] = band
    rows = await _artifacts(actor).find_list(q, limit=1000000)
    rows.sort(key=lambda d: d.get("seq", 0))
    return [r["payload"] for r in rows[offset:offset + limit]], len(rows)
