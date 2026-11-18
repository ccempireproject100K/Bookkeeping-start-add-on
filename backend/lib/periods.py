"""Period-close workflow (Top-20 #19): checklist, snapshots, two-stage
close, reasoned/timed reopen, and the changes-after-close spotlight.

Enforcement of the lock itself lives at the posting chokepoint in
lib/ledger.py (`_posting_gate` / `resolve_period`); this module is the
workflow around it. Every enhancement above the core lock has its own
tenant-level ON/OFF switch (owner condition, 2026-11-14), all ON by
default, stored at tenant.settings.period_close:

- snapshot_on_close  — freeze TB/P&L/BS at close (OFF: no snapshot taken)
- soft_close_stage   — soft close, then hard lock after grace
                       (OFF: close goes straight to hard lock)
- reopen_apparatus   — reason + timer + spotlight on reopen
                       (OFF: plain owner/accountant reopen, no records)

The core lock is not a switch: closing is always a deliberate act.
"""
import calendar
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from lib import ledger as L
from lib.base import Scoped, new_id, now_iso

DEFAULT_SETTINGS = {
    "snapshot_on_close": True,
    "soft_close_stage": True,
    "reopen_apparatus": True,
    "grace_days": 7,          # soft -> hard, 0 = straight to hard
    "reopen_window_hours": 24,
}

REOPEN_ROLES = ("owner", "accountant")


# ---------------- settings ----------------
def _clamp(value, lo, hi, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


async def get_settings(db, tenant_id):
    t = await db.tenants.find_one({"id": tenant_id}, {"_id": 0}) or {}
    stored = (t.get("settings") or {}).get("period_close") or {}
    out = dict(DEFAULT_SETTINGS)
    for k in ("snapshot_on_close", "soft_close_stage", "reopen_apparatus"):
        if k in stored:
            out[k] = bool(stored[k])
    out["grace_days"] = _clamp(stored.get("grace_days", out["grace_days"]), 0, 30, 7)
    out["reopen_window_hours"] = _clamp(
        stored.get("reopen_window_hours", out["reopen_window_hours"]), 1, 168, 24)
    return out


async def update_settings(db, tenant_id, user, patch):
    for k in ("snapshot_on_close", "soft_close_stage", "reopen_apparatus"):
        if k in patch and not isinstance(patch[k], bool):
            raise HTTPException(status_code=422, detail=f"{k} must be true or false")
    if "grace_days" in patch:
        _clamp(patch["grace_days"], 0, 30, -1)
        if not isinstance(patch["grace_days"], int) or not 0 <= patch["grace_days"] <= 30:
            raise HTTPException(status_code=422, detail="grace_days must be an integer 0–30")
    if "reopen_window_hours" in patch:
        if not isinstance(patch["reopen_window_hours"], int) or not 1 <= patch["reopen_window_hours"] <= 168:
            raise HTTPException(status_code=422, detail="reopen_window_hours must be an integer 1–168")
    tenant = await db.tenants.find_one({"id": tenant_id}, {"_id": 0}) or {}
    settings = dict(tenant.get("settings") or {})
    merged = dict((settings.get("period_close") or {}))
    merged.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
    settings["period_close"] = merged
    await db.tenants.update_one({"id": tenant_id}, {"$set": {"settings": settings}})
    effective = await get_settings(db, tenant_id)
    await L._period_audit(db, tenant_id, user, "period.settings_updated", None,
                          {"settings": effective})
    return effective


# ---------------- helpers ----------------
def period_range(key):
    y, m = map(int, key.split("-"))
    last = calendar.monthrange(y, m)[1]
    start = f"{key}-01"
    end = f"{key}-{last:02d}"
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    next_start = f"{ny:04d}-{nm:02d}-01"
    return start, end, next_start


def _plus(iso, **delta):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(**delta)).isoformat()


async def _get_period(db, tenant_id, key):
    await L.ensure_period(db, tenant_id, f"{key}-01")
    period = await Scoped(db.fiscal_periods, tenant_id).find_one({"key": key})
    return await L.resolve_period(db, tenant_id, period)


