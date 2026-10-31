"""Duplicate-entity suggestions for clients & vendors (cleanup layer).

POST /dedupe/scan runs record linkage (lib/dataclean/linkage) over ONE
tenant's clients or vendors and stores candidate pairs as SUGGESTIONS.
Nothing here ever merges on its own: a merge happens only when a human
POSTs /dedupe/suggestions/{id}/merge. Merges are audit-logged and
REVERSIBLE — the merge record keeps full before-snapshots of both
records, and POST /dedupe/merges/{id}/reverse restores them exactly.
Linkage only ever sees the caller's own tenant (Scoped collections), so
suggestions can never cross tenants.

Phase 2: every scan (manual or scheduled) persists a row in
``dedupe_scan_runs`` so "last scan" is exact, including zero-result
scans; tenants may opt into scheduled scans (GET/PUT /dedupe/schedule,
run by lib/scheduler.run_dedupe_scans) which still only ever create
suggestions; and a dismissal can be undone via
POST /dedupe/suggestions/{id}/restore.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from lib.audit import append_audit
from lib.auth import get_current_user
from db import db
from lib.base import Scoped, new_id, now_iso
from lib.dataclean import linkage

router = APIRouter(prefix="/dedupe", tags=["dedupe"])

_KINDS = {"customers": "customers", "vendors": "vendors"}


def _coll(user, kind):
    return Scoped(getattr(db, _KINDS[kind]), user["tenant_id"])


def _suggestions(user):
    return Scoped(db.dedupe_suggestions, user["tenant_id"])


def _merges(user):
    return Scoped(db.entity_merges, user["tenant_id"])


def _scan_runs(user):
    return Scoped(db.dedupe_scan_runs, user["tenant_id"])


def _active(doc):
    return not (doc.get("merged_into") or doc.get("deleted_at")
                or doc.get("status") in ("merged", "deleted"))


class ScanIn(BaseModel):
    entity: str = "customers"


async def _run_scan(user, entity, trigger):
    """One scan, shared by the manual endpoint and the scheduler.
    Results land ONLY as pending suggestions — a scan never merges,
    never dismisses, never touches records. Every run (including
    zero-result and failed ones) persists a scan-run row, so "last
    scan" is an exact fact, not an inference from suggestions."""
    if entity not in _KINDS:
        raise HTTPException(400, "entity must be 'customers' or 'vendors'")
    run = {"id": new_id(), "entity": entity, "trigger": trigger,
           "status": "done", "started_at": now_iso(), "finished_at": None,
           "scanned": 0, "suggestions_new": 0, "engine": None,
           "engine_note": None, "error": None, "by_user": user.get("id")}
    try:
        coll = _coll(user, entity)
        entities = await coll.find_list({}, limit=5000)
        entities = [e for e in entities if _active(e)]
        result = linkage.suggest_duplicates(entities)
        sugg = _suggestions(user)
        # Refresh: drop still-pending suggestions for this entity kind,
        # then store the fresh set. Merged/dismissed history is never
        # rewritten.
        for old in await sugg.find_list(
                {"entity": entity, "status": "pending"}, limit=5000):
            await sugg.c.delete_one({"tenant_id": user["tenant_id"],
                                     "id": old["id"]})
        by_id = {e["id"]: e for e in entities}
        stored = []
        for pair in result["pairs"]:
            doc = {"id": new_id(), "entity": entity,
                   "a_id": pair["a_id"], "b_id": pair["b_id"],
                   "a_name": (by_id.get(pair["a_id"]) or {}).get("name"),
                   "b_name": (by_id.get(pair["b_id"]) or {}).get("name"),
                   "score": pair["score"], "engine": pair["engine"],
                   "evidence": pair["evidence"], "status": "pending",
                   "created_at": now_iso()}
            await sugg.insert_one(doc)
            stored.append(doc)
        run.update({"scanned": len(entities), "engine": result["engine"],
                    "engine_note": result.get("engine_note"),
                    "suggestions_new": len(stored)})
        return stored, run
    except Exception as e:  # persisted, then re-raised for the caller
        run.update({"status": "failed", "error": str(e)[:300]})
        raise
    finally:
        run["finished_at"] = now_iso()
        await _scan_runs(user).insert_one(dict(run))


@router.post("/scan")
async def scan(body: ScanIn, user: dict = Depends(get_current_user)):
    stored, run = await _run_scan(user, body.entity, "manual")
    return {"entity": body.entity, "scanned": run["scanned"],
            "engine": run["engine"], "engine_note": run["engine_note"],
            "suggestions": stored, "scan_run_id": run["id"],
            "finished_at": run["finished_at"]}


@router.get("/scan-runs")
async def list_scan_runs(user: dict = Depends(get_current_user)):
    """Recent scan runs for this tenant (manual + scheduled), newest
    first — the exact record behind "last scan" / "next scan"."""
    runs = await _scan_runs(user).find_list(sort=[("finished_at", -1)],
                                            limit=20)
    return {"scan_runs": runs}


