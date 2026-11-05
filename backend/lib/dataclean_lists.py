"""DataClean stored lists + the Studio merge-approval queue (P7).

A LIST is a tenant-owned record set living outside the books' own
customers/vendors: an export from a CRM, a spreadsheet, a connector
pull. Lists are the unit everything else in the power-up works on —
sync scans (small lists), async jobs (P1), continuous re-scans (P3),
connector write-back (P4).

MERGE LAW — mirrored exactly from the internal console
(routers/dedupe.py), per the crm-data-cleanup skill: merges are
human-invoked only; the survivor keeps its own values and gains ONLY
the absorbed record's values for fields it lacks (fill-blanks over
email / phone / address, plus address_parts when the address is
filled); the absorbed record is flagged merged, NEVER deleted; both
pre-merge snapshots are stored on the merge document; reversal
restores both snapshots byte-exact and marks the merge reversed.
Nothing in this module merges on its own — scans only store pending
suggestions, exactly like the internal scanner.

Collections (all tenant-scoped via Scoped):
  dataclean_lists            one doc per list
  dataclean_list_records     one doc per record (list_id + fields)
  dataclean_list_suggestions pending/dismissed/merged pair queue
  dataclean_list_merges      merge documents with full snapshots
"""
from db import db
from lib.audit import append_audit
from lib.base import Scoped, new_id, now_iso
from lib.dataclean import linkage

RETENTION_WORDING = ("Stored lists are kept until you delete them. "
                     "Deleting a list permanently removes its records, "
                     "suggestions and merge history.")

MAX_RECORDS_PER_LIST = 500000


def _lists(actor):
    return Scoped(db.dataclean_lists, actor["tenant_id"])


def _records(actor):
    return Scoped(db.dataclean_list_records, actor["tenant_id"])


def _suggestions(actor):
    return Scoped(db.dataclean_list_suggestions, actor["tenant_id"])


def _merges(actor):
    return Scoped(db.dataclean_list_merges, actor["tenant_id"])


def _audit(actor, action, entity_id, details):
    return append_audit(db, {
        "tenant_id": actor["tenant_id"], "id": new_id(), "at": now_iso(),
        "user_id": actor.get("id"), "action": action,
        "entity": "dataclean_list", "entity_id": entity_id,
        "details": details})


def active(doc):
    return not (doc.get("merged_into") or doc.get("deleted_at")
                or doc.get("status") in ("merged", "deleted"))


# ------------------------------------------------------------------ lists

async def create_list(actor, name, kind="records", source="api",
                      retention=RETENTION_WORDING):
    doc = {"id": new_id(), "name": name or "Untitled list",
           "kind": kind, "source": source, "record_count": 0,
           "retention": retention,
           "created_at": now_iso(), "created_by": actor.get("id"),
           "last_scan_at": None, "last_job_id": None}
    await _lists(actor).insert_one(doc)
    return doc


async def get_list(actor, list_id):
    return await _lists(actor).find_one({"id": list_id})


async def list_lists(actor):
    rows = await _lists(actor).find_list({}, limit=500)
    rows.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return rows


async def append_records(actor, list_doc, records):
    """Append one chunk of records (router enforces the chunk size).
    Each record keeps its submitted fields verbatim; the list-scoped
    record id is the caller's `id` when given, else minted."""
    coll = _records(actor)
    docs = []
    for i, r in enumerate(records):
        doc = {k: v for k, v in r.items()}
        doc["id"] = str(r.get("id") or new_id())
        doc["list_id"] = list_doc["id"]
        doc.setdefault("status", "active")
        docs.append(doc)
    if docs:
        await coll.insert_many(docs)
        await _lists(actor).update_one(
            {"id": list_doc["id"]},
            {"$inc": {"record_count": len(docs)}})
    return len(docs)


async def list_records(actor, list_id, limit=0):
    return await _records(actor).find_list({"list_id": list_id},
                                           limit=limit)


async def delete_list(actor, list_id):
    """Cascade delete: records, suggestions, merges, and the list doc.
    Provably gone = every collection is swept by list_id, and the
    counts removed are returned as the proof."""
    counts = {}
    for name, coll in (("records", _records(actor)),
                       ("suggestions", _suggestions(actor)),
                       ("merges", _merges(actor))):
        rows = await coll.find_list({"list_id": list_id}, limit=1000000)
        for r in rows:
            await coll.c.delete_one({"tenant_id": actor["tenant_id"],
                                     "id": r["id"]})
        counts[name] = len(rows)
    await _lists(actor).c.delete_one({"tenant_id": actor["tenant_id"],
                                      "id": list_id})
    counts["lists"] = 1
    await _audit(actor, "dataclean.list_delete", list_id, counts)
    return counts


# ------------------------------------------------------------------ scans

