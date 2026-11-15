"""Bill-this-time bridge (Top-20 gap #15): approved billable time -> invoice lines.

Time tracking already captures and approves entries (routers/time.py) and the
field-capture lane posts their labor COST to jobs. This lane is the missing
REVENUE side: approved, billable, not-yet-billed entries are listed, priced at
a BILLING rate, previewed line-by-line, and — only after the user confirms the
preview — converted into a DRAFT invoice in the standard invoices collection
(same doc shape as routers/invoices.py). Converting stamps entries billed
(both ways: invoice lines carry the entry ids); voiding the draft frees them;
once the draft is issued the billing is permanent.

Billing-rate resolution (owner-shaped, 2026-11-14) — first hit wins:
  1. the entry's own billable rate (`billable_rate_cents` on the entry)
  2. the JOB's remembered billing rate (`billing_rate_cents` on the job —
     remembered whenever a conversion bills that job at a rate)
  3. the tenant default billing rate (`settings.default_billing_rate_cents`)
  4. none of the above -> the entry "needs a rate" and cannot be billed
The entry's `rate_cents` is its PAY rate (a cost figure) and is NEVER used as
a billing rate — billing at cost would silently give the work away.

The preview is a hard gate: POST /convert must carry the fingerprint of the
exact lines the user was shown (entry set + grouping + final rates). Any
change since the preview — a rate edited elsewhere, an entry billed by
someone else — changes the recomputed fingerprint and the conversion is
refused with 409 so the user re-checks the new preview. Nothing converts
without that confirmation.

Revenue and cost stay separate sides: conversion reads time entries and
writes an invoice; it never touches job_costs or the journal (a draft posts
nothing). Issuing the draft posts the standard invoice-issue entry through
the same posting builder routers/invoices.py uses.
"""
import hashlib
import json
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/time-billing", tags=["time-billing"])

REVENUE_CODE = "4020"          # same default as routers/invoices.py InvLine
BILLABLE_STATUSES = ("approved", "closed")
GROUPINGS = ("entry", "day", "job")


# --------------------------------------------------------------------------
# shared lookups
# --------------------------------------------------------------------------
def entries_coll(user):
    return Scoped(db.time_entries, user["tenant_id"])


def invoices_coll(user):
    return Scoped(db.invoices, user["tenant_id"])


def jobs_coll(user):
    return Scoped(db.jobs, user["tenant_id"])


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


async def _tenant_doc(tenant_id):
    return await db.tenants.find_one({"id": tenant_id}, {"_id": 0}) or {}


async def tenant_default_billing_rate_cents(tenant_id):
    """Tenant default billing rate from the tenant doc's settings (the same
    nested-settings home lib/burden.py reads), or None when unset."""
    t = await _tenant_doc(tenant_id)
    val = (t.get("settings") or {}).get("default_billing_rate_cents")
    return int(val) if val else None


async def _job_map(user, job_ids):
    out = {}
    for jid in {j for j in job_ids if j}:
        job = await jobs_coll(user).find_one({"id": jid})
        if job:
            out[jid] = job
    return out


def resolve_client(entry, job):
    """The client an entry bills to: its own client_name, else its job's
    customer. Empty string when neither exists (a blocking problem)."""
    name = (entry.get("client_name") or "").strip()
    if name:
        return name
    if job:
        return (job.get("customer_name") or "").strip()
    return ""


def resolve_rate_cents(entry, job, tenant_default_cents, override_cents=None):
    """(rate_cents, source) — owner resolution order; source is one of
    'override' (set in the preview), 'entry', 'job', 'tenant', or None."""
    if override_cents:
        return int(override_cents), "override"
    if entry.get("billable_rate_cents"):
        return int(entry["billable_rate_cents"]), "entry"
    if job and job.get("billing_rate_cents"):
        return int(job["billing_rate_cents"]), "job"
    if tenant_default_cents:
        return int(tenant_default_cents), "tenant"
    return None, None


def entry_amount_cents(entry, rate_cents):
    return int(round((entry.get("hours") or 0) * rate_cents))