# ---------------- scheduled scans (opt-in per tenant) ----------------
SCHEDULE_ID = "dedupe-schedule"
DEFAULT_SCHEDULE = {"enabled": False, "frequency": "weekly",
                    "entities": ["customers", "vendors"]}


def _schedule_coll(user):
    return Scoped(db.dedupe_schedule_configs, user["tenant_id"])


async def get_schedule_config(user) -> dict:
    doc = await _schedule_coll(user).find_one({"id": SCHEDULE_ID})
    if doc:
        merged = dict(DEFAULT_SCHEDULE)
        merged.update({k: v for k, v in doc.items()
                       if k in DEFAULT_SCHEDULE})
        return merged
    return dict(DEFAULT_SCHEDULE)


async def last_scan_at(user):
    """Newest finished scan-run stamp for this tenant, or None. Exact:
    zero-result scans leave a run row too."""
    runs = await _scan_runs(user).find_list(sort=[("finished_at", -1)],
                                            limit=1)
    return runs[0]["finished_at"] if runs else None


def next_scan_at(cfg, last):
    """When the next scheduled scan is due: last run + the frequency
    interval. None when scans are off or have never run."""
    if not cfg.get("enabled") or not last:
        return None
    try:
        base = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return None
    step = timedelta(days=1) if cfg.get("frequency") == "daily" \
        else timedelta(days=7)
    return (base + step).isoformat()


class ScheduleIn(BaseModel):
    enabled: bool = False
    frequency: str = "weekly"
    entities: list = ["customers", "vendors"]


@router.get("/schedule")
async def read_schedule(user: dict = Depends(get_current_user)):
    cfg = await get_schedule_config(user)
    last = await last_scan_at(user)
    return {**cfg, "last_scan_at": last,
            "next_scan_at": next_scan_at(cfg, last)}


@router.put("/schedule")
async def write_schedule(body: ScheduleIn,
                         user: dict = Depends(get_current_user)):
    if body.frequency not in ("daily", "weekly"):
        raise HTTPException(422, "frequency must be 'daily' or 'weekly'")
    entities = [e for e in (body.entities or []) if e in _KINDS]
    if body.enabled and not entities:
        raise HTTPException(422, "pick at least one entity kind to scan")
    doc = {"id": SCHEDULE_ID, "enabled": bool(body.enabled),
           "frequency": body.frequency, "entities": entities,
           "updated_at": now_iso(), "updated_by": user["id"]}
    coll = _schedule_coll(user)
    if await coll.find_one({"id": SCHEDULE_ID}):
        await coll.update_one({"id": SCHEDULE_ID}, {"$set": doc})
    else:
        await coll.insert_one(doc)
    await append_audit(db, {
        "tenant_id": user["tenant_id"], "id": new_id(), "at": now_iso(),
        "user_id": user["id"], "action": "dedupe.schedule_update",
        "entity": "dedupe_schedule", "entity_id": SCHEDULE_ID,
        "details": {"enabled": doc["enabled"],
                    "frequency": doc["frequency"],
                    "entities": doc["entities"]}})
    return await read_schedule(user)


@router.get("/suggestions")
async def list_suggestions(entity: str = Query("customers"),
                           status: str = Query("pending"),
                           user: dict = Depends(get_current_user)):
    if entity not in _KINDS:
        raise HTTPException(400, "entity must be 'customers' or 'vendors'")
    rows = await _suggestions(user).find_list(
        {"entity": entity, "status": status}, limit=500)
    rows.sort(key=lambda d: -d.get("score", 0))
    return {"suggestions": rows}


async def _get_suggestion(user, sid):
    doc = await _suggestions(user).find_one({"id": sid})
    if not doc:
        raise HTTPException(404, "suggestion not found")
    return doc


@router.post("/suggestions/{sid}/dismiss")
async def dismiss(sid: str, user: dict = Depends(get_current_user)):
    doc = await _get_suggestion(user, sid)
    if doc["status"] != "pending":
        raise HTTPException(409, f"suggestion is {doc['status']}")
    await _suggestions(user).update_one(
        {"id": sid}, {"$set": {"status": "dismissed",
                               "dismissed_at": now_iso(),
                               "dismissed_by": user["id"]}})
    await append_audit(db, {
        "tenant_id": user["tenant_id"], "id": new_id(), "at": now_iso(),
        "user_id": user["id"], "action": "dedupe.dismiss",
        "entity": "dedupe_suggestions", "entity_id": sid, "details": {}})
    return {"id": sid, "status": "dismissed"}