async def store_scan_results(actor, list_id, result, mode="within",
                             other_list_id=None, job_id=None,
                             entities_by_id=None):
    """Persist a scan's pairs as pending suggestions, refreshing the
    still-pending set for this list/mode (merged/dismissed history is
    never rewritten — the internal scanner's rule)."""
    sugg = _suggestions(actor)
    stale_q = {"list_id": list_id, "mode": mode, "status": "pending"}
    if mode == "cross":
        stale_q["other_list_id"] = other_list_id
    for old in await sugg.find_list(stale_q, limit=1000000):
        await sugg.c.delete_one({"tenant_id": actor["tenant_id"],
                                 "id": old["id"]})
    by_id = entities_by_id or {}
    stored = []
    for pair in list(result.get("pairs", [])) + \
            list(result.get("review_pairs", [])):
        a = by_id.get(pair["a_id"]) or {}
        b = by_id.get(pair["b_id"]) or {}
        doc = {"id": new_id(), "list_id": list_id,
               "other_list_id": other_list_id, "mode": mode,
               "a_id": pair["a_id"], "b_id": pair["b_id"],
               # Cross pairs from the engine always carry the A-side
               # (this list) as a_id and the other list as b_id.
               "a_list_id": list_id,
               "b_list_id": other_list_id if mode == "cross" else list_id,
               "a_name": a.get("name"), "b_name": b.get("name"),
               "score": pair["score"], "engine": pair.get("engine"),
               "evidence": pair["evidence"],
               "band": pair.get("band", "suggested"),
               "status": "pending", "job_id": job_id,
               "created_at": now_iso()}
        await sugg.insert_one(doc)
        stored.append(doc)
    await _lists(actor).update_one(
        {"id": list_id}, {"$set": {"last_scan_at": now_iso(),
                                   "last_job_id": job_id}})
    return stored


async def scan_list(actor, list_doc, other_list_doc=None):
    """Synchronous scan for small lists (the router enforces the size
    ceiling and routes big lists to jobs). Returns the engine result;
    suggestions are stored as a side effect, like the internal scan."""
    entities = await list_records(actor, list_doc["id"], limit=1000000)
    entities = [e for e in entities if active(e)]
    if other_list_doc is None:
        result = linkage.suggest_duplicates(entities)
        by_id = {e["id"]: e for e in entities}
        await store_scan_results(actor, list_doc["id"], result,
                                 entities_by_id=by_id)
        return result
    others = await list_records(actor, other_list_doc["id"],
                                limit=1000000)
    others = [e for e in others if active(e)]
    result = linkage.suggest_duplicates_cross(entities, others)
    by_id = {e["id"]: e for e in entities}
    by_id.update({e["id"]: e for e in others})
    await store_scan_results(actor, list_doc["id"], result, mode="cross",
                             other_list_id=other_list_doc["id"],
                             entities_by_id=by_id)
    return result


async def list_suggestions(actor, list_id, status="pending", band=None,
                           offset=0, limit=100):
    q = {"list_id": list_id, "status": status}
    if band:
        q["band"] = band
    rows = await _suggestions(actor).find_list(q, limit=1000000)
    rows.sort(key=lambda d: -d.get("score", 0))
    return rows[offset:offset + limit], len(rows)


# ----------------------------------------------------------------- merges

async def _get_suggestion(actor, list_id, sid):
    doc = await _suggestions(actor).find_one(
        {"id": sid, "list_id": list_id})
    return doc


async def dismiss_suggestion(actor, list_id, sid):
    doc = await _get_suggestion(actor, list_id, sid)
    if not doc:
        return None, "not_found"
    if doc["status"] != "pending":
        return None, f"suggestion is {doc['status']}"
    await _suggestions(actor).update_one(
        {"id": sid}, {"$set": {"status": "dismissed",
                               "dismissed_at": now_iso(),
                               "dismissed_by": actor.get("id")}})
    await _audit(actor, "dataclean.list_dismiss", list_id,
                 {"suggestion_id": sid})
    return {"id": sid, "status": "dismissed"}, None


async def restore_suggestion(actor, list_id, sid):
    doc = await _get_suggestion(actor, list_id, sid)
    if not doc:
        return None, "not_found"
    if doc["status"] != "dismissed":
        return None, f"suggestion is {doc['status']}"
    await _suggestions(actor).update_one(
        {"id": sid}, {"$set": {"status": "pending",
                               "restored_at": now_iso(),
                               "restored_by": actor.get("id")}})
    await _audit(actor, "dataclean.list_dismiss_restore", list_id,
                 {"suggestion_id": sid})
    return {"id": sid, "status": "pending"}, None


