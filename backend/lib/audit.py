"""Audit trail writer + hash chain.

Every mutating endpoint records who did what: ``await audit(user, action,
entity, entity_id, details)`` — or, in routers with their own ``_audit``
helper, ``append_audit(db, doc)``. Rows land in the ``audit_log``
collection and are readable through the ``/api/audit`` viewer
(owner/accountant only).

Hash chain (security audit 2026-10-26, N-4 — ported from Trade
Circle's app/audit.py): each new row carries ``chain_seq`` (allocated
atomically from the ``audit_chain`` head document), ``prev_hash``
(the previous chained row's hash; the GENESIS marker for the first),
and ``hash`` over its canonical content. Editing or deleting a
chained row breaks the chain, and ``verify_audit_chain`` reports the
first break. Rows written before the chain existed carry no chain
fields: they are pre-chain history, never rewritten, and the verifier
counts them separately.

Honest limit: the chain makes the trail tamper-EVIDENT, not
tamper-proof — while the app's database user can still rewrite the
collection, a determined attacker with database write access could
recompute a chain. Restricting that user to insert/find on
``audit_log`` (the Atlas step, owner-side) is what closes that, and
it remains open, here and in Trade Circle alike.

Writers must never break the operation they record: if the chain
store is unavailable (e.g. a minimal test double), the row still
lands as a plain, unchained insert.
"""
import asyncio
import hashlib
import json

from pymongo import ReturnDocument

from db import db
from lib.base import new_id, now_iso

GENESIS_PREV_HASH = "GENESIS"

# Serializes chain appends inside this process (Trade Circle's
# pattern): the next row's prev_hash is read from the row the last
# append landed. Sequence numbers themselves are allocated atomically
# by Mongo, so a crashed append burns a seq and the gap stays visible
# to the verifier instead of silently forking the chain.
_chain_lock = asyncio.Lock()


def _canonical_details(value):
    """Deterministic form of a details payload: JSON round-trip with
    sorted keys, so the hash recomputes identically from the stored
    row (tuples become lists, non-JSON values become strings — the
    same at write time and at verify time)."""
    return json.loads(json.dumps(value or {}, sort_keys=True,
                                  default=str))


def compute_audit_hash(doc: dict) -> str:
    """Hash over the row's canonical content. Writer shapes vary
    (actor vs actor_id, details vs detail, created_at vs at) — the
    projection normalizes them, identically at write and verify."""
    payload = {
        "id": doc.get("id", ""),
        "tenant_id": doc.get("tenant_id", ""),
        "actor": doc.get("actor_id") or doc.get("actor") or "",
        "action": doc.get("action", ""),
        "entity": doc.get("entity", ""),
        "entity_id": doc.get("entity_id", ""),
        "created_at": doc.get("created_at") or doc.get("at") or "",
        "details": _canonical_details(
            doc.get("details", doc.get("detail"))),
        "chain_seq": doc.get("chain_seq"),
        "prev_hash": doc.get("prev_hash"),
    }
    canonical = json.dumps(payload, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_chain(entries: list) -> dict:
    """Walk chained entries (ascending chain_seq) and report the
    first break. Pure function of the stored entries (Trade Circle's
    verifier, ported). A break is a gap or duplicate in the sequence,
    a prev_hash that does not match the previous entry's hash, or a
    stored hash that does not recompute from the entry's content."""
    expected = 1
    prev_hash = GENESIS_PREV_HASH
    for entry in entries:
        seq = entry.get("chain_seq")
        if seq != expected:
            return {"ok": False, "checked": expected - 1,
                    "break": {"seq": seq, "reason":
                              f"expected chain_seq {expected}, found "
                              f"{seq} — an entry is missing or "
                              f"duplicated"}}
        if entry.get("prev_hash") != prev_hash:
            return {"ok": False, "checked": expected - 1,
                    "break": {"seq": seq, "reason":
                              "prev_hash does not match the previous "
                              "entry's hash — an entry was edited, "
                              "deleted, or reordered"}}
        if entry.get("hash") != compute_audit_hash(entry):
            return {"ok": False, "checked": expected - 1,
                    "break": {"seq": seq, "reason":
                              "hash does not recompute from the "
                              "stored content — the entry was "
                              "modified after it was written"}}
        prev_hash = entry["hash"]
        expected += 1
    return {"ok": True, "checked": expected - 1, "break": None}


async def _append_chained(db, doc: dict) -> dict:
    async with _chain_lock:
        head = await db.audit_chain.find_one_and_update(
            {"_id": "head"}, {"$inc": {"seq": 1}}, upsert=True,
            return_document=ReturnDocument.AFTER)
        seq = head["seq"]
        if seq == 1:
            prev_hash = GENESIS_PREV_HASH
        else:
            prev = await db.audit_log.find_one(
                {"chain_seq": seq - 1}, {"hash": 1})
            # A previous append burned this sequence without landing:
            # point at the marker so the gap stays visible to
            # verify_audit_chain instead of silently forking.
            prev_hash = (prev or {}).get("hash") or GENESIS_PREV_HASH
        full = dict(doc)
        full["chain_seq"] = seq
        full["prev_hash"] = prev_hash
        full["hash"] = compute_audit_hash(full)
        await db.audit_log.insert_one(full)
        await db.audit_chain.update_one(
            {"_id": "head"}, {"$set": {"hash": full["hash"]}})
        return full


async def append_audit(db, doc: dict) -> dict:
    """Insert one audit row, hash-chained when the chain store is
    available. Never raises: on any chain failure the row still lands
    as a plain (unchained) insert, so recording never breaks the
    operation it describes."""
    try:
        if getattr(db, "audit_chain", None) is None:
            raise RuntimeError("audit chain store unavailable")
        return await _append_chained(db, doc)
    except Exception:
        try:
            await db.audit_log.insert_one(doc)
            return doc
        except Exception:
            return {}


async def verify_audit_chain(db) -> dict:
    """Verify the stored chain: {ok, checked, break, unchained}.
    `unchained` counts the pre-chain history rows, which are
    legitimate and never rewritten."""
    entries = await db.audit_log.find(
        {"chain_seq": {"$exists": True}}, {"_id": 0}).sort(
            "chain_seq", 1).to_list(length=100000)
    result = check_chain(entries)
    result["unchained"] = await db.audit_log.count_documents(
        {"chain_seq": {"$exists": False}})
    return result


async def audit(user: dict, action: str, entity: str = "",
                entity_id: str = "", details: dict = None) -> dict:
    """Write one audit row for the user's tenant. Never raises."""
    try:
        doc = {
            "id": new_id(),
            "tenant_id": (user or {}).get("tenant_id", ""),
            "actor": (user or {}).get("id", ""),
            "actor_name": (user or {}).get("name", "") or (user or {}).get("email", ""),
            "actor_email": (user or {}).get("email", ""),
            "action": action,
            "entity": entity or "",
            "entity_id": entity_id or "",
            "details": details or {},
            "created_at": now_iso(),
        }
    except Exception:
        # Auditing must never break the operation it records.
        return {}
    return await append_audit(db, doc)
