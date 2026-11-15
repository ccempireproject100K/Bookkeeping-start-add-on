"""Bill-pay PREP (Top-20 gap #14, owner-approved 2026-11-14).

One queue with due dates + amounts up front, a VA-pattern approval trail
(prepared -> reviewed -> approved, actor + timestamp, VISIBLE on the bill),
scheduling + reminders, a ready-to-pay packet, and mark-paid recording
through bills.py's existing GL posting. Books does NOT send money — the
paying half waits for a payments partner (lib/payments_rail.py).
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import billpay as BP
from lib.payments_rail import rail_status
from routers.bills import pay_bill, PayBody

router = APIRouter(prefix="/billpay", tags=["billpay"])

HONEST_NOTE = ("Books prepares and records bill payments — it does not "
               "send money. Pay outside Books, then mark the bill paid.")


def coll(user):
    return Scoped(db.bills, user["tenant_id"])


def notifs(user):
    return Scoped(db.app_notifications, user["tenant_id"])


def _balance(b):
    return b["amount_cents"] - b.get("paid_cents", 0)


def _approval_trail(b):
    """Who did what, when — from prep_history, shaped for display."""
    trail = {}
    for h in b.get("prep_history", []):
        trail[h["state"]] = {"by": h.get("actor"), "at": h.get("at")}
    return trail


def _public(b, today):
    b = {k: v for k, v in b.items() if k != "_id"}
    b["paid_cents"] = b.get("paid_cents", 0)
    b["balance_cents"] = _balance(b)
    b["balance"] = cents_to_dollars(b["balance_cents"])
    b["amount"] = cents_to_dollars(b["amount_cents"])
    b["bucket"] = BP.bucket_for(b.get("due_date"), today)
    b["discount"] = BP.discount_info(b, today)
    b["prep_state"] = BP.prep_state(b)
    b["approval"] = _approval_trail(b)
    return b


async def _open_bills(user):
    rows = await coll(user).find_list(sort=[("due_date", 1)], limit=1000)
    return [b for b in rows if _balance(b) > 0]


async def _cash_position(user):
    """Recorded bank balances from the books (NOT a live bank feed)."""
    bals = await L._balances(db, user["tenant_id"], as_of=today_str())
    accounts = await Scoped(db.bank_accounts, user["tenant_id"]).find_list(
        sort=[("created_at", 1)])
    out, total = [], 0
    for a in accounts:
        if a.get("kind") != "bank":
            continue
        v = bals.get(a["gl_code"], {"debit": 0, "credit": 0})
        net = L._net_balance(a["gl_code"], v["debit"], v["credit"])
        total += net
        out.append({"name": a.get("name", ""), "gl_code": a["gl_code"],
                    "recorded_balance_cents": net,
                    "recorded_balance": cents_to_dollars(net)})
    return {"accounts": out, "total_cents": total,
            "total": cents_to_dollars(total),
            "label": "Recorded balances from the books — not a live bank feed"}


# ---------------- Queue ----------------
@router.get("/queue")
async def pay_queue(vendor: Optional[str] = None, job_id: Optional[str] = None,
                    user: dict = Depends(get_current_user)):
    today = today_str()
    bills = [_public(b, today) for b in await _open_bills(user)]
    if vendor:
        bills = [b for b in bills if (b.get("vendor") or "").lower() == vendor.lower()]
    if job_id:
        bills = [b for b in bills if (b.get("job_id") or "") ==
                 ("" if job_id == "__none" else job_id)]
    buckets = {"due_now": [], "due_this_week": [], "upcoming": []}
    for b in bills:
        buckets[b["bucket"]].append(b)

    def totals(rows):
        return {"count": len(rows),
                "balance_cents": sum(r["balance_cents"] for r in rows)}

    by_vendor, by_job = {}, {}
    for b in bills:
        by_vendor.setdefault(b.get("vendor", ""), []).append(b)
        by_job.setdefault(b.get("job_id") or "", []).append(b)
    return {
        "note": HONEST_NOTE,
        "rail": rail_status(),
        "cash_position": await _cash_position(user),
        "buckets": {k: {"bills": v, **totals(v)} for k, v in buckets.items()},
        "total": totals(bills),
        "by_vendor": [{"vendor": k, **totals(v)} for k, v in
                      sorted(by_vendor.items(), key=lambda kv: kv[0])],
        "by_job": [{"job_id": k, "job_label": k or "No job", **totals(v)}
                   for k, v in sorted(by_job.items(), key=lambda kv: kv[0])],
        "filters": {"vendor": vendor, "job_id": job_id},
    }


# ---------------- Approval workflow ----------------
class TransitionBody(BaseModel):
    target: str                       # prepared | reviewed | approved


async def _get_bill(user, bill_id):
    bill = await coll(user).find_one({"id": bill_id})
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    return bill


async def _do_transition(user, bill, target):
    if target not in BP.PREP_STATES:
        raise HTTPException(status_code=422, detail=f"Unknown prep state '{target}'")
    if _balance(bill) <= 0:
        raise HTTPException(status_code=409, detail="Bill already paid")
    cur = BP.prep_state(bill)
    if cur == target:
        raise HTTPException(status_code=409, detail=f"Bill already {target}")
    if not BP.can_transition(bill, target):
        nxt = BP.NEXT_STATE.get(cur)
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move {cur or 'unprepared'} -> {target}; next step is {nxt}")
    actor = user.get("name") or user.get("email", "")
    entry = {"state": target, "actor": actor, "actor_id": user.get("id"),
             "at": now_iso()}
    await coll(user).update_one({"id": bill["id"]}, {
        "$set": {"prep_state": target}, "$push": {"prep_history": entry}})
    bill["prep_state"] = target
    bill.setdefault("prep_history", []).append(entry)
    return bill


@router.post("/bills/{bill_id}/transition")
async def transition(bill_id: str, body: TransitionBody,
                     user: dict = Depends(require("ops:manage"))):
    bill = await _do_transition(user, await _get_bill(user, bill_id), body.target)
    return _public(bill, today_str())


class BatchBody(BaseModel):
    bill_ids: List[str]
    target: str


@router.post("/batch")
async def batch_transition(body: BatchBody,
                           user: dict = Depends(require("ops:manage"))):
    results = []
    for bid in body.bill_ids:
        try:
            bill = await _do_transition(user, await _get_bill(user, bid), body.target)
            results.append({"bill_id": bid, "ok": True,
                            "prep_state": bill["prep_state"],
                            "approval": _approval_trail(bill)})
        except HTTPException as e:
            results.append({"bill_id": bid, "ok": False, "error": e.detail})
    return {"results": results,
            "succeeded": len([r for r in results if r["ok"]]),
            "failed": len([r for r in results if not r["ok"]])}


class ScheduleBody(BaseModel):
    pay_date: str


@router.post("/bills/{bill_id}/schedule")
async def schedule(bill_id: str, body: ScheduleBody,
                   user: dict = Depends(require("ops:manage"))):
    bill = await _get_bill(user, bill_id)
    if BP.prep_state(bill) != "approved":
        raise HTTPException(status_code=409,
                            detail="Only approved bills can be scheduled")
    await coll(user).update_one({"id": bill_id},
                                {"$set": {"pay_date_planned": body.pay_date}})
    bill["pay_date_planned"] = body.pay_date
    return _public(bill, today_str())


# ---------------- Reminders ----------------
async def _notify(user, kind, title, body, dedupe_key, bill):
    existing = await notifs(user).find_one({"dedupe_key": dedupe_key})
    if existing:
        return False
    await notifs(user).insert_one({
        "id": new_id(), "user_id": None, "tenant_wide": True, "kind": kind,
        "title": title, "body": body, "link": "/bill-pay", "read": False,
        "dedupe_key": dedupe_key, "bill_id": bill["id"],
        "vendor": bill.get("vendor", ""), "created_at": now_iso(),
    })
    return True


@router.post("/reminders/run")
async def run_reminders(user: dict = Depends(require("ops:manage"))):
    """Fire due-date and discount-window reminders — once each (deduped),
    and quiet for anything already paid."""
    today = today_str()
    created = 0
    for b in await _open_bills(user):
        if BP.prep_state(b) == "approved" and b.get("pay_date_planned") \
                and b["pay_date_planned"] <= today:
            if await _notify(
                    user, "bill_pay_date_due",
                    f"Pay date reached — {b.get('vendor', '')}",
                    f"Bill of ${cents_to_dollars(_balance(b)):,.2f} was planned "
                    f"for {b['pay_date_planned']}. Pay it outside Books, then "
                    f"mark it paid here.",
                    f"billpay-duedate:{b['id']}:{b['pay_date_planned']}", b):
                created += 1
        info = BP.discount_info(b, today)
        if info and info["window_open"] and info["days_left"] <= 2:
            if await _notify(
                    user, "bill_discount_closing",
                    f"Early-pay discount closing — {b.get('vendor', '')}",
                    f"Pay by {info['discount_date']} to save "
                    f"${cents_to_dollars(info['save_cents']):,.2f} "
                    f"({info['percent']}% early-pay discount).",
                    f"billpay-discount:{b['id']}:{info['discount_date']}", b):
                created += 1
    return {"created": created}


@router.get("/notifications")
async def list_notifications(user: dict = Depends(get_current_user)):
    rows = await notifs(user).find_list(sort=[("created_at", -1)], limit=100)
    rows = [r for r in rows if str(r.get("kind", "")).startswith("bill_")
            or r.get("link") == "/bill-pay"]
    return {"notifications": rows,
            "unread": len([r for r in rows if not r.get("read")])}


@router.post("/notifications/{notif_id}/read")
async def mark_read(notif_id: str, user: dict = Depends(get_current_user)):
    res = await notifs(user).update_one({"id": notif_id}, {"$set": {"read": True}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"read": True}


# ---------------- Ready-to-pay packet ----------------
@router.get("/packet")
async def packet(format: str = Query("csv"), vendor: Optional[str] = None,
                 job_id: Optional[str] = None,
                 user: dict = Depends(get_current_user)):
    bills = [b for b in await _open_bills(user)
             if BP.prep_state(b) == "approved"]
    if vendor:
        bills = [b for b in bills if (b.get("vendor") or "").lower() == vendor.lower()]
    if job_id:
        bills = [b for b in bills if (b.get("job_id") or "") ==
                 ("" if job_id == "__none" else job_id)]
    rows = BP.packet_rows(bills)
    if format == "pdf":
        return Response(content=BP.packet_pdf(rows, today_str()),
                        media_type="application/pdf",
                        headers={"Content-Disposition":
                                 "attachment; filename=ready-to-pay.pdf"})
    return PlainTextResponse(
        BP.packet_csv(rows), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ready-to-pay.csv"})


# ---------------- Mark paid (recording only) ----------------
class MarkPaidBody(BaseModel):
    payment_method: str = "bank"      # bank | card | cash
    reference: Optional[str] = None   # check no. / transfer reference
    pay_date: Optional[str] = None
    amount: Optional[float] = None    # None = full remaining balance


@router.post("/bills/{bill_id}/mark-paid")
async def mark_paid(bill_id: str, body: MarkPaidBody,
                    user: dict = Depends(require("ops:manage"))):
    """Record a payment the shop made OUTSIDE Books, through bills.py's
    existing GL posting path. A fully-paid bill refuses (409) — no doubles."""
    result = await pay_bill(bill_id, PayBody(
        payment_method=body.payment_method, amount=body.amount,
        pay_date=body.pay_date, reference=body.reference), user)
    return {"recorded": True, **result}


# ---------------- Partner seam ----------------
@router.get("/rail/status")
async def rail(user: dict = Depends(get_current_user)):
    return rail_status()