async def merge_suggestion(actor, list_id, sid, survivor_id=None):
    """THE human-approved merge for stored lists — semantics mirrored
    from routers/dedupe.py: fill-blanks only, absorbed flagged never
    deleted, both snapshots stored for reversal."""
    sugg = await _get_suggestion(actor, list_id, sid)
    if not sugg:
        return None, "not_found"
    if sugg["status"] != "pending":
        return None, f"suggestion is {sugg['status']}"
    records = _records(actor)
    a = await records.find_one({"id": sugg["a_id"],
                                "list_id": sugg.get("a_list_id", list_id)})
    b = await records.find_one({"id": sugg["b_id"],
                                "list_id": sugg.get("b_list_id", list_id)})
    if not a or not b or not active(a) or not active(b):
        return None, "one of the records is gone or already merged"
    if survivor_id and survivor_id not in (a["id"], b["id"]):
        return None, "survivor_id must be one of the pair"
    survivor, absorbed = (a, b) if survivor_id in (None, a["id"]) else (b, a)
    surv_before, abs_before = dict(survivor), dict(absorbed)

    fills = {}
    for f in ("email", "phone", "address"):
        if not survivor.get(f) and absorbed.get(f):
            fills[f] = absorbed[f]
    if fills.get("address") and absorbed.get("address_parts"):
        fills["address_parts"] = absorbed["address_parts"]
    if fills:
        await records.update_one({"id": survivor["id"]}, {"$set": fills})
        survivor = await records.find_one({"id": survivor["id"]})
    await records.update_one({"id": absorbed["id"]}, {"$set": {
        "status": "merged", "merged_into": survivor["id"],
        "merged_at": now_iso()}})

    merge_doc = {"id": new_id(), "list_id": list_id,
                 "other_list_id": sugg.get("other_list_id"),
                 "mode": sugg.get("mode", "within"),
                 "survivor_id": survivor["id"],
                 "absorbed_id": absorbed["id"],
                 "survivor_list_id": sugg.get("a_list_id")
                 if survivor["id"] == sugg["a_id"]
                 else sugg.get("b_list_id"),
                 "absorbed_list_id": sugg.get("b_list_id")
                 if survivor["id"] == sugg["a_id"]
                 else sugg.get("a_list_id"),
                 "survivor_before": surv_before,
                 "absorbed_before": abs_before,
                 "fills": fills,
                 "suggestion_id": sid, "score": sugg.get("score"),
                 "evidence": sugg.get("evidence"), "status": "active",
                 "created_at": now_iso(), "created_by": actor.get("id")}
    await _merges(actor).insert_one(merge_doc)
    await _suggestions(actor).update_one(
        {"id": sid}, {"$set": {"status": "merged",
                               "merge_id": merge_doc["id"]}})
    await _audit(actor, "dataclean.list_merge", list_id,
                 {"absorbed_id": absorbed["id"], "merge_id": merge_doc["id"],
                  "score": sugg.get("score")})
    return {"merge_id": merge_doc["id"], "survivor": survivor,
            "absorbed_id": absorbed["id"]}, None


async def list_merges(actor, list_id):
    rows = await _merges(actor).find_list({"list_id": list_id},
                                          limit=100000)
    rows.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return rows


async def reverse_merge(actor, list_id, mid):
    """Restore both snapshots byte-exact (delete + re-insert, the
    internal console's method); the merge is marked reversed, never
    removed."""
    merges = _merges(actor)
    doc = await merges.find_one({"id": mid, "list_id": list_id})
    if not doc:
        return None, "not_found"
    if doc["status"] != "active":
        return None, f"merge is {doc['status']}"
    records = _records(actor)
    for snap in (doc["survivor_before"], doc["absorbed_before"]):
        await records.c.delete_one({"tenant_id": actor["tenant_id"],
                                    "id": snap["id"]})
        await records.insert_one(dict(snap))
    await merges.update_one({"id": mid}, {"$set": {
        "status": "reversed", "reversed_at": now_iso(),
        "reversed_by": actor.get("id")}})
    await _audit(actor, "dataclean.list_merge_reverse", list_id,
                 {"absorbed_id": doc["absorbed_id"], "merge_id": mid})
    return {"id": mid, "status": "reversed"}, None


async def export_decisions(actor, list_id):
    """Merge-decision export for pure-API customers: every merge and
    dismissal decision on the list, with the evidence it was made on,
    so an external system can replay the same decisions against its
    own copy of the records."""
    merges = await list_merges(actor, list_id)
    sugg = await _suggestions(actor).find_list(
        {"list_id": list_id, "status": "dismissed"}, limit=100000)
    return {
        "list_id": list_id,
        "exported_at": now_iso(),
        "merges": [{
            "merge_id": m["id"], "status": m["status"],
            "mode": m.get("mode"), "survivor_id": m["survivor_id"],
            "absorbed_id": m["absorbed_id"],
            "survivor_list_id": m.get("survivor_list_id"),
            "absorbed_list_id": m.get("absorbed_list_id"),
            "fills": m.get("fills", {}), "score": m.get("score"),
            "evidence": m.get("evidence"),
            "created_at": m.get("created_at"),
            "created_by": m.get("created_by"),
            "reversed_at": m.get("reversed_at"),
        } for m in merges],
        "dismissals": [{
            "suggestion_id": s["id"], "a_id": s["a_id"],
            "b_id": s["b_id"], "score": s.get("score"),
            "evidence": s.get("evidence"),
            "dismissed_at": s.get("dismissed_at"),
            "dismissed_by": s.get("dismissed_by"),
        } for s in sugg],
    }
