"""Invoice Assistant — rundown greeting + scheduled in-app rundown
notifications (Bookkeeping InvoiceEase, owner addition 2026-11-14).

The Bookkeeping USER (the shop owner) gets:
  * A login greeting panel (Invoices surface) computed LIVE from the real
    invoice data — never hardcoded.
  * Scheduled rundown notifications delivered INSIDE the app only. The app
    had NO notification mechanism before this module, so this builds the
    minimal in-app store (`app_notifications` collection) + list/read UI
    endpoints. No SMS, no email — in-app is free. A true phone push path
    would need push infrastructure the app does not have; that is a labeled
    seam (see PUSH NOTE below), not faked here.

Frequency is user-choosable per tenant+user: off / daily / twice_daily,
with times. DEFAULT: off — the login greeting panel covers the "on login"
ask without spamming anyone; scheduled rundowns are opt-in in Settings.

Rundown definitions (from the app's real invoice states):
  open          status issued|partial with a balance
  sent_waiting  open AND at least one send recorded (waiting on the client)
  pending       open AND never sent yet
  viewed        open AND viewed (first_viewed_at or a viewed timeline event)
  overdue       open AND due_date before today
  issued_today  created today (split parents excluded — children are real)
  paid_today    payments[] entries dated today (count + cents)
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import db
from lib.audit import audit
from lib.auth import get_current_user, require
from lib.base import Scoped, new_id, now_iso

router = APIRouter(tags=["invoice-assistant"])

DEFAULT_SETTINGS = {"frequency": "off", "times": ["09:00"]}
FREQUENCIES = ("off", "daily", "twice_daily")

# After a user COMPLETES the full monster walkthrough, the assistant stops
# proactively offering it for this many days (owner rule, 2026-11-14). The
# walkthrough stays available on demand forever, and single-topic help is
# never suppressed — only the proactive offer is.
WALKTHROUGH_RESURFACE_DAYS = 21
MAX_PER_FREQUENCY = {"off": 0, "daily": 1, "twice_daily": 2}

# PUSH NOTE (labeled seam): phone push (APNs/FCM) needs device-token
# registration + a push provider, which this app does not have. Rundowns
# are delivered to the in-app store only; a push transport can subscribe
# to the same `app_notifications` inserts later without changing callers.


def _cfg_coll(tenant_id):
    return Scoped(db.invoice_assistant_config, tenant_id)


def _notif_coll(tenant_id):
    return Scoped(db.app_notifications, tenant_id)


def _parse_now(now: Optional[str]) -> datetime:
    if now:
        try:
            dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(422, "now must be an ISO timestamp")
    return datetime.now(timezone.utc)


def _was_sent(inv) -> bool:
    if inv.get("sends"):
        return True
    return any(e.get("event") in ("sent", "reminder_sent")
               for e in inv.get("timeline", []))


def _was_viewed(inv) -> bool:
    if inv.get("first_viewed_at"):
        return True
    return any(e.get("event") == "viewed" for e in inv.get("timeline", []))


async def compute_rundown(tenant_id: str, now: Optional[str] = None) -> dict:
    dt = _parse_now(now)
    today = dt.date().isoformat()
    invs = await Scoped(db.invoices, tenant_id).find_list(limit=5000)

    open_invs = [i for i in invs
                 if i.get("status") in ("issued", "partial")
                 and (i.get("total_cents", 0) - i.get("paid_cents", 0)) > 0]
    sent_waiting = [i for i in open_invs if _was_sent(i)]
    pending = [i for i in open_invs if not _was_sent(i)]
    viewed = [i for i in open_invs if _was_viewed(i)]
    overdue = [i for i in open_invs
               if i.get("due_date") and i["due_date"] < today]
    issued_today = [i for i in invs
                    if i.get("status") != "split"
                    and (i.get("created_at") or "")[:10] == today]

    paid_today_cents = 0
    paid_today_count = 0
    for i in invs:
        todays = [p for p in i.get("payments", []) if p.get("date") == today]
        if todays:
            paid_today_count += 1
            paid_today_cents += sum(p.get("amount_cents", 0) for p in todays)

    outstanding = sum(i.get("total_cents", 0) - i.get("paid_cents", 0)
                      for i in open_invs)

    # ---- Assistant helps (gaps research 2026-11-14) — all data-backed ----
    def bal(i):
        return i.get("total_cents", 0) - i.get("paid_cents", 0)

    # #12: opened repeatedly, still unpaid -> good time to call
    follow_up = []
    for i in open_invs:
        views = sum(1 for e in i.get("timeline", [])
                    if e.get("event") == "viewed")
        if views >= 2:
            follow_up.append({"invoice_id": i["id"],
                              "client_name": i.get("client_name", ""),
                              "views": views, "balance_cents": bal(i)})
    follow_up.sort(key=lambda x: -x["views"])

    # #7: autopay failed / card declined, with the recorded reason
    autopay_alerts = [
        {"invoice_id": i["id"], "client_name": i.get("client_name", ""),
         "error": i.get("autopay_last_error"),
         "at": i.get("autopay_failed_at")}
        for i in invs if i.get("autopay_last_error")]

    # #8: split installments due soon or overdue (parts are real invoices)
    from datetime import timedelta
    soon = (dt.date() + timedelta(days=7)).isoformat()
    installments = [
        {"invoice_id": i["id"], "client_name": i.get("client_name", ""),
         "label": i.get("split_label") or "Installment",
         "due_date": i.get("due_date"),
         "overdue": bool(i.get("due_date") and i["due_date"] < today),
         "balance_cents": bal(i)}
        for i in open_invs
        if i.get("split_parent_id") and i.get("due_date")
        and i["due_date"] <= soon]

    # #3: money-movement visibility — failed payments + disputes on the
    # timeline (visibility only; NO no-holds promise, per owner ruling)
    money_flags = []
    disputes = []
    for i in invs:
        for e in i.get("timeline", []):
            ev = e.get("event")
            if ev == "payment_failed":
                money_flags.append({
                    "invoice_id": i["id"],
                    "client_name": i.get("client_name", ""),
                    "kind": "payment_failed",
                    "detail": (e.get("detail") or {}).get("error", ""),
                    "at": e.get("at")})
            elif ev == "dispute_opened":
                d = e.get("detail") or {}
                disputes.append({
                    "invoice_id": i["id"],
                    "client_name": i.get("client_name", ""),
                    "dispute_id": d.get("dispute_id"),
                    "reason": d.get("reason", ""),
                    "amount_cents": d.get("amount_cents"),
                    "packet_ready": True,
                    "packet_url": f"/api/invoices/{i['id']}/dispute-packet",
                    "at": e.get("at")})

    # DataClean (gaps #11): possible duplicate transactions, counted with
    # EXACTLY the rule cleanup.py's scan uses — same (abs amount, date)
    # key + similar description (first 20 chars) over bank_transactions —
    # so this number matches what the Cleanup/DataClean surface shows.
    txns = await Scoped(db.bank_transactions, tenant_id).find_list(
        limit=5000)
    seen = {}
    duplicates_flagged = 0
    for t in txns:
        amt = abs(t.get("amount_cents", 0))
        d = (t.get("txn_date") or "")[:10]
        desc = (t.get("description") or "").strip().lower()
        key = (amt, d)
        if key in seen and desc and seen[key].lower()[:20] == desc[:20]:
            duplicates_flagged += 1
        else:
            seen[key] = desc

    # #2: fee savings, bank vs card, last 30 days, from recorded payments.
    # Estimate basis is labeled: card processing at the industry-standard
    # 2.9% + 30c vs the bank payments' recorded platform fees.
    cutoff = (dt.date() - timedelta(days=30)).isoformat()
    bank_paid_30d = 0
    fee_savings_30d = 0
    for i in invs:
        for p in i.get("payments", []):
            if p.get("date", "") < cutoff or p.get("method") != "ach":
                continue
            amt = p.get("amount_cents", 0)
            bank_paid_30d += amt
            card_est = round(amt * 0.029) + 30
            fee_savings_30d += max(0, card_est
                                   - (p.get("platform_fee_cents") or 0))

    return {
        "date": today,
        "open": len(open_invs),
        "sent_waiting": len(sent_waiting),
        "pending": len(pending),
        "viewed": len(viewed),
        "overdue": len(overdue),
        "issued_today": len(issued_today),
        "paid_today_count": paid_today_count,
        "paid_today_cents": paid_today_cents,
        "outstanding_cents": outstanding,
        "follow_up": follow_up[:5],
        "autopay_alerts": autopay_alerts,
        "installments": installments,
        "installments_overdue": sum(1 for x in installments if x["overdue"]),
        "money_flags": money_flags[-10:],
        "disputes": disputes,
        "duplicates_flagged": duplicates_flagged,
        "duplicates_link": "/cleanup",
        "bank_paid_cents_30d": bank_paid_30d,
        "est_fee_savings_cents_30d": fee_savings_30d,
        "fee_savings_basis": ("Estimate: card processing at 2.9% + 30¢ vs "
                               "recorded bank-payment fees, last 30 days."),
        "link": "/invoices",
    }


def rundown_text(r: dict) -> str:
    def money(c):
        return f"${c / 100:,.2f}"
    return (f"You have {r['open']} invoices open — {r['sent_waiting']} sent and "
            f"waiting, {r['pending']} pending (not sent yet), {r['overdue']} "
            f"overdue. {r['issued_today']} issued today, "
            f"{money(r['outstanding_cents'])} outstanding, "
            f"{money(r['paid_today_cents'])} paid today.")


# ---------------- endpoints ----------------

@router.get("/invoice-assistant/rundown")
async def get_rundown(user: dict = Depends(get_current_user)):
    r = await compute_rundown(user["tenant_id"])
    return {**r, "text": rundown_text(r)}


def _walkthrough_state(doc: Optional[dict]) -> dict:
    """Proactive-offer state for the FULL monster walkthrough.

    offer_full_walkthrough is False for WALKTHROUGH_RESURFACE_DAYS after a
    recorded completion; the walkthrough itself is always available on
    demand and single-topic help is never affected by this flag.
    """
    from datetime import timedelta
    completed = (doc or {}).get("walkthrough_completed_at")
    suppressed_until = None
    offer = True
    if completed:
        try:
            done_on = datetime.fromisoformat(
                str(completed).replace("Z", "+00:00")).date()
            until = done_on + timedelta(days=WALKTHROUGH_RESURFACE_DAYS)
            suppressed_until = until.isoformat()
            offer = datetime.now(timezone.utc).date() >= until
        except ValueError:
            completed = None
    return {
        "walkthrough_completed_at": completed,
        "walkthrough_suppressed_until": suppressed_until,
        "offer_full_walkthrough": offer,
    }


async def get_settings(tenant_id: str, user_id: str) -> dict:
    doc = await _cfg_coll(tenant_id).find_one({"user_id": user_id})
    if not doc:
        return {**DEFAULT_SETTINGS, "user_id": user_id,
                **_walkthrough_state(None)}
    return {"frequency": doc.get("frequency", "off"),
            "times": doc.get("times", ["09:00"]),
            "user_id": user_id,
            **_walkthrough_state(doc)}


@router.get("/invoice-assistant/settings")
async def read_settings(user: dict = Depends(get_current_user)):
    return await get_settings(user["tenant_id"], user["id"])


@router.post("/invoice-assistant/walkthrough-completed")
async def mark_walkthrough_completed(
        user: dict = Depends(get_current_user)):
    """Record that the user finished the FULL monster walkthrough.

    Idempotent; refreshes the completion date. Single-topic help never
    touches this — it only drives the proactive-offer suppression window.
    """
    await _cfg_coll(user["tenant_id"]).update_one(
        {"user_id": user["id"]},
        {"$set": {"walkthrough_completed_at": now_iso()}},
        upsert=True)
    return {"ok": True,
            **(await get_settings(user["tenant_id"], user["id"]))}


class SettingsBody(BaseModel):
    frequency: str = Field(pattern=r"^(off|daily|twice_daily)$")
    times: List[str] = Field(default_factory=lambda: ["09:00"])


@router.put("/invoice-assistant/settings")
async def write_settings(body: SettingsBody,
                         user: dict = Depends(require("ops:manage"))):
    times = []
    for t in body.times:
        try:
            datetime.strptime(t, "%H:%M")
        except (ValueError, TypeError):
            raise HTTPException(422, f"Invalid time '{t}' — use HH:MM")
        times.append(t)
    limit = MAX_PER_FREQUENCY[body.frequency]
    if len(times) > limit and limit:
        raise HTTPException(
            422, f"{body.frequency} allows at most {limit} time(s)")
    times = sorted(set(times))
    doc = {"user_id": user["id"], "frequency": body.frequency,
           "times": times, "updated_at": now_iso()}
    await _cfg_coll(user["tenant_id"]).update_one(
        {"user_id": user["id"]}, {"$set": doc}, upsert=True)
    await audit(user, "invoice_assistant.settings", "config",
                "invoice_assistant", doc)
    return await get_settings(user["tenant_id"], user["id"])


async def generate_due_rundowns(tenant_id: str, user_id: str,
                                now: Optional[str] = None) -> dict:
    """Scheduler hook core: create a rundown notification for every slot
    due today that has not fired yet. Deduped by (user, date, slot)."""
    dt = _parse_now(now)
    today = dt.date().isoformat()
    settings = await get_settings(tenant_id, user_id)
    created = []
    if settings["frequency"] == "off":
        return {"created": created, "frequency": "off", "date": today}
    now_hm = dt.strftime("%H:%M")
    for slot in settings["times"][:MAX_PER_FREQUENCY[settings["frequency"]]]:
        if slot > now_hm:
            continue  # not due yet today
        dedupe_key = f"{user_id}:{today}:{slot}"
        existing = await _notif_coll(tenant_id).find_one(
            {"dedupe_key": dedupe_key})
        if existing:
            continue  # this slot already fired — never twice
        r = await compute_rundown(tenant_id, now)
        doc = {
            "id": new_id(), "user_id": user_id, "kind": "invoice_rundown",
            "title": "Your invoice rundown",
            "body": rundown_text(r), "rundown": r,
            "link": "/invoices", "read": False,
            "dedupe_key": dedupe_key, "slot": slot,
            "created_at": now_iso(),
        }
        await _notif_coll(tenant_id).insert_one(doc)
        created.append(doc["id"])
    return {"created": created, "frequency": settings["frequency"],
            "date": today}


class RunDueBody(BaseModel):
    now: Optional[str] = None


@router.post("/invoice-assistant/run-due")
async def run_due(body: RunDueBody = RunDueBody(),
                  user: dict = Depends(get_current_user)):
    """Scheduler/run-due style hook — a cron or the app shell calls this;
    it fires only the slots that are due and not yet fired (deduped)."""
    return await generate_due_rundowns(user["tenant_id"], user["id"],
                                       body.now)


@router.get("/invoice-assistant/notifications")
async def list_notifications(user: dict = Depends(get_current_user)):
    rows = await _notif_coll(user["tenant_id"]).find_list(
        {"user_id": user["id"]}, sort=[("created_at", -1)], limit=50)
    return {"notifications": rows,
            "unread": sum(1 for r in rows if not r.get("read"))}


@router.post("/invoice-assistant/notifications/{notif_id}/read")
async def mark_read(notif_id: str, user: dict = Depends(get_current_user)):
    res = await _notif_coll(user["tenant_id"]).update_one(
        {"id": notif_id, "user_id": user["id"]}, {"$set": {"read": True}})
    return {"ok": True}