# ---------------- checklist ----------------
async def compute_checklist(db, tenant_id, key):
    """What stands between this period and a clean close — computed only
    from records that actually exist in the books. Bills and invoices post
    to the ledger the moment they are created in this codebase, so there
    is no such thing as an unposted bill/invoice to hunt for; they are
    reported as informational 'still open' counts instead of blockers."""
    start, end, next_start = period_range(key)
    entries = Scoped(db.journal_entries, tenant_id)
    txns = Scoped(db.bank_transactions, tenant_id)
    in_period = {"$gte": start, "$lte": end}

    draft_count = await entries.count({"status": "draft", "entry_date": in_period})
    unposted_bank = await txns.count({"status": "unmatched", "txn_date": in_period})
    unreconciled_bank = await txns.count({
        "status": {"$in": ["cleared", "matched"]},
        "reconciled": {"$ne": True}, "txn_date": in_period})

    open_bills = await Scoped(db.bills, tenant_id).find_list(
        {"status": {"$in": ["open", "partial"]}, "bill_date": in_period})
    open_invoices = await Scoped(db.invoices, tenant_id).find_list(
        {"status": {"$in": ["issued", "partial"]},
         "created_at": {"$gte": start, "$lt": next_start}})

    items = [
        {"key": "draft_entries", "blocking": True,
         "label": "Draft journal entries dated in this period (not yet posted)",
         "count": draft_count},
        {"key": "unposted_bank_txns", "blocking": True,
         "label": "Bank transactions in this period not yet posted to the books",
         "count": unposted_bank},
        {"key": "unreconciled_bank_txns", "blocking": True,
         "label": "Posted bank transactions in this period not yet reconciled",
         "count": unreconciled_bank},
        {"key": "open_bills", "blocking": False,
         "label": "Bills dated in this period still open (unpaid) at close",
         "count": len(open_bills),
         "amount_cents": sum(int(b.get("amount_cents", 0)) - int(b.get("paid_cents", 0))
                             for b in open_bills)},
        {"key": "open_invoices", "blocking": False,
         "label": "Invoices issued in this period still open (unpaid) at close",
         "count": len(open_invoices),
         "amount_cents": sum(int(i.get("total_cents", 0)) - int(i.get("paid_cents", 0))
                             for i in open_invoices)},
    ]
    notes = [
        "Bills and invoices post to the ledger the moment they are created, "
        "so there are no unposted bills or invoices to find — the counts "
        "above are what is still open (unpaid), for information only.",
        "Anything never entered into Bookkeeping (paper receipts, "
        "off-system payments) cannot be checked by this list.",
    ]
    return {"period_key": key, "computed_at": now_iso(), "items": items, "notes": notes}


# ---------------- snapshots ----------------
async def take_snapshot(db, tenant_id, user, period, kind):
    start, end, _ = period_range(period["key"])
    existing = await Scoped(db.period_snapshots, tenant_id).count(
        {"period_key": period["key"]})
    snap = {
        "id": new_id(),
        "period_key": period["key"],
        "kind": kind,  # "close" | "reclose"
        "seq": existing + 1,
        "taken_at": now_iso(),
        "taken_by": (user or {}).get("email", "system"),
        "trial_balance": await L.trial_balance(db, tenant_id, end),
        "pnl": await L.pnl(db, tenant_id, start, end),
        "balance_sheet": await L.balance_sheet(db, tenant_id, end),
    }
    await Scoped(db.period_snapshots, tenant_id).insert_one(snap)
    return snap


async def list_snapshots(db, tenant_id, key):
    return await Scoped(db.period_snapshots, tenant_id).find_list(
        {"period_key": key}, sort=[("seq", 1)])