def _entry_view(entry, job, tenant_default_cents, override_cents=None):
    rate, source = resolve_rate_cents(entry, job, tenant_default_cents, override_cents)
    return {
        "id": entry["id"],
        "date": entry.get("date"),
        "user_id": entry.get("user_id"),
        "user_name": entry.get("user_name"),
        "hours": entry.get("hours") or 0,
        "notes": entry.get("notes") or "",
        "client_name": resolve_client(entry, job),
        "job_id": entry.get("job_id"),
        "job_number": (job or {}).get("job_number"),
        "job_label": (f"{job['job_number']} — {job.get('customer_name', '')}".strip(" —")
                      if job else None),
        "status": entry.get("status"),
        "own_billable_rate_cents": entry.get("billable_rate_cents"),
        "job_remembered_rate_cents": (job or {}).get("billing_rate_cents"),
        "rate_cents": rate,
        "rate": cents_to_dollars(rate) if rate else None,
        "rate_source": source,
        "needs_rate": rate is None,
        "amount_cents": entry_amount_cents(entry, rate) if rate else None,
        "amount": cents_to_dollars(entry_amount_cents(entry, rate)) if rate else None,
    }


async def _load_entries(user, entry_ids):
    rows = []
    for eid in entry_ids:
        doc = await entries_coll(user).find_one({"id": eid})
        if not doc:
            raise HTTPException(status_code=404, detail=f"Time entry {eid} not found")
        rows.append(doc)
    return rows


def _entry_problems(entry, job, rate_cents):
    problems = []
    if entry.get("status") not in BILLABLE_STATUSES:
        problems.append(f"entry is {entry.get('status')}, not approved")
    if not entry.get("billable", True):
        problems.append("entry is marked non-billable")
    if entry.get("billed_invoice_id"):
        problems.append("entry is already billed")
    if not (entry.get("hours") or 0) > 0:
        problems.append("entry has no hours")
    if not resolve_client(entry, job):
        problems.append("entry has no client — add one before billing")
    if rate_cents is None:
        problems.append("needs a billing rate")
    return problems


# --------------------------------------------------------------------------
# line building (shared by preview + convert so they can never disagree)
# --------------------------------------------------------------------------
def _line_name(grouping, key, views):
    first = views[0]
    if grouping == "entry":
        parts = [first["date"], first["user_name"] or "Tech"]
        if first.get("job_number"):
            parts.append(first["job_number"])
        if first.get("notes"):
            parts.append(first["notes"])
        return " · ".join(p for p in parts if p)
    if grouping == "day":
        bits = [first["date"], "Labor"]
        if first.get("job_number"):
            bits.append(first["job_number"])
        if len(views) > 1:
            bits.append(f"{len(views)} entries")
        return " · ".join(bits)
    # job rollup
    dates = sorted(v["date"] for v in views if v.get("date"))
    span = dates[0] if dates and dates[0] == dates[-1] else (
        f"{dates[0]} – {dates[-1]}" if dates else "")
    head = f"{first['job_number']} — Labor" if first.get("job_number") else "Labor"
    return " · ".join(p for p in [head, span] if p)


def build_lines(views, grouping):
    """Group entry views into invoice lines. Amounts are always the sum of
    per-entry amounts (hours x that entry's resolved rate), so every grouping
    of the same entries totals identically."""
    groups = OrderedDict()
    for v in views:
        if grouping == "entry":
            key = v["id"]
        elif grouping == "day":
            key = (v["date"], v.get("job_id") or "")
        else:
            key = v.get("job_id") or ""
        groups.setdefault(key, []).append(v)
    lines = []
    for key, members in groups.items():
        amount = sum(m["amount_cents"] for m in members)
        rates = {m["rate_cents"] for m in members}
        sources = {m["rate_source"] for m in members}
        lines.append({
            "kind": "service",
            "name": _line_name(grouping, key, members),
            "revenue_code": REVENUE_CODE,
            "amount": cents_to_dollars(amount),
            "amount_cents": amount,
            "hours": round(sum(m["hours"] for m in members), 2),
            "rate_cents": rates.pop() if len(rates) == 1 else None,
            "rate_source": sources.pop() if len(sources) == 1 else "mixed",
            "time_entry_ids": [m["id"] for m in members],
            "entries": [{
                "id": m["id"], "date": m["date"], "user_name": m["user_name"],
                "hours": m["hours"], "rate_cents": m["rate_cents"],
                "rate_source": m["rate_source"], "amount_cents": m["amount_cents"],
                "job_id": m.get("job_id"), "job_number": m.get("job_number"),
                "notes": m["notes"],
            } for m in members],
        })
    return lines


def _fingerprint(entry_ids, grouping, lines, total_cents):
    canon = {
        "entries": sorted(entry_ids),
        "grouping": grouping,
        "lines": [[sorted(l["time_entry_ids"]), l["amount_cents"],
                   [e["rate_cents"] for e in sorted(l["entries"], key=lambda e: e["id"])]]
                  for l in lines],
        "total": total_cents,
    }
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()[:32]


