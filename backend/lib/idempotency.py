"""HTTP Idempotency-Key enforcement for the Momo sync surface.

Momo (services/accounting.py on its side) already sends an
`Idempotency-Key: <sha256>` header on every bookkeeping push and derives
the key deterministically from tenant + local record + step, so a retry
of the same logical operation always carries the same key. Until now the
books receiving side ignored the header, and deduplication rested on
provider ids recorded on the Momo side only — a lost response could
therefore duplicate a customer, invoice, or payment here.

This module implements the receiving contract (see
INTEGRATION_CONTRACT.md at the repo root):

* One record per (tenant_id, key) in `idempotency_records`, protected by
  a unique index (created in server.py startup). The record stores the
  request hash, status, the response once known, and failure details —
  it doubles as the sync-outcome log owners can inspect.
* First delivery claims the record atomically (unique-index insert)
  before the handler runs.
* Duplicate delivery with the same payload replays the stored response
  verbatim (header `Idempotency-Replayed: true`) — no second posting.
* Same key with a *different* payload is a 409: the key identifies one
  logical operation, so a changed payload means a client bug.
* A concurrent duplicate waits (bounded) for the in-flight request and
  then replays its outcome; it never executes the handler twice.
* A `failed` record may be re-claimed by a retry, and a `processing`
  record older than PROCESSING_TTL_SECONDS is treated as abandoned
  (the claimant died) and may be taken over. Handlers additionally
  stamp created resources with the key (`sync_key`) so a retry after a
  crash between "resource created" and "response recorded" returns the
  stamped resource instead of duplicating it.
"""
import asyncio
import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError

from db import db
from lib.base import new_id, now_iso

PROCESSING_TTL_SECONDS = 120
WAIT_TIMEOUT_SECONDS = 15.0
WAIT_POLL_SECONDS = 0.1

MISMATCH_DETAIL = ("Idempotency-Key was already used with a different "
                   "request payload")
IN_PROGRESS_DETAIL = ("A request with this Idempotency-Key is still "
                      "in progress")


def request_hash(method: str, path: str, body) -> str:
    """SHA-256 over the canonical JSON of {method, path, body}.

    The body is the parsed request model (key order normalized by
    json.dumps(sort_keys=True)), so a byte-level retry and a reordered-
    keys retry hash identically, while any semantic change — including
    a different invoice id in the path — changes the hash.
    """
    canon = json.dumps(
        {"method": method, "path": path, "body": body},
        sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _age_seconds(iso_ts: str) -> float:
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


class IdempotencySession:
    """One claimed (or replayed) idempotency record.

    Handlers do::

        sess = await begin(...)
        if sess.is_replay:
            return sess.replay_response()
        try:
            result = ...existing logic...
        except HTTPException as e:
            await sess.fail(e.status_code, str(e.detail))
            raise
        return await sess.complete(result, resource_id=...)

    A session with no key is inactive: every method is a no-op and
    `complete` returns the handler result untouched, so unkeyed
    requests (the UI) behave exactly as before.
    """

    def __init__(self, record_id=None, replay=None):
        self.record_id = record_id
        self._replay = replay  # stored record dict when replaying

    @property
    def active(self) -> bool:
        return self.record_id is not None

    @property
    def is_replay(self) -> bool:
        return self._replay is not None

    def replay_response(self) -> JSONResponse:
        rec = self._replay
        return JSONResponse(
            content=rec.get("response_body"),
            status_code=rec.get("response_status") or 200,
            headers={"Idempotency-Replayed": "true"},
        )

    async def complete(self, body, resource_id=None):
        """Record a successful outcome; returns `body` unchanged."""
        if not self.active:
            return body
        try:
            await db.idempotency_records.update_one(
                {"id": self.record_id},
                {"$set": {
                    "status": "succeeded",
                    "response_status": 200,
                    "response_body": body,
                    "resource_id": resource_id,
                    "error": None,
                    "completed_at": now_iso(),
                    "updated_at": now_iso(),
                }})
        except Exception:
            # Recording must never break the operation it records; the
            # resource's sync_key stamp still protects a later retry.
            pass
        return body

    async def fail(self, status_code: int, error: str):
        """Record a failed outcome so a retry may re-execute."""
        if not self.active:
            return
        try:
            await db.idempotency_records.update_one(
                {"id": self.record_id},
                {"$set": {
                    "status": "failed",
                    "response_status": status_code,
                    "error": str(error)[:500],
                    "completed_at": now_iso(),
                    "updated_at": now_iso(),
                }})
        except Exception:
            pass


async def begin(*, tenant_id: str, key, endpoint: str, req_hash: str,
                actor: str) -> IdempotencySession:
    """Claim `key` for this tenant, or resolve to a replay / 409.

    Raises HTTPException(409) on payload mismatch or when a concurrent
    request is still processing after the wait bound.
    """
    if not key:
        return IdempotencySession()

    coll = db.idempotency_records
    now = now_iso()
    doc = {
        "id": new_id(),
        "tenant_id": tenant_id,
        "key": key,
        "endpoint": endpoint,
        "request_hash": req_hash,
        "status": "processing",
        "response_status": None,
        "response_body": None,
        "resource_id": None,
        "error": None,
        "actor": actor,
        "attempts": 1,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    try:
        await coll.insert_one(doc)
        return IdempotencySession(record_id=doc["id"])
    except DuplicateKeyError:
        pass  # someone holds (or held) this key — resolve below

    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_TIMEOUT_SECONDS
    while True:
        rec = await coll.find_one({"tenant_id": tenant_id, "key": key})
        if rec is None:
            # Record vanished mid-flight (should not happen); re-claim.
            try:
                await coll.insert_one(doc)
                return IdempotencySession(record_id=doc["id"])
            except DuplicateKeyError:
                continue
        if rec.get("request_hash") != req_hash:
            raise HTTPException(status_code=409, detail=MISMATCH_DETAIL)
        status = rec.get("status")
        if status == "succeeded":
            return IdempotencySession(replay=rec)
        if status in ("failed", "processing"):
            stale = (status == "failed"
                     or _age_seconds(rec.get("updated_at", "")) > PROCESSING_TTL_SECONDS)
            if stale:
                # Re-claim, conditional on the record not having moved
                # since we read it — exactly one racer wins this update.
                res = await coll.update_one(
                    {"tenant_id": tenant_id, "key": key,
                     "status": status, "updated_at": rec.get("updated_at")},
                    {"$set": {"status": "processing",
                              "attempts": int(rec.get("attempts", 1)) + 1,
                              "actor": actor,
                              "error": None,
                              "updated_at": now_iso()}})
                if getattr(res, "matched_count", 0):
                    return IdempotencySession(record_id=rec["id"])
        if loop.time() >= deadline:
            raise HTTPException(status_code=409, detail=IN_PROGRESS_DETAIL)
        await asyncio.sleep(WAIT_POLL_SECONDS)