# ---------------- close / lock ----------------
async def close_period(db, tenant_id, user, key, acknowledge=None, grace_days=None,
                       via="ui", force_hard=False, now=None):
    now = now or now_iso()
    settings = await get_settings(db, tenant_id)
    period = await _get_period(db, tenant_id, key)
    reopen = period.get("reopen") or {}
    was_reopen = reopen.get("status") == "active"
    if period.get("status") in ("soft_closed", "locked") and not was_reopen:
        raise HTTPException(status_code=409,
                            detail=f"Period {key} is already {period['status']}")

    checklist = await compute_checklist(db, tenant_id, key)
    blocking_open = [i for i in checklist["items"] if i["blocking"] and i["count"] > 0]
    ack = set(acknowledge or [])
    if via == "legacy_status":
        ack = {i["key"] for i in blocking_open}
    unack = [i for i in blocking_open if i["key"] not in ack]
    if unack:
        labels = "; ".join(i["label"] for i in unack)
        raise HTTPException(
            status_code=422,
            detail=(f"The close checklist still has unacknowledged items: {labels}. "
                    "Acknowledge each one to close with exceptions."))

    s = Scoped(db.fiscal_periods, tenant_id)
    updates = {}
    if was_reopen:
        ended = {**reopen, "status": "ended", "ended_at": now, "end_reason": "reclosed"}
        updates["reopen"] = ended
        await Scoped(db.period_reopens, tenant_id).update_one(
            {"id": reopen["id"]},
            {"$set": {"status": "ended", "ended_at": now, "end_reason": "reclosed"}})

    snapshot = None
    if settings["snapshot_on_close"]:
        kind = "reclose" if period.get("times_closed") else "close"
        snapshot = await take_snapshot(db, tenant_id, user, period, kind)

    grace = settings["grace_days"] if grace_days is None else _clamp(grace_days, 0, 30, settings["grace_days"])
    go_hard = force_hard or not settings["soft_close_stage"] or grace == 0
    updates.update({
        "closed_at": now,
        "closed_by": (user or {}).get("email", "system"),
        "close_checklist": {**checklist,
                            "acknowledged": sorted(ack),
                            "acknowledged_by": (user or {}).get("email", "system"),
                            "via": via},
        "close_snapshot_id": snapshot["id"] if snapshot else None,
        "times_closed": int(period.get("times_closed") or 0) + 1,
    })
    if go_hard:
        updates.update({"status": "locked",
                        "locked_by": (user or {}).get("email", "system"),
                        "locked_at": now, "hard_lock_at": None})
    else:
        updates.update({"status": "soft_closed", "soft_closed_at": now,
                        "hard_lock_at": _plus(now, days=grace)})
    await s.update_one({"key": key}, {"$set": updates})
    period = {**period, **updates}
    await L._period_audit(db, tenant_id, user, "period.closed", key, {
        "stage": updates["status"],
        "snapshot_id": updates["close_snapshot_id"],
        "exceptions": sorted(ack),
        "hard_lock_at": updates.get("hard_lock_at"),
        "via": via,
    })
    return {"period": period, "checklist": checklist, "snapshot": snapshot}


async def lock_now(db, tenant_id, user, key, now=None):
    now = now or now_iso()
    period = await _get_period(db, tenant_id, key)
    reopen = period.get("reopen") or {}
    if reopen.get("status") == "active":
        raise HTTPException(status_code=409,
                            detail=f"Period {key} is currently reopened — end the reopen or re-close it first")
    if period.get("status") == "locked":
        raise HTTPException(status_code=409, detail=f"Period {key} is already locked")
    if period.get("status") != "soft_closed":
        raise HTTPException(status_code=409,
                            detail=f"Period {key} is open — close it first, then lock")
    updates = {"status": "locked",
               "locked_by": (user or {}).get("email", "system"),
               "locked_at": now, "hard_lock_at": None, "auto_locked": False}
    await Scoped(db.fiscal_periods, tenant_id).update_one({"key": key}, {"$set": updates})
    period = {**period, **updates}
    await L._period_audit(db, tenant_id, user, "period.locked", key, {"manual": True})
    return period


# ---------------- reopen ----------------
async def _notify_reopen(db, tenant_id, actor, key, reopen_rec):
    users = await db.users.find(
        {"tenant_id": tenant_id, "role": {"$in": list(REOPEN_ROLES)}}).to_list(length=100)
    notes = Scoped(db.app_notifications, tenant_id)
    for u in users:
        if u.get("id") == (actor or {}).get("id"):
            continue
        await notes.insert_one({
            "id": new_id(),
            "user_id": u["id"],
            "type": "period_reopened",
            "title": f"Period {key} was reopened",
            "detail": (f"{(actor or {}).get('name') or (actor or {}).get('email', 'Someone')} "
                       f"reopened {key} until {reopen_rec['closes_at'][:16].replace('T', ' ')} UTC. "
                       f"Reason: {reopen_rec['reason']}"),
            "period_key": key,
            "reopen_id": reopen_rec["id"],
            "created_at": now_iso(),
            "read_at": None,
        })