async def _prepare(user, entry_ids, grouping, overrides_dollars):
    """Load + validate + price a conversion. Returns (entries, jobs, views,
    lines, problems, client_name). Pure read — creates nothing."""
    if grouping not in GROUPINGS:
        raise HTTPException(status_code=422, detail=f"grouping must be one of {GROUPINGS}")
    if not entry_ids:
        raise HTTPException(status_code=422, detail="Select at least one time entry")
    if len(set(entry_ids)) != len(entry_ids):
        raise HTTPException(status_code=422, detail="Duplicate entry ids")
    entries = await _load_entries(user, entry_ids)
    jobs = await _job_map(user, [e.get("job_id") for e in entries])
    default_rate = await tenant_default_billing_rate_cents(user["tenant_id"])
    overrides = {eid: to_cents(v) for eid, v in (overrides_dollars or {}).items()
                 if v is not None}
    views, problems = [], []
    for e in entries:
        job = jobs.get(e.get("job_id") or "")
        rate, _ = resolve_rate_cents(e, job, default_rate, overrides.get(e["id"]))
        for p in _entry_problems(e, job, rate):
            problems.append({"entry_id": e["id"], "problem": p})
        views.append(_entry_view(e, job, default_rate, overrides.get(e["id"])))
    clients = {v["client_name"] for v in views if v["client_name"]}
    if len(clients) > 1:
        problems.append({"entry_id": None,
                         "problem": "entries span more than one client — bill one client per invoice"})
    lines = []
    if not problems:
        lines = build_lines(views, grouping)
    return entries, jobs, views, lines, problems, (clients.pop() if len(clients) == 1 else "")


# --------------------------------------------------------------------------
# settings: tenant default billing rate
# --------------------------------------------------------------------------
class SettingsBody(BaseModel):
    default_billing_rate: Optional[float] = Field(default=None, ge=0, le=10000)


@router.get("/settings")
async def get_settings(user: dict = Depends(require("ops:manage"))):
    rate = await tenant_default_billing_rate_cents(user["tenant_id"])
    return {"default_billing_rate_cents": rate,
            "default_billing_rate": cents_to_dollars(rate) if rate else None}


@router.put("/settings")
async def put_settings(body: SettingsBody, user: dict = Depends(require("ops:manage"))):
    t = await _tenant_doc(user["tenant_id"])
    settings = dict(t.get("settings") or {})
    if body.default_billing_rate:
        settings["default_billing_rate_cents"] = to_cents(body.default_billing_rate)
    else:
        settings.pop("default_billing_rate_cents", None)
    # read-modify-write of the whole settings object (no dotted-path $set)
    await db.tenants.update_one({"id": user["tenant_id"]}, {"$set": {"settings": settings}})
    await _audit(user, "time_billing.default_rate", "tenant", user["tenant_id"],
                 {"default_billing_rate_cents": settings.get("default_billing_rate_cents")})
    return {"default_billing_rate_cents": settings.get("default_billing_rate_cents"),
            "default_billing_rate": (cents_to_dollars(settings["default_billing_rate_cents"])
                                     if settings.get("default_billing_rate_cents") else None)}


# --------------------------------------------------------------------------
# unbilled view
# --------------------------------------------------------------------------
@router.get("/unbilled")
async def unbilled(user: dict = Depends(require("ops:manage"))):
    rows = await entries_coll(user).find_list(
        {"status": {"$in": list(BILLABLE_STATUSES)}, "billable": True},
        sort=[("date", 1), ("created_at", 1)], limit=2000)
    rows = [r for r in rows if not r.get("billed_invoice_id") and (r.get("hours") or 0) > 0]
    jobs = await _job_map(user, [r.get("job_id") for r in rows])
    default_rate = await tenant_default_billing_rate_cents(user["tenant_id"])
    views = [_entry_view(r, jobs.get(r.get("job_id") or ""), default_rate) for r in rows]

    groups = OrderedDict()
    for v in views:
        g = groups.setdefault(v["client_name"] or "(no client)",
                              {"client_name": v["client_name"], "jobs": OrderedDict(),
                               "entries": [], "hours": 0.0, "amount_cents": 0,
                               "needs_rate_count": 0})
        g["entries"].append(v)
        g["hours"] = round(g["hours"] + v["hours"], 2)
        if v["needs_rate"]:
            g["needs_rate_count"] += 1
        else:
            g["amount_cents"] += v["amount_cents"]
        jg = g["jobs"].setdefault(v.get("job_id") or "",
                                  {"job_id": v.get("job_id"), "job_number": v.get("job_number"),
                                   "job_label": v.get("job_label"),
                                   "remembered_rate_cents": v.get("job_remembered_rate_cents"),
                                   "entry_ids": [], "hours": 0.0, "amount_cents": 0})
        jg["entry_ids"].append(v["id"])
        jg["hours"] = round(jg["hours"] + v["hours"], 2)
        if not v["needs_rate"]:
            jg["amount_cents"] += v["amount_cents"]
    out_groups = []
    for g in groups.values():
        g = dict(g)
        g["jobs"] = list(g["jobs"].values())
        out_groups.append(g)
    return {
        "default_billing_rate_cents": default_rate,
        "entries": views,
        "groups": out_groups,
        "total_hours": round(sum(v["hours"] for v in views), 2),
        "total_amount_cents": sum(v["amount_cents"] for v in views if not v["needs_rate"]),
        "needs_rate_count": sum(1 for v in views if v["needs_rate"]),
    }


