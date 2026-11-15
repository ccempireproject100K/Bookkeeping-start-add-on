"""Receipt-match review lane (owner-approved 2026-11-14, walk item #8).

receiptmatch.py scores every scanned receipt against the bank feed.
Strong, unambiguous matches link quietly. Everything else — a weak match,
an amount that disagrees, a date too far away, two equal candidates, or
no bank twin at all once the feed should have caught up — lands HERE,
in a review queue with a plain-words reason, the same flag-and-review
pattern as the categorization lane (cat_review.py). One-tap resolves:

  * confirm_link — link the receipt to the suggested bank transaction
  * pick_txn     — link it to a different transaction the user picks
  * no_match     — the receipt stands alone (cash purchase, personal
                   card, ...); the decision is recorded, not re-flagged

Resolutions are recorded on the queue item and on the receipt itself.
A bank transaction already claimed by another receipt can never be
linked twice — the guard lives in receipts.post_receipt and is mirrored
here so a resolve fails with the same clear error.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso, to_cents
from lib import receiptmatch as RM
from routers import receipts as receipts_router

router = APIRouter(prefix="/receipt-match", tags=["receipt-match"])


def queue(user):
    return Scoped(db.receipt_match_queue, user["tenant_id"])


def _receipts(user):
    return receipts_router.receipts(user)


def _receipt_summary(doc):
    ex = doc.get("extracted") or {}
    return {
        "receipt_id": doc["id"],
        "vendor": ex.get("vendor", ""),
        "receipt_date": (ex.get("date") or "")[:10],
        "amount_cents": to_cents(ex.get("amount") or 0),
        "receipt_status": doc.get("status"),
        "match_link": doc.get("match_link"),
    }


async def _upsert_flag(user, doc, flag, best):
    """One pending queue item per (receipt, flag type) — re-running the
    scan refreshes an open flag instead of stacking duplicates."""
    q = queue(user)
    existing = await q.find_one({"receipt_id": doc["id"],
                                 "flag_type": flag["type"],
                                 "status": "pending"})
    fields = {
        "reason": flag["reason"],
        "flag_txn_id": flag.get("txn_id"),
        "best_txn_id": (best or {}).get("txn_id"),
        "best_score": (best or {}).get("score"),
        **_receipt_summary(doc),
        "updated_at": now_iso(),
    }
    if existing:
        await q.update_one({"id": existing["id"]}, {"$set": fields})
        return existing["id"]
    item = {"id": new_id(), "flag_type": flag["type"], "status": "pending",
            "created_at": now_iso(), **fields}
    await q.insert_one(item)
    return item["id"]


async def assess_and_apply(user, doc, today=None):
    """Score one scanned receipt; link it quietly when the match is
    strong and unambiguous, otherwise raise/refresh its flags.
    Returns (verdict, flags_raised)."""
    ex = doc.get("extracted") or {}
    if not ex.get("amount") or not ex.get("date"):
        return "skipped", 0
    if doc.get("match_link") or doc.get("match_reviewed"):
        return "already_decided", 0
    scored = await receipts_router.scored_candidates(user, doc)
    verdict = RM.classify(ex, scored, today=today)
    raised = 0
    for flag in verdict["flags"]:
        await _upsert_flag(user, doc, flag, verdict["best"])
        raised += 1
    if verdict["verdict"] == "strong":
        best = verdict["best"]
        await _receipts(user).update_one({"id": doc["id"]}, {"$set": {
            "match_link": {"txn_id": best["txn_id"], "score": best["score"],
                           "source": "auto", "linked_at": now_iso()},
        }})
        await audit(user, "receipt.match_auto_link", "receipt", doc["id"],
                    {"txn_id": best["txn_id"], "score": best["score"]})
        return "strong", raised
    return verdict["verdict"], raised


# ---------------- scan ----------------
@router.post("/scan")
async def scan(user: dict = Depends(require("ops:manage"))):
    """Run matching over every scanned, unposted receipt for this tenant."""
    docs = await _receipts(user).find_list({"status": "scanned"},
                                           sort=[("created_at", -1)],
                                           limit=500)
    linked = flagged = seen = 0
    for doc in docs:
        verdict, raised = await assess_and_apply(user, doc)
        if verdict == "skipped":
            continue
        seen += 1
        if verdict == "strong":
            linked += 1
        flagged += raised
    return {"receipts_checked": seen, "auto_linked": linked,
            "flags_open": flagged}


# ---------------- queue ----------------
@router.get("/queue")
async def list_queue(user: dict = Depends(require("ledger:read"))):
    items = await queue(user).find_list({"status": "pending"},
                                        sort=[("created_at", -1)], limit=500)
    out = []
    for item in items:
        doc = await _receipts(user).find_one({"id": item["receipt_id"]})
        cands = []
        if doc and doc.get("extracted"):
            scored = await receipts_router.scored_candidates(user, doc)
            cands = scored[:8]
        out.append({**item, "candidates": cands})
    return out


@router.get("/stats")
async def stats(user: dict = Depends(require("ledger:read"))):
    q = queue(user)
    all_receipts = await _receipts(user).find_list({}, limit=5000)
    return {
        "pending": await q.count({"status": "pending"}),
        "flagged_total": await q.count({}),
        "resolved_total": await q.count({"status": "resolved"}),
        "auto_linked": sum(1 for r in all_receipts
                           if (r.get("match_link") or {}).get("source") == "auto"),
        "linked_total": sum(1 for r in all_receipts if r.get("match_link")),
    }


# ---------------- resolve ----------------
class ResolveBody(BaseModel):
    action: str  # confirm_link | pick_txn | no_match
    txn_id: Optional[str] = None


async def _link(user, doc, txn_id, score, source):
    """Record the receipt<->transaction link, enforcing the one-claim
    rule: a bank transaction claimed by another receipt is refused."""
    claimed = await receipts_router.claimed_bank_txn_ids(
        user, exclude_receipt_id=doc["id"])
    if txn_id in claimed:
        raise HTTPException(
            409, "That bank transaction is already linked to another "
                 "receipt — unlink it there first, or pick a different one.")
    txn = await Scoped(db.bank_transactions, user["tenant_id"]).find_one(
        {"id": txn_id})
    if not txn:
        raise HTTPException(422, "Bank transaction not found")
    if score is None and doc.get("extracted"):
        score = RM.score_candidate(doc["extracted"], txn)["score"]
    await _receipts(user).update_one({"id": doc["id"]}, {"$set": {
        "match_link": {"txn_id": txn_id, "score": score,
                       "source": source, "linked_at": now_iso()},
    }})
    return txn


@router.post("/queue/{item_id}/resolve")
async def resolve(item_id: str, body: ResolveBody,
                  user: dict = Depends(require("ops:manage"))):
    item = await queue(user).find_one({"id": item_id})
    if not item:
        raise HTTPException(404, "Review item not found")
    if item["status"] != "pending":
        raise HTTPException(409, "Already resolved")
    doc = await _receipts(user).find_one({"id": item["receipt_id"]})
    if not doc:
        raise HTTPException(404, "Receipt not found")

    linked_txn = None
    if body.action in ("confirm_link", "pick_txn"):
        txn_id = body.txn_id or item.get("best_txn_id")
        if not txn_id:
            raise HTTPException(422, "No bank transaction to link — "
                                     "pick one, or resolve as no-match.")
        await _link(user, doc, txn_id,
                    item.get("best_score") if txn_id == item.get("best_txn_id")
                    else None,
                    source="review")
        linked_txn = txn_id
        # A linked receipt's other open flags are moot — close them too.
        others = await queue(user).find_list(
            {"receipt_id": doc["id"], "status": "pending"}, limit=100)
        for other in others:
            if other["id"] != item_id:
                await queue(user).update_one({"id": other["id"]}, {"$set": {
                    "status": "resolved",
                    "resolution_action": "superseded_by_link",
                    "resolved_at": now_iso(),
                    "resolved_by": user.get("email"),
                }})
    elif body.action == "no_match":
        await _receipts(user).update_one({"id": doc["id"]}, {"$set": {
            "match_reviewed": {"action": "no_match", "at": now_iso(),
                               "by": user.get("email")},
        }})
    else:
        raise HTTPException(422, f"Unknown action: {body.action}")

    await queue(user).update_one({"id": item_id}, {"$set": {
        "status": "resolved", "resolution_action": body.action,
        "resolution_txn_id": linked_txn,
        "resolved_at": now_iso(), "resolved_by": user.get("email"),
    }})
    await audit(user, "receipt.match_resolve", "receipt", doc["id"],
                {"action": body.action, "txn_id": linked_txn,
                 "queue_item": item_id})
    return {"status": "resolved", "action": body.action,
            "txn_id": linked_txn}