async def reopen_period(db, tenant_id, user, key, reason=None, window_hours=None, now=None):
    now = now or now_iso()
    if (user or {}).get("role") not in REOPEN_ROLES:
        raise HTTPException(status_code=403,
                            detail="Only the owner or the accountant can reopen a closed period")
    period = await _get_period(db, tenant_id, key)
    reopen = period.get("reopen") or {}
    if reopen.get("status") == "active":
        raise HTTPException(
            status_code=409,
            detail=f"Period {key} is already reopened until {reopen.get('closes_at')}")
    if period.get("status") not in ("soft_closed", "locked"):
        raise HTTPException(status_code=409, detail=f"Period {key} is not closed")
    settings = await get_settings(db, tenant_id)
    s = Scoped(db.fiscal_periods, tenant_id)

    if not settings["reopen_apparatus"]:
        # Apparatus OFF: plain reopen — no reason, no timer, no records,
        # no notifications. The deliberate act + role gate remain.
        updates = {"status": "open", "reopen": None}
        await s.update_one({"key": key}, {"$set": updates})
        period = {**period, **updates}
        await L._period_audit(db, tenant_id, user, "period.reopened", key,
                              {"apparatus": False})
        return period

    if not reason or not str(reason).strip():
        raise HTTPException(status_code=422,
                            detail="Reopening a closed period needs a written reason")
    hours = _clamp(window_hours if window_hours is not None
                   else settings["reopen_window_hours"], 1, 168,
                   settings["reopen_window_hours"])
    rec = {
        "id": new_id(),
        "period_key": key,
        "reason": str(reason).strip(),
        "by_id": (user or {}).get("id"),
        "by_name": (user or {}).get("name") or (user or {}).get("email"),
        "by_role": (user or {}).get("role"),
        "opened_at": now,
        "closes_at": _plus(now, hours=hours),
        "previous_status": period["status"],
        "status": "active",
        "ended_at": None,
        "end_reason": None,
    }
    await Scoped(db.period_reopens, tenant_id).insert_one(rec)
    embedded = {k: rec[k] for k in ("id", "reason", "by_id", "by_name", "by_role",
                                    "opened_at", "closes_at", "previous_status", "status")}
    await s.update_one({"key": key}, {"$set": {"reopen": embedded}})
    period = {**period, "reopen": embedded}
    await _notify_reopen(db, tenant_id, user, key, rec)
    await L._period_audit(db, tenant_id, user, "period.reopened", key,
                          {"reason": rec["reason"], "closes_at": rec["closes_at"],
                           "reopen_id": rec["id"]})
    return period


async def end_reopen(db, tenant_id, user, key, now=None):
    now = now or now_iso()
    if (user or {}).get("role") not in REOPEN_ROLES:
        raise HTTPException(status_code=403,
                            detail="Only the owner or the accountant can end a reopen")
    period = await _get_period(db, tenant_id, key)
    reopen = period.get("reopen") or {}
    if reopen.get("status") != "active":
        raise HTTPException(status_code=409, detail=f"Period {key} is not reopened")
    back_to = reopen.get("previous_status") or "locked"
    if back_to == "soft_closed" and period.get("hard_lock_at") and now >= period["hard_lock_at"]:
        back_to = "locked"
    ended = {**reopen, "status": "ended", "ended_at": now, "end_reason": "manual"}
    updates = {"reopen": ended, "status": back_to}
    if back_to == "locked" and period.get("status") != "locked":
        updates["locked_by"] = (user or {}).get("email", "system")
        updates["locked_at"] = now
    await Scoped(db.fiscal_periods, tenant_id).update_one({"key": key}, {"$set": updates})
    await Scoped(db.period_reopens, tenant_id).update_one(
        {"id": reopen["id"]},
        {"$set": {"status": "ended", "ended_at": now, "end_reason": "manual"}})
    period = {**period, **updates}
    await L._period_audit(db, tenant_id, user, "period.reopen_ended", key,
                          {"reopen_id": reopen["id"], "back_to": back_to})
    return period


# ---------------- changes after close (spotlight) ----------------
async def changes_after_close(db, tenant_id, period_key=None):
    q = {}
    if period_key:
        q["period_key"] = period_key
    reopens = await Scoped(db.period_reopens, tenant_id).find_list(
        q, sort=[("opened_at", 1)])
    entries_scope = Scoped(db.journal_entries, tenant_id)
    grouped = {}
    for r in reopens:
        rows = await entries_scope.find_list({"reopen_id": r["id"]},
                                             sort=[("created_at", 1)])
        entry_rows = [{
            "id": e["id"], "number": e.get("number"),
            "entry_date": e.get("entry_date"), "status": e.get("status"),
            "memo": e.get("memo"), "total_cents": e.get("total_cents"),
            "created_by": e.get("created_by"), "posted_by": e.get("posted_by"),
            "created_at": e.get("created_at"),
        } for e in rows]
        grouped.setdefault(r["period_key"], []).append({
            "reopen": {k: r.get(k) for k in (
                "id", "reason", "by_name", "by_role", "opened_at", "closes_at",
                "ended_at", "end_reason", "status", "previous_status")},
            "entries": entry_rows,
        })
    return {
        "periods": [{"period_key": k, "reopens": v}
                    for k, v in sorted(grouped.items())],
        "reopen_count": len(reopens),
    }