# --------------------------------------------------------------------------
# preview (the confirmation gate) + convert
# --------------------------------------------------------------------------
class PreviewBody(BaseModel):
    entry_ids: list[str]
    grouping: str = "entry"
    rate_overrides: dict[str, float] = {}   # entry_id -> $/hr chosen in the preview


class ConvertBody(PreviewBody):
    fingerprint: str                        # from the preview the user confirmed
    due_date: Optional[str] = None
    terms: Optional[str] = "Net 30"


@router.post("/preview")
async def preview(body: PreviewBody, user: dict = Depends(require("ops:manage"))):
    _, _, _, lines, problems, client = await _prepare(
        user, body.entry_ids, body.grouping, body.rate_overrides)
    total = sum(l["amount_cents"] for l in lines)
    return {
        "client_name": client,
        "grouping": body.grouping,
        "lines": lines,
        "total_cents": total,
        "total": cents_to_dollars(total),
        "problems": problems,
        "can_convert": not problems,
        "fingerprint": (_fingerprint(body.entry_ids, body.grouping, lines, total)
                        if not problems else None),
    }


@router.post("/convert")
async def convert(body: ConvertBody, user: dict = Depends(require("ops:manage"))):
    entries, jobs, views, lines, problems, client = await _prepare(
        user, body.entry_ids, body.grouping, body.rate_overrides)
    if problems:
        raise HTTPException(status_code=409, detail={
            "message": "These entries can't be billed as shown — re-check the preview",
            "problems": problems})
    total = sum(l["amount_cents"] for l in lines)
    if _fingerprint(body.entry_ids, body.grouping, lines, total) != body.fingerprint:
        raise HTTPException(status_code=409, detail=(
            "The preview has changed since you reviewed it (a rate or entry "
            "changed) — open the preview again and confirm the new lines."))

    inv_id = new_id()
    overrides = {eid: to_cents(v) for eid, v in (body.rate_overrides or {}).items()
                 if v is not None}
    view_by_id = {v["id"]: v for v in views}

    # Stamp entries first (each re-checked unbilled); roll back on any race.
    stamped = []
    try:
        for e in entries:
            fresh = await entries_coll(user).find_one({"id": e["id"]})
            if not fresh or fresh.get("billed_invoice_id"):
                raise HTTPException(status_code=409, detail=(
                    f"Entry {e['id']} was billed while you were previewing — "
                    "re-open the preview."))
            patch = {"billed_invoice_id": inv_id, "billed_at": now_iso(),
                     "billed_rate_cents": view_by_id[e["id"]]["rate_cents"]}
            if overrides.get(e["id"]):
                patch["billable_rate_cents"] = overrides[e["id"]]
            await entries_coll(user).update_one({"id": e["id"]}, {
                "$set": patch,
                "$push": {"billing_history": {
                    "action": "billed", "invoice_id": inv_id, "at": now_iso(),
                    "by": user.get("name"), "rate_cents": patch["billed_rate_cents"]}}})
            stamped.append(e["id"])
    except HTTPException:
        for eid in stamped:
            await entries_coll(user).update_one({"id": eid}, {
                "$set": {"billed_invoice_id": None, "billed_at": None,
                         "billed_rate_cents": None}})
        raise

    # Remember the rate per job: the single rate the job was billed at, or —
    # when its entries went at mixed rates — the rate on its largest entry.
    by_job = {}
    for e in entries:
        if e.get("job_id"):
            by_job.setdefault(e["job_id"], []).append(view_by_id[e["id"]])
    for jid, vs in by_job.items():
        rates = {v["rate_cents"] for v in vs}
        if len(rates) == 1:
            remembered = vs[0]["rate_cents"]
        else:
            remembered = max(vs, key=lambda v: (v["hours"], v["date"] or ""))["rate_cents"]
        await jobs_coll(user).update_one({"id": jid}, {"$set": {
            "billing_rate_cents": remembered,
            "billing_rate_updated_at": now_iso(),
            "billing_rate_source_invoice": inv_id}})

    doc = {
        "id": inv_id, "client_name": client, "customer_id": None,
        "lines": [{k: l[k] for k in ("kind", "name", "revenue_code", "amount")}
                  | {"time_entry_ids": l["time_entry_ids"], "hours": l["hours"],
                     "rate_cents": l["rate_cents"]}
                  for l in lines],
        "subtotal_cents": total, "tax_cents": 0, "total_cents": total,
        "tax_jurisdiction_id": None,
        "status": "draft", "paid_cents": 0, "payments": [],
        "due_date": body.due_date, "terms": body.terms or "Net 30",
        "issue_entry": None, "created_at": now_iso(),
        "source": "time_billing",
        "time_entry_ids": [e["id"] for e in entries],
    }
    await invoices_coll(user).insert_one(doc)
    await _audit(user, "time_billing.convert", "invoice", inv_id,
                 {"client": client, "entries": len(entries), "total_cents": total,
                  "grouping": body.grouping})
    doc.pop("_id", None)
    doc["total"] = cents_to_dollars(doc["total_cents"])
    doc["balance_cents"] = doc["total_cents"]
    doc["balance"] = doc["total"]
    return doc


