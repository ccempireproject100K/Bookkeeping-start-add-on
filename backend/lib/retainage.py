"""Retainage tracking — receivable + payable sub-ledger (owner-approved
2026-11-14, job-costing ask #7: "QuickBooks has no retainage sub-ledger").

Retainage is money a customer holds back from a contractor (and money a
contractor holds back from subs) until the work is proven. We TRACK it;
we never touch it (owner correction 2026-11-14 ~10:17 CST): the hold is
an agreement between the shop and its customer — "we ain't going to be
able to touch their money" — and this product is payroll-lite until a
payment partner lands. So this module is display + track + remind:

  * A job carries a retainage % (receivable side: held from us;
    `retainage_payable_pct`: held by us from subs, defaults to the
    receivable %). Per-phase overrides live on the job doc as
    `retainage_phase_pcts` ({phase_id: {"receivable": pct,
    "payable": pct}} or a bare number = the receivable pct). Phases are
    the job doc's `phases: [{id, name, ...}]` list (feat/job-phase-
    costing shape). Jobs with no % behave exactly as before.
  * Billing a job with a % splits the posting: the net due rides the
    regular AR account (1200) and the held slice rides the retainage
    receivable account (1210) — RECORDING the shop's own billing, which
    is bookkeeping, not money movement. The invoice shows gross / held /
    net due plainly. Bills from subs mirror it: net payable in AP
    (2010), held slice in retainage payable (2030).
  * Every hold is a row in the `retainage_entries` sub-ledger (side,
    job, optional phase, source, gross, held, released, outstanding).
  * RELEASE IS A MARKING, never a payment. The shop records that the
    customer (or the shop, for a sub) released a hold — a fact they
    enter: who / when / how much. Marking updates the entry and posts
    ONE bookkeeping reclass (1210 -> 1200 receivable, 2020 -> 2010
    payable) so the ledger reflects the fact. It creates NO invoice,
    NO bill, touches NO cash account, and initiates NO payment.
  * A due reminder fires into `app_notifications` (same shape as the
    fold-in / jobalerts lanes: kind / title / body / link / read /
    dedupe_key, tenant-wide) when a job's release due date arrives
    with retainage still outstanding. Due date = explicit
    `retainage_release_due_date`, else substantial-completion date +
    `retainage_release_days`.

SEAM (not implementation): a true money-moving release — actually
collecting or paying the released amount — arrives only behind a
payment-partner integration (owner: "we could just change it once we
get a partner"). `execute_release_payment` below is that seam: it is
deliberately unwired and refuses, so no route can reach money movement
by accident.
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, cents_to_dollars
from lib import ledger as L
from lib import posting as P

RET_RECEIVABLE = "1210"   # Retainage Receivable (asset)
RET_PAYABLE = "2030"      # Retainage Payable (liability)
AR = "1200"
AP = "2010"

_ACCOUNTS = [
    (RET_RECEIVABLE, "Retainage Receivable", "asset", "debit"),
    (RET_PAYABLE, "Retainage Payable", "liability", "credit"),
]


def entries_coll(tenant_id):
    return Scoped(db.retainage_entries, tenant_id)


def jobs_coll(tenant_id):
    return Scoped(db.jobs, tenant_id)


def _notif_coll(tenant_id):
    return Scoped(db.app_notifications, tenant_id)


# ---------------- accounts ----------------

async def ensure_accounts(tenant_id: str) -> None:
    """Idempotent: tenants seeded before this lane lack 1210/2030 in
    their chart; add the two system accounts so postings validate.
    New tenants get them from BASE_COA via seed_coa."""
    acc = Scoped(db.accounts, tenant_id)
    existing = {a["code"] for a in await acc.find_list()}
    for code, name, atype, normal in _ACCOUNTS:
        if code in existing:
            continue
        await acc.insert_one({
            "id": new_id(), "code": code, "name": name, "type": atype,
            "normal_balance": normal, "parent_code": None,
            "is_system": True, "archived": False, "description": None,
            "created_at": now_iso(),
        })


# ---------------- percentages ----------------

def held_cents_for(gross_cents: int, pct) -> int:
    """Held amount for a gross figure at pct %, half-up to the cent."""
    if not pct or gross_cents <= 0:
        return 0
    held = (Decimal(int(gross_cents)) * Decimal(str(pct)) / Decimal(100))
    return int(held.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _phase_override(job: dict, phase_id, side: str):
    if not phase_id:
        return None
    overrides = job.get("retainage_phase_pcts") or {}
    raw = overrides.get(phase_id)
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return raw if side == "receivable" else None
    if isinstance(raw, dict):
        val = raw.get(side)
        return val if isinstance(val, (int, float)) else None
    return None


def pct_for_job(job: dict, phase_id=None, side: str = "receivable"):
    """Effective retainage % for a job (+ optional phase).
    Payable falls back to the receivable % when not set separately."""
    override = _phase_override(job, phase_id, side)
    if override is not None:
        return override
    if side == "payable":
        own = job.get("retainage_payable_pct")
        if isinstance(own, (int, float)):
            return own
        # a phase override on the receivable side does not leak here;
        # fall through to the job's receivable %
    base = job.get("retainage_pct")
    return base if isinstance(base, (int, float)) else 0


# ---------------- due dates + reminders ----------------

def release_due_date(job: dict):
    """Explicit due date wins; else substantial completion + days."""
    if job.get("retainage_release_due_date"):
        return job["retainage_release_due_date"]
    sc = job.get("substantial_completion_date")
    days = job.get("retainage_release_days")
    if sc and isinstance(days, int):
        try:
            return (date.fromisoformat(sc) + timedelta(days=days)).isoformat()
        except ValueError:
            return None
    return None


async def check_reminders(tenant_id: str, today: str = None) -> list:
    """Fire a due reminder per job+side whose release date has arrived
    with retainage still outstanding. Deduped by
    (side, job, due date) — fires once, and stays silent once the
    outstanding balance is marked released."""
    today = today or today_str()
    fired = []
    rows = await entries_coll(tenant_id).find_list(limit=5000)
    outstanding = {}
    for e in rows:
        out = e.get("held_cents", 0) - e.get("released_cents", 0)
        if out > 0:
            key = (e["job_id"], e["side"])
            outstanding[key] = outstanding.get(key, 0) + out
    if not outstanding:
        return fired
    job_ids = {k[0] for k in outstanding}
    for job_id in job_ids:
        job = await jobs_coll(tenant_id).find_one({"id": job_id})
        if not job:
            continue
        due = release_due_date(job)
        if not due or due > today:
            continue
        for side in ("receivable", "payable"):
            out = outstanding.get((job_id, side), 0)
            if out <= 0:
                continue
            dedupe_key = f"retainage:{side}:{job_id}:{due}"
            existing = await _notif_coll(tenant_id).find_one(
                {"dedupe_key": dedupe_key})
            if existing:
                continue
            if side == "receivable":
                title = f"{job.get('job_number')} retainage release due"
                body = (f"{job.get('job_number')} ({job.get('customer_name')}): "
                        f"${cents_to_dollars(out)} of retainage was due for "
                        f"release on {due} and is still shown as held. "
                        f"Record the release here once the customer pays it "
                        f"out — Bookkeeping does not collect it for you.")
                kind = "retainage_release_due"
            else:
                title = f"{job.get('job_number')} sub retainage release due"
                body = (f"{job.get('job_number')} ({job.get('customer_name')}): "
                        f"${cents_to_dollars(out)} held from subcontractors "
                        f"was due for release on {due} and is still shown "
                        f"as held. Record the release here once you pay it "
                        f"out — Bookkeeping does not pay it for you.")
                kind = "retainage_payable_due"
            doc = {
                "id": new_id(), "user_id": None, "tenant_wide": True,
                "kind": kind, "job_id": job_id,
                "job_number": job.get("job_number"),
                "title": title, "body": body, "link": "/jobs",
                "read": False, "dedupe_key": dedupe_key,
                "created_by": None, "created_at": now_iso(),
            }
            await _notif_coll(tenant_id).insert_one(doc)
            fired.append(doc)
    return fired


# ---------------- sub-ledger ----------------

async def record_hold(tenant_id: str, *, side: str, job: dict,
                      phase_id=None, source_type: str, source_id: str,
                      gross_cents: int, held_cents: int, pct,
                      counterparty: str = "") -> dict:
    doc = {
        "id": new_id(), "side": side,
        "job_id": job["id"], "job_number": job.get("job_number"),
        "phase_id": phase_id,
        "source_type": source_type, "source_id": source_id,
        "counterparty": counterparty,
        "gross_cents": int(gross_cents), "held_cents": int(held_cents),
        "released_cents": 0, "pct": pct,
        "status": "held", "releases": [],
        "release_due_date": release_due_date(job),
        "created_at": now_iso(),
    }
    await entries_coll(tenant_id).insert_one(doc)
    doc.pop("_id", None)
    return doc


def _public_entry(e: dict) -> dict:
    e = {k: v for k, v in e.items() if k != "_id"}
    e["outstanding_cents"] = e.get("held_cents", 0) - e.get("released_cents", 0)
    e["held"] = cents_to_dollars(e.get("held_cents", 0))
    e["released"] = cents_to_dollars(e.get("released_cents", 0))
    e["outstanding"] = cents_to_dollars(e["outstanding_cents"])
    e["gross"] = cents_to_dollars(e.get("gross_cents", 0))
    return e


def _sum(rows, side=None):
    held = released = 0
    for e in rows:
        if side and e["side"] != side:
            continue
        held += e.get("held_cents", 0)
        released += e.get("released_cents", 0)
    return {"held_cents": held, "released_cents": released,
            "outstanding_cents": held - released,
            "held": cents_to_dollars(held),
            "released": cents_to_dollars(released),
            "outstanding": cents_to_dollars(held - released)}


async def job_summary(tenant_id: str, job: dict) -> dict:
    rows = await entries_coll(tenant_id).find_list(
        {"job_id": job["id"]}, sort=[("created_at", -1)], limit=500)
    phases = {}
    for e in rows:
        if e.get("phase_id"):
            phases.setdefault(e["phase_id"], []).append(e)
    return {
        "job_id": job["id"], "job_number": job.get("job_number"),
        "customer_name": job.get("customer_name"),
        "retainage_pct": job.get("retainage_pct") or 0,
        "retainage_payable_pct": pct_for_job(job, side="payable"),
        "release_due_date": release_due_date(job),
        "receivable": _sum(rows, "receivable"),
        "payable": _sum(rows, "payable"),
        "phases": {pid: {"receivable": _sum(rs, "receivable"),
                         "payable": _sum(rs, "payable")}
                   for pid, rs in phases.items()},
        "entries": [_public_entry(e) for e in rows],
    }


async def tenant_rollup(tenant_id: str) -> dict:
    rows = await entries_coll(tenant_id).find_list(limit=5000)
    by_job = {}
    for e in rows:
        by_job.setdefault(e["job_id"], []).append(e)
    jobs_out = []
    for job_id, rs in by_job.items():
        job = await jobs_coll(tenant_id).find_one({"id": job_id})
        jobs_out.append({
            "job_id": job_id,
            "job_number": (job or {}).get("job_number") or rs[0].get("job_number"),
            "customer_name": (job or {}).get("customer_name") or rs[0].get("counterparty"),
            "release_due_date": release_due_date(job) if job else rs[0].get("release_due_date"),
            "receivable": _sum(rs, "receivable"),
            "payable": _sum(rs, "payable"),
        })
    jobs_out.sort(key=lambda j: j["job_number"] or "")
    return {
        "receivable": _sum(rows, "receivable"),
        "payable": _sum(rows, "payable"),
        "jobs": jobs_out,
        "entries": [_public_entry(e) for e in
                    sorted(rows, key=lambda e: e.get("created_at", ""), reverse=True)],
    }


# ---------------- release MARKING (record-only) ----------------

async def mark_release(tenant_id: str, user: dict, entry: dict,
                       amount_cents: int = None, note: str = "") -> dict:
    """Record that a hold was released OUTSIDE Bookkeeping.

    This is a record of an outside event — who/when/how much the shop
    enters. It posts one bookkeeping reclass so the ledger reflects
    the fact (receivable 1210 -> 1200; payable 2030 -> 2010), creates
    NO invoice or bill, touches NO cash account, and initiates NO
    payment. Guarded: marking beyond the outstanding balance is
    refused, so a hold can never be marked released twice."""
    outstanding = entry.get("held_cents", 0) - entry.get("released_cents", 0)
    if outstanding <= 0:
        raise HTTPException(status_code=409,
                            detail="This hold is already fully marked released")
    amount = outstanding if amount_cents is None else int(amount_cents)
    if amount <= 0:
        raise HTTPException(status_code=422,
                            detail="Release amount must be positive")
    if amount > outstanding:
        raise HTTPException(
            status_code=422,
            detail=f"Release exceeds the held balance "
                   f"(${cents_to_dollars(outstanding)} still held)")

    await ensure_accounts(tenant_id)
    if entry["side"] == "receivable":
        lines = [P.dr(AR, amount, "Retainage released — recorded"),
                 P.cr(RET_RECEIVABLE, amount, "Retainage released — recorded")]
    else:
        lines = [P.dr(RET_PAYABLE, amount, "Retainage released — recorded"),
                 P.cr(AP, amount, "Retainage released — recorded")]
    je = await L.post_journal_entry(db, tenant_id, user, {
        "entry_date": today_str(), "source": "retainage_release_recorded",
        "source_ref": entry["id"],
        "memo": (f"Retainage release recorded — {entry.get('job_number')} "
                 f"({entry['side']}): released outside Bookkeeping; "
                 f"no payment processed").strip(),
        "lines": lines,
    }, status="posted")

    record = {
        "amount_cents": amount, "date": today_str(),
        "recorded_by": (user or {}).get("email") or (user or {}).get("id"),
        "entry_number": je["number"], "note": note or "",
        "recorded_at": now_iso(),
    }
    new_released = entry.get("released_cents", 0) + amount
    status = "released" if new_released >= entry.get("held_cents", 0) \
        else "partially_released"
    await entries_coll(tenant_id).update_one(
        {"id": entry["id"]},
        {"$set": {"released_cents": new_released, "status": status},
         "$push": {"releases": record}})
    await L.audit(db, tenant_id, user, "retainage.release_recorded",
                  {"job": entry.get("job_number"), "side": entry["side"],
                   "amount_cents": amount, "entry_number": je["number"]})
    updated = await entries_coll(tenant_id).find_one({"id": entry["id"]})
    return _public_entry(updated)


async def mark_release_for_job(tenant_id: str, user: dict, job: dict,
                               side: str, amount_cents: int = None,
                               note: str = "") -> list:
    """Mark a release across a job's holds for one side, oldest first."""
    rows = await entries_coll(tenant_id).find_list(
        {"job_id": job["id"], "side": side},
        sort=[("created_at", 1)], limit=500)
    rows = [e for e in rows
            if e.get("held_cents", 0) - e.get("released_cents", 0) > 0]
    if not rows:
        raise HTTPException(status_code=409,
                            detail="No outstanding retainage to mark released")
    total_out = sum(e["held_cents"] - e["released_cents"] for e in rows)
    amount = total_out if amount_cents is None else int(amount_cents)
    if amount <= 0 or amount > total_out:
        raise HTTPException(
            status_code=422,
            detail=f"Release amount must be between $0.01 and "
                   f"${cents_to_dollars(total_out)}")
    done = []
    remaining = amount
    for e in rows:
        if remaining <= 0:
            break
        take = min(remaining, e["held_cents"] - e["released_cents"])
        done.append(await mark_release(tenant_id, user, e, take, note))
        remaining -= take
    return done


# ---------------- SEAM: money-moving release (NOT implemented) ----------------

async def execute_release_payment(*args, **kwargs):
    """SEAM — deliberately unwired. A release that actually moves
    money (collecting a customer's released retainage, or paying a
    sub theirs) arrives only behind a payment-partner integration.
    Until then, releases are recorded facts (mark_release), never
    payment actions. Do not route this."""
    raise NotImplementedError(
        "Money-moving retainage release requires a payment partner; "
        "releases are record-only until one lands.")
