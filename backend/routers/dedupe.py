"""Duplicate-entity suggestions for clients & vendors (cleanup layer).

POST /dedupe/scan runs record linkage (lib/dataclean/linkage) over ONE
tenant's clients or vendors and stores candidate pairs as SUGGESTIONS.
Nothing here ever merges on its own: a merge happens only when a human
POSTs /dedupe/suggestions/{id}/merge. Merges are audit-logged and
REVERSIBLE — the merge record keeps full before-snapshots of both
records, and POST /dedupe/merges/{id}/reverse restores them exactly.
Linkage only ever sees the caller's own tenant (Scoped collections), so
suggestions can never cross tenants.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from lib.audit import append_audit
from lib.auth import get_current_user
from db import db
from lib.base import Scoped, new_id, now_iso

router = APIRouter(prefix="/dedupe", tags=["dedupe"])

_KINDS = {"customers": "customers", "vendors": "vendors"}


def _coll(user, kind):
    return Scoped(getattr(db, _KINDS[kind]), user["tenant_id"])


def _suggestions(user):
    return Scoped(db.dedupe_suggestions, user["tenant_id"])


def _merges(user):
    return Scoped(db.entity_merges, user["tenant_id"])


def _active(doc):
    return not (doc.get("merged_into") or doc.get("deleted_at")
                or doc.get("status") in ("merged", "deleted"))


class ScanIn(BaseModel):
    entity: str = "customers"


@router.post("/scan")
async def scan(body: ScanIn, user: dict = Depends(get_current_user)):
    if body.entity not in _KINDS:
        raise HTTPException(400, "entity must be 'customers' or 'vendors'")
    from lib.dataclean import linkage

    coll = _coll(user, body.entity)
    entities = await coll.find_list({}, limit=5000)
    entities = [e for e in entities if _active(e)]
    result = linkage.suggest_duplicates(entities)
    sugg = _suggestions(user)
    # Refresh: drop still-pending suggestions for this entity kind, then
    # store the fresh set. Merged/dismissed history is never rewritten.
    for old in await sugg.find_list(
            {"entity": body.entity, "status": "pending"}, limit=5000):
        await sugg.c.delete_one({"tenant_id": user["tenant_id"],
                                 "id": old["id"]})
    by_id = {e["id"]: e for e in entities}
    stored = []
    for pair in result["pairs"]:
        doc = {"id": new_id(), "entity": body.entity,
               "a_id": pair["a_id"], "b_id": pair["b_id"],
               "a_name": (by_id.get(pair["a_id"]) or {}).get("name"),
               "b_name": (by_id.get(pair["b_id"]) or {}).get("name"),
               "score": pair["score"], "engine": pair["engine"],
               "evidence": pair["evidence"], "status": "pending",
               "created_at": now_iso()}
        await sugg.insert_one(doc)
        stored.append(doc)
    return {"entity": body.entity, "scanned": len(entities),
            "engine": result["engine"],
            "engine_note": result.get("engine_note"),
            "suggestions": stored}


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
        {"id": sid}, {"$set": {"status": "dismissed"}})
    return {"id": sid, "status": "dismissed"}


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