# --------------------------------------------------------------------------
# draft lifecycle: issue (billing becomes permanent) / void (frees entries)
# --------------------------------------------------------------------------
@router.post("/invoices/{invoice_id}/issue")
async def issue_draft(invoice_id: str, user: dict = Depends(require("ops:manage"))):
    inv = await invoices_coll(user).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.get("source") != "time_billing":
        raise HTTPException(status_code=409, detail="Not a time-billing draft")
    if inv["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"Only drafts can be issued (is {inv['status']})")
    by_code = {}
    for ln in inv["lines"]:
        amt = to_cents(ln["amount"])
        by_code[ln.get("revenue_code", REVENUE_CODE)] = by_code.get(ln.get("revenue_code", REVENUE_CODE), 0) + amt
    rev_lines = [{"revenue_code": code, "amount_cents": amt} for code, amt in by_code.items()]
    entry = await L.post_journal_entry(db, user["tenant_id"], user, P.build_invoice_issue(
        revenue_lines=rev_lines, tax_cents=0, cogs_cents=0,
        gross_cents=inv["total_cents"], entry_date=today_str(), source_ref=invoice_id,
        memo=f"Invoice — {inv['client_name']} (time billing)",
    ), status="posted")
    await invoices_coll(user).update_one({"id": invoice_id}, {"$set": {
        "status": "issued", "issue_entry": entry["number"]}})
    await _audit(user, "time_billing.issue", "invoice", invoice_id,
                 {"entry": entry["number"]})
    inv.update(status="issued", issue_entry=entry["number"])
    inv["total"] = cents_to_dollars(inv["total_cents"])
    inv["balance_cents"] = inv["total_cents"]
    inv["balance"] = inv["total"]
    inv.pop("_id", None)
    return inv


@router.post("/invoices/{invoice_id}/void")
async def void_draft(invoice_id: str, user: dict = Depends(require("ops:manage"))):
    inv = await invoices_coll(user).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.get("source") != "time_billing":
        raise HTTPException(status_code=409, detail="Not a time-billing invoice")
    if inv["status"] != "draft":
        raise HTTPException(status_code=409, detail=(
            "Only a draft can be voided — once issued, the time on it stays billed"))
    await invoices_coll(user).update_one({"id": invoice_id}, {"$set": {"status": "void"}})
    freed = 0
    for eid in inv.get("time_entry_ids", []):
        e = await entries_coll(user).find_one({"id": eid})
        if e and e.get("billed_invoice_id") == invoice_id:
            await entries_coll(user).update_one({"id": eid}, {
                "$set": {"billed_invoice_id": None, "billed_at": None,
                         "billed_rate_cents": None},
                "$push": {"billing_history": {
                    "action": "unbilled", "invoice_id": invoice_id,
                    "at": now_iso(), "by": user.get("name"),
                    "reason": "draft voided"}}})
            freed += 1
    await _audit(user, "time_billing.void", "invoice", invoice_id, {"freed": freed})
    return {"ok": True, "freed_entries": freed}