@router.post("/suggestions/{sid}/restore")
async def restore(sid: str, user: dict = Depends(get_current_user)):
    """Undo a dismissal: the suggestion returns to the pending queue
    exactly as it was. The dismissal stamps stay on the record as
    history; nothing else about the pair changes."""
    doc = await _get_suggestion(user, sid)
    if doc["status"] != "dismissed":
        raise HTTPException(409, f"suggestion is {doc['status']}")
    await _suggestions(user).update_one(
        {"id": sid}, {"$set": {"status": "pending",
                               "restored_at": now_iso(),
                               "restored_by": user["id"]}})
    await append_audit(db, {
        "tenant_id": user["tenant_id"], "id": new_id(), "at": now_iso(),
        "user_id": user["id"], "action": "dedupe.dismiss_restore",
        "entity": "dedupe_suggestions", "entity_id": sid, "details": {}})
    return {"id": sid, "status": "pending"}


class MergeIn(BaseModel):
    survivor_id: str | None = None     # default: the earlier-created record


@router.post("/suggestions/{sid}/merge")
async def merge(sid: str, body: MergeIn,
                user: dict = Depends(get_current_user)):
    """THE human-approved merge. Absorbed record is flagged merged (never
    deleted); both before-snapshots are stored for reversal."""
    sugg = await _get_suggestion(user, sid)
    if sugg["status"] != "pending":
        raise HTTPException(409, f"suggestion is {sugg['status']}")
    coll = _coll(user, sugg["entity"])
    a = await coll.find_one({"id": sugg["a_id"]})
    b = await coll.find_one({"id": sugg["b_id"]})
    if not a or not b or not _active(a) or not _active(b):
        raise HTTPException(409, "one of the records is gone or already merged")
    if body.survivor_id and body.survivor_id not in (a["id"], b["id"]):
        raise HTTPException(400, "survivor_id must be one of the pair")
    survivor, absorbed = (a, b) if (body.survivor_id in (None, a["id"])) else (b, a)
    surv_before, abs_before = dict(survivor), dict(absorbed)

    fills = {}
    for f in ("email", "phone", "address"):
        if not survivor.get(f) and absorbed.get(f):
            fills[f] = absorbed[f]
    if fills.get("address") and absorbed.get("address_parts"):
        fills["address_parts"] = absorbed["address_parts"]
    if fills:
        await coll.update_one({"id": survivor["id"]}, {"$set": fills})
        survivor = await coll.find_one({"id": survivor["id"]})
    await coll.update_one({"id": absorbed["id"]}, {"$set": {
        "status": "merged", "merged_into": survivor["id"],
        "merged_at": now_iso()}})

    merge_doc = {"id": new_id(), "entity": sugg["entity"],
                 "survivor_id": survivor["id"], "absorbed_id": absorbed["id"],
                 "survivor_before": surv_before, "absorbed_before": abs_before,
                 "suggestion_id": sid, "score": sugg.get("score"),
                 "evidence": sugg.get("evidence"), "status": "active",
                 "created_at": now_iso(), "created_by": user["id"]}
    await _merges(user).insert_one(merge_doc)
    await _suggestions(user).update_one(
        {"id": sid}, {"$set": {"status": "merged", "merge_id": merge_doc["id"]}})
    await append_audit(db, {
        "tenant_id": user["tenant_id"], "id": new_id(), "at": now_iso(),
        "user_id": user["id"], "action": "dedupe.merge",
        "entity": sugg["entity"], "entity_id": survivor["id"],
        "details": {"absorbed_id": absorbed["id"], "merge_id": merge_doc["id"],
                    "score": sugg.get("score")}})
    return {"merge_id": merge_doc["id"], "survivor": survivor,
            "absorbed_id": absorbed["id"]}


@router.get("/merges")
async def list_merges(entity: str = Query("customers"),
                      user: dict = Depends(get_current_user)):
    if entity not in _KINDS:
        raise HTTPException(400, "entity must be 'customers' or 'vendors'")
    rows = await _merges(user).find_list({"entity": entity}, limit=500)
    return {"merges": rows}


@router.post("/merges/{mid}/reverse")
async def reverse_merge(mid: str, user: dict = Depends(get_current_user)):
    merges = _merges(user)
    doc = await merges.find_one({"id": mid})
    if not doc:
        raise HTTPException(404, "merge not found")
    if doc["status"] != "active":
        raise HTTPException(409, f"merge is {doc['status']}")
    coll = _coll(user, doc["entity"])
    for snap in (doc["survivor_before"], doc["absorbed_before"]):
        await coll.c.delete_one({"tenant_id": user["tenant_id"],
                                 "id": snap["id"]})
        await coll.insert_one(dict(snap))
    await merges.update_one({"id": mid}, {"$set": {
        "status": "reversed", "reversed_at": now_iso(),
        "reversed_by": user["id"]}})
    await append_audit(db, {
        "tenant_id": user["tenant_id"], "id": new_id(), "at": now_iso(),
        "user_id": user["id"], "action": "dedupe.merge_reverse",
        "entity": doc["entity"], "entity_id": doc["survivor_id"],
        "details": {"absorbed_id": doc["absorbed_id"], "merge_id": mid}})
    return {"id": mid, "status": "reversed"}
