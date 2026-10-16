"""Smart collections autopilot: an opt-in escalation ladder for overdue invoices.

The owner configures steps (day 3: friendly nudge, day 10: firm, day 21: final +
late fee, day 35: pause service). Running the autopilot evaluates every overdue
invoice and executes the due steps it hasn't seen yet:

- nudge / firm / final: records a payment reminder (same store as manual ones)
  AND delivers it by email via the configured SMTP sender
  (see backend/lib/mailer.py). Delivery is best-effort and key-gated:
  without SMTP_HOST the email is skipped gracefully and the reminder
  stays queued — it is retried on the next run once SMTP is configured.
- late_fee: posts DR 1200 / CR 4090 and grows the invoice balance
- pause_service: flags the customer with collections_hold

Nothing runs on a timer by itself unless the in-app scheduler is enabled
(see backend/lib/scheduler.py and ENABLE_SCHEDULER) — otherwise the owner
hits Run. Every action is audited. Preview shows what a run would do.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso, today_str
from lib.mailer import send_email
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/collections", tags=["collections"])

LATE_FEE_CODE = "4090"

DEFAULT_STEPS = [
    {"days": 3, "action": "nudge",
     "template": "Hi {client_name},\n\nJust a friendly reminder that invoice {invoice_ref} for {balance} was due on {due_date} ({days} days ago).\n\nCould you let us know when we can expect payment? Reply if anything looks off.\n\nThanks!"},
    {"days": 10, "action": "firm",
     "template": "Hi {client_name},\n\nThis is a follow-up on invoice {invoice_ref} for {balance}, now {days} days overdue (due {due_date}).\n\nPlease arrange payment this week to avoid late fees. If you've already paid, disregard this note.\n\nThanks,"},
    {"days": 21, "action": "final",
     "template": "Hi {client_name},\n\nInvoice {invoice_ref} for {balance} is now {days} days overdue. A late fee has been applied per our terms.\n\nThis is our final notice before we pause scheduling new work. Please pay today or call us to work something out."},
]

DEFAULT_CONFIG = {
    "enabled": False,
    "steps": DEFAULT_STEPS,
    "late_fee_days": 21,          # apply fee when a step with action late_fee/final hits
    "late_fee_percent": 1.5,      # of outstanding balance
    "late_fee_min_cents": 2500,   # $25 minimum
    "late_fee_max_cents": 25000,  # $250 cap
    "pause_service_days": 35,
}


def _cfg(user) -> dict:
    return Scoped(db.collections_config, user["tenant_id"])


async def get_config(user) -> dict:
    doc = await _cfg(user).find_one({"id": "autopilot"})
    if doc:
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in doc.items() if k not in ("_id", "id", "tenant_id")})
        return merged
    return dict(DEFAULT_CONFIG)


def _days_overdue(due_date: Optional[str], today: str) -> int:
    if not due_date:
        return 0
    try:
        return max(0, (date.fromisoformat(today) - date.fromisoformat(due_date[:10])).days)
    except ValueError:
        return 0


def _fmt_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _render(template: str, inv: dict, days: int) -> str:
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    return (template.replace("{client_name}", inv.get("client_name", ""))
            .replace("{invoice_ref}", f"#{inv['id'][:8]}")
            .replace("{balance}", _fmt_money(balance))
            .replace("{days}", str(days))
            .replace("{due_date}", (inv.get("due_date") or "receipt")[:10]))


async def _shop_name(user) -> str:
    """Tenant's display name for email signatures. Falls back to the
    Mind at Ease Solutions brand when the tenant has no name set."""
    try:
        tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"name": 1})
    except Exception:
        tenant = None
    return ((tenant or {}).get("name") or "").strip() or "Mind at Ease Solutions"


async def _reminder_recipient(user, inv: dict) -> Optional[str]:
    """Customer email for an invoice, or None when there is none on file."""
    cid = inv.get("customer_id")
    if cid:
        try:
            c = await Scoped(db.customers, user["tenant_id"]).find_one({"id": cid})
        except Exception:
            c = None
        if c and (c.get("email") or "").strip():
            return c["email"].strip()
    return None


_STAGE_SUBJECTS = {
    "nudge": "Friendly reminder: invoice #{ref} from {shop}",
    "firm": "Payment follow-up: invoice #{ref} is {days} days overdue",
    "final": "Final notice: invoice #{ref} ({days} days overdue) — action needed",
}


def _build_reminder_email(message: str, invoice_id: str, days: int, stage: str,
                          shop_name: str) -> tuple[str, str]:
    """(subject, body) for a collections reminder email. Plain text, branded
    with the shop name; no third-party residue."""
    ref = (invoice_id or "")[:8]
    subject = _STAGE_SUBJECTS.get(stage, _STAGE_SUBJECTS["nudge"]).format(
        ref=ref, shop=shop_name, days=days)
    body = (f"{message}\n\n"
            f"— {shop_name}\n"
            f"Sent via Mind at Ease Solutions")
    return subject, body


async def _overdue_invoices(user, today: str) -> list:
    docs = await Scoped(db.invoices, user["tenant_id"]).find_list(
        {"status": {"$in": ["issued", "partial"]}}, limit=2000)
    out = []
    for inv in docs:
        days = _days_overdue(inv.get("due_date"), today)
        if days <= 0:
            continue
        balance = inv["total_cents"] - inv.get("paid_cents", 0)
        if balance <= 0:
            continue
        out.append({"invoice": inv, "days": days, "balance": balance})
    return sorted(out, key=lambda r: -r["days"])


async def _ensure_fee_account(user):
    coll = Scoped(db.accounts, user["tenant_id"])
    if not await coll.find_one({"code": LATE_FEE_CODE}):
        await coll.insert_one({"id": new_id(), "code": LATE_FEE_CODE,
                               "name": "Late Fee Income", "type": "revenue",
                               "normal_balance": "credit", "is_system": True,
                               "created_at": now_iso()})


async def _apply_late_fee(user, inv: dict, cfg: dict) -> dict:
    """Post DR 1200 / CR 4090 and grow the invoice balance. Idempotent per run."""
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    fee = int(round(balance * cfg["late_fee_percent"] / 100))
    fee = max(cfg["late_fee_min_cents"], min(cfg["late_fee_max_cents"], fee))
    # don't stack fees more than once per calendar month
    last = max([f.get("applied_at", "") for f in inv.get("late_fees", [])] or [""])
    if last and last[:7] == today_str()[:7]:
        return {"applied": False, "reason": "fee already applied this month"}
    await _ensure_fee_account(user)
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "late_fee", "source_ref": inv["id"],
        "memo": f"Late fee — invoice #{inv['id'][:8]}",
        "lines": [P.dr("1200", fee, "Late fee receivable"),
                  P.cr(LATE_FEE_CODE, fee, "Late fee income")],
    }, status="posted")
    fee_rec = {"id": new_id(), "amount_cents": fee, "applied_at": now_iso(),
               "entry_number": entry["number"]}
    await Scoped(db.invoices, user["tenant_id"]).update_one(
        {"id": inv["id"]},
        {"$inc": {"total_cents": fee, "late_fee_cents": fee},
         "$push": {"late_fees": fee_rec}})
    await audit(user, "collections.late_fee", "invoice", inv["id"],
                {"fee_cents": fee, "entry": entry["number"]})
    return {"applied": True, "fee_cents": fee}


async def _record_reminder(user, inv: dict, message: str, stage: str, days: int):
    rid = new_id()
    rec = {
        "id": rid, "invoice_id": inv["id"], "client_name": inv.get("client_name"),
        "message": message, "stage": stage, "via": "autopilot",
        "days_overdue": days, "created_at": now_iso()}
    # Best-effort email delivery. Key-gated in lib/mailer: without SMTP_HOST
    # this returns ("skipped", ...) and the reminder stays queued for a later
    # run — never a crash, never a fake "sent".
    email_to = await _reminder_recipient(user, inv)
    if not email_to:
        rec.update({"email_to": None, "email_status": "no_recipient",
                    "email_error": "no customer email on file"})
    else:
        shop = await _shop_name(user)
        subject, body = _build_reminder_email(message, inv["id"], days, stage,
                                              shop)
        rec["email_subject"] = subject
        try:
            status, err = await send_email(email_to, subject, body)
        except Exception as exc:                      # belt-and-braces; mailer already catches
            status, err = "skipped", str(exc)[:200]
        if status == "sent":
            rec.update({"email_to": email_to, "email_status": "sent",
                        "email_error": None, "email_sent_at": now_iso()})
        elif err == "SMTP not configured":
            rec.update({"email_to": email_to, "email_status": "queued",
                        "email_error": err})
        else:
            rec.update({"email_to": email_to, "email_status": "failed",
                        "email_error": err})
    await Scoped(db.payment_reminders, user["tenant_id"]).insert_one(rec)
    await Scoped(db.invoices, user["tenant_id"]).update_one(
        {"id": inv["id"]}, {"$push": {"autopilot_stages": stage}})
    await audit(user, "collections.reminder", "invoice", inv["id"],
                {"stage": stage, "reminder_id": rid,
                 "email_status": rec.get("email_status")})
    return rid


async def _flush_queued_reminders(user) -> dict:
    """Attempt delivery for reminders whose email never went out
    (email_status 'queued' or 'failed'). Runs at the start of every autopilot
    run. Reminders already marked 'sent' are never touched — no duplicates.
    Never raises."""
    coll = Scoped(db.payment_reminders, user["tenant_id"])
    pending = await coll.find_list({"email_status": {"$in": ["queued", "failed"]}},
                                   limit=500)
    sent, still_pending = 0, 0
    for rec in pending:
        try:
            inv = await Scoped(db.invoices, user["tenant_id"]).find_one(
                {"id": rec.get("invoice_id")})
            inv = inv or {}
            to = rec.get("email_to") or await _reminder_recipient(user, inv)
            if not to:
                continue
            subject = rec.get("email_subject") or _build_reminder_email(
                rec.get("message", ""), rec.get("invoice_id", ""),
                rec.get("days_overdue", 0), rec.get("stage", "nudge"),
                await _shop_name(user))[0]
            status, err = await send_email(to, subject, rec.get("message", ""))
            if status == "sent":
                await coll.update_one(
                    {"id": rec["id"]},
                    {"$set": {"email_to": to, "email_status": "sent",
                              "email_error": None, "email_sent_at": now_iso()}})
                sent += 1
            else:
                await coll.update_one(
                    {"id": rec["id"]},
                    {"$set": {"email_to": to, "email_status": "queued",
                              "email_error": err}})
                still_pending += 1
        except Exception:
            still_pending += 1
    return {"sent": sent, "still_pending": still_pending}


def _due_steps(cfg: dict, days: int, done: list) -> list:
    return [s for s in sorted(cfg["steps"], key=lambda s: s["days"])
            if s["days"] <= days and s["action"] not in done]


# ---------------- Config ----------------
@router.get("/config")
async def read_config(user: dict = Depends(require("ledger:read"))):
    return await get_config(user)


class StepBody(BaseModel):
    days: int
    action: str            # nudge | firm | final | pause_service
    template: Optional[str] = ""


class ConfigBody(BaseModel):
    enabled: bool = False
    steps: list = []
    late_fee_days: int = 21
    late_fee_percent: float = 1.5
    late_fee_min_cents: int = 2500
    late_fee_max_cents: int = 25000
    pause_service_days: int = 35


@router.put("/config")
async def write_config(body: ConfigBody, user: dict = Depends(require("ops:manage"))):
    for s in body.steps:
        if s.get("action") not in ("nudge", "firm", "final", "pause_service"):
            raise HTTPException(422, f"Bad step action: {s.get('action')}")
        if int(s.get("days", 0)) < 0:
            raise HTTPException(422, "Step days must be >= 0")
    doc = {"id": "autopilot", **body.model_dump(), "updated_at": now_iso()}
    await _cfg(user).update_one({"id": "autopilot"}, {"$set": doc}, upsert=True)
    await audit(user, "collections.config", "config", "autopilot",
                {"enabled": body.enabled, "steps": len(body.steps)})
    return await get_config(user)


# ---------------- Pipeline & runs ----------------
@router.get("/scheduler/status")
async def scheduler_status(user: dict = Depends(require("ops:manage"))):
    """Is the in-app scheduler running, when is the next autopilot run,
    and what did recent runs do? Owner/manager only."""
    from lib import scheduler as sched
    runs = await db.scheduler_runs.find(
        {"job": "collections-autopilot"},
        {"_id": 0}).sort("started_at", -1).limit(10).to_list(10)
    st = sched.status()
    return {"scheduler_env_enabled": sched.scheduler_enabled(),
            "running": st["running"],
            "instance_id": st["instance_id"],
            "next_run": st["next_run"],
            "last_outcome": st["last_outcome"],
            "fx_enabled": sched.fx_rates_enabled(),
            "fx_next_run": st.get("fx_next_run"),
            "fx_last_outcome": st.get("fx_last_outcome"),
            "recent_runs": runs}


@router.get("/pipeline")
async def pipeline(user: dict = Depends(require("ledger:read"))):
    cfg = await get_config(user)
    today = today_str()
    rows = []
    for r in await _overdue_invoices(user, today):
        inv, days = r["invoice"], r["days"]
        done = inv.get("autopilot_stages", [])
        due = _due_steps(cfg, days, done)
        bucket = "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"
        rows.append({"invoice_id": inv["id"], "client_name": inv.get("client_name"),
                     "due_date": inv.get("due_date"), "days_overdue": days,
                     "balance_cents": r["balance"],
                     "late_fee_cents": inv.get("late_fee_cents", 0),
                     "bucket": bucket, "stages_done": done,
                     "next_step": due[0] if due else None,
                     "on_hold": bool(inv.get("customer_hold"))})
    total = sum(r["balance_cents"] for r in rows)
    return {"enabled": cfg["enabled"], "total_overdue_cents": total,
            "count": len(rows), "rows": rows}


@router.post("/autopilot/preview")
async def autopilot_preview(user: dict = Depends(require("ops:manage"))):
    cfg = await get_config(user)
    if not cfg["enabled"]:
        raise HTTPException(409, "Autopilot is off — enable it in the config first")
    today = today_str()
    plan = []
    for r in await _overdue_invoices(user, today):
        inv, days = r["invoice"], r["days"]
        for s in _due_steps(cfg, days, inv.get("autopilot_stages", [])):
            plan.append({"invoice_id": inv["id"], "client_name": inv.get("client_name"),
                         "days_overdue": days, "action": s["action"],
                         "balance_cents": r["balance"]})
        if days >= cfg["pause_service_days"] and not inv.get("customer_hold"):
            plan.append({"invoice_id": inv["id"], "client_name": inv.get("client_name"),
                         "days_overdue": days, "action": "pause_service",
                         "balance_cents": r["balance"]})
    return {"actions": plan, "count": len(plan)}


@router.post("/autopilot/run")
async def autopilot_run(user: dict = Depends(require("ops:manage"))):
    cfg = await get_config(user)
    if not cfg["enabled"]:
        raise HTTPException(409, "Autopilot is off — enable it in the config first")
    today = today_str()
    # Deliver any reminder emails still queued from earlier runs (e.g. SMTP
    # was not configured yet). Runs before new stages so nothing is lost.
    flushed = await _flush_queued_reminders(user)
    done, errors = [], []
    for r in await _overdue_invoices(user, today):
        inv, days = r["invoice"], r["days"]
        inv = await Scoped(db.invoices, user["tenant_id"]).find_one({"id": inv["id"]})
        try:
            for s in _due_steps(cfg, days, inv.get("autopilot_stages", [])):
                action = s["action"]
                if action in ("nudge", "firm", "final"):
                    msg = _render(s.get("template") or "", inv, days)
                    await _record_reminder(user, inv, msg, action, days)
                    done.append({"invoice_id": inv["id"], "action": action})
                if action == "final" and days >= cfg["late_fee_days"]:
                    res = await _apply_late_fee(user, inv, cfg)
                    if res["applied"]:
                        done.append({"invoice_id": inv["id"], "action": "late_fee",
                                     "fee_cents": res["fee_cents"]})
                if action == "pause_service" or days >= cfg["pause_service_days"]:
                    if not inv.get("customer_hold"):
                        await Scoped(db.invoices, user["tenant_id"]).update_one(
                            {"id": inv["id"]}, {"$set": {"customer_hold": True}})
                        if inv.get("customer_id"):
                            await Scoped(db.customers, user["tenant_id"]).update_one(
                                {"id": inv["customer_id"]},
                                {"$set": {"collections_hold": True}})
                        await audit(user, "collections.pause", "invoice", inv["id"],
                                    {"days_overdue": days})
                        done.append({"invoice_id": inv["id"], "action": "pause_service"})
        except Exception as exc:
            errors.append({"invoice_id": inv["id"], "error": str(exc)[:200]})
    await audit(user, "collections.run", "autopilot", "run",
                {"actions": len(done), "errors": len(errors),
                 "emails_flushed": flushed})
    return {"actions": done, "errors": errors, "emails_flushed": flushed}
