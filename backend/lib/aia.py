"""AIA-style progress billing — schedule of values, pay applications in
the standard G702/G703 structure (job-costing ask #8).

NAMING (owner rule): the G702/G703 forms are AIA's own documents. This
feature produces documents in that structure and format, labeled
honestly everywhere as "AIA-style (G702/G703 format)" — never as an
official AIA form.

Model:
- One schedule of values (SOV) per job: line items whose scheduled
  values sum to the contract sum (sum-checked, mismatch warns).
- Billing periods per job (open -> closed). Each open period collects
  per-line entries: work completed THIS period and materials presently
  stored. Closing a period freezes its application (a snapshot, the
  WIP-snapshot pattern) and posts the period's billing as an invoice
  through the books' invoicing machinery — RECORDING ONLY: no payment
  is initiated or moved anywhere (the house posture: we don't touch
  their money).
- The application (G702 summary) and continuation sheet (G703) are
  computed from the job's own data — contract sum from the job,
  change orders approved-only (change-order workflow shapes: a CO's
  `state` wins when present, legacy `approved` flag otherwise),
  retainage per line (line % -> phase override -> job %, the
  retainage lane's resolution order, display/track posture).

All money is integer cents. Every summary figure carries its inputs
in the application dict so each number is traceable.
"""
import io

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, cents_to_dollars
from lib import ledger as L
from lib import posting as P

FORMAT_LABEL = "AIA-style (G702/G703 format)"
RET_RECEIVABLE = "1210"   # Retainage Receivable (retainage lane's account)
AR_ACCOUNT = "1200"
REVENUE_ACCOUNT = "4000"


def sovs_coll(tenant_id):
    return Scoped(db.aia_sovs, tenant_id)


def periods_coll(tenant_id):
    return Scoped(db.aia_periods, tenant_id)


# ---------------- contract figures ----------------
def co_is_approved(co: dict) -> bool:
    """Approved-only, merge-safe across the change-order workflow lane:
    an explicit workflow `state` wins; legacy rows use `approved`."""
    st = co.get("state")
    if st:
        return st == "approved"
    return bool(co.get("approved"))


def contract_figures(job: dict) -> dict:
    original = job.get("estimated_revenue_cents", 0) or 0
    approved = []
    for co in job.get("change_orders", []) or []:
        if co_is_approved(co):
            approved.append({
                "id": co.get("id"),
                "description": co.get("description", ""),
                "revenue_delta_cents": co.get("revenue_delta_cents", 0) or 0,
            })
    co_total = sum(c["revenue_delta_cents"] for c in approved)
    return {
        "original_contract_cents": original,
        "change_orders": approved,
        "change_orders_cents": co_total,
        "contract_sum_to_date_cents": original + co_total,
    }


# ---------------- retainage ----------------
def retainage_pct_for(job: dict, line: dict):
    """Per-line retainage % and where it came from: the line's own %
    wins, then the retainage lane's per-phase override, then the job %.
    Returns (pct, source)."""
    if line.get("retainage_pct") is not None:
        return float(line["retainage_pct"]), "line"
    phase_id = line.get("phase_id")
    overrides = job.get("retainage_phase_pcts") or {}
    if phase_id and isinstance(overrides.get(phase_id), dict):
        pct = overrides[phase_id].get("receivable")
        if pct is not None:
            return float(pct), "phase"
    base = job.get("retainage_pct")
    if base:
        return float(base), "job"
    return 0.0, "none"


def _held(total_completed_stored: int, pct: float) -> int:
    return int(round(total_completed_stored * pct / 100.0))


# ---------------- SOV ----------------
def sov_total(lines: list) -> int:
    return sum(l.get("scheduled_value_cents", 0) or 0 for l in lines)


def _money(cents) -> str:
    return f"${cents_to_dollars(cents):,.2f}"


def sov_sum_check(lines: list, contract_sum_cents: int) -> dict:
    total = sov_total(lines)
    matches = total == contract_sum_cents
    warning = None
    if not matches:
        diff = total - contract_sum_cents
        warning = (
            f"Schedule of values totals {_money(total)} but the "
            f"contract sum to date is {_money(contract_sum_cents)} "
            f"({'over' if diff > 0 else 'under'} by "
            f"{_money(abs(diff))}). The application still "
            f"computes, but the SOV should reconcile to the contract.")
    return {"sov_total_cents": total,
            "contract_sum_to_date_cents": contract_sum_cents,
            "matches": matches, "warning": warning}


def phase_name(job: dict, phase_id):
    if not phase_id:
        return None
    for p in job.get("phases") or []:
        if p.get("id") == phase_id:
            return p.get("name")
    return None


# ---------------- the math (pure) ----------------
def compute_lines(job: dict, sov_lines: list, prev_lines: dict,
                  entries: dict) -> list:
    """G703 continuation rows. prev_lines maps line_id -> the frozen
    row from the previous closed application ({} for period 1)."""
    rows = []
    for line in sov_lines:
        lid = line["id"]
        scheduled = line.get("scheduled_value_cents", 0) or 0
        prev = prev_lines.get(lid, {})
        prev_completed = prev.get("total_completed_stored_cents", 0) or 0
        entry = entries.get(lid, {}) or {}
        this_period = entry.get("this_period_cents", 0) or 0
        stored = entry.get("stored_cents", 0) or 0
        total = prev_completed + this_period + stored
        if scheduled > 0:
            pct = round(total / scheduled * 100, 2)
        else:
            pct = 100.0 if total > 0 else 0.0
        pct_ret, pct_source = retainage_pct_for(job, line)
        rows.append({
            "line_id": lid,
            "item_no": line.get("item_no", ""),
            "description": line.get("description", ""),
            "phase_id": line.get("phase_id"),
            "phase_name": phase_name(job, line.get("phase_id")),
            "scheduled_value_cents": scheduled,
            "previous_completed_cents": prev_completed,
            "this_period_cents": this_period,
            "stored_cents": stored,
            "total_completed_stored_cents": total,
            "pct_complete": pct,
            "balance_to_finish_cents": scheduled - total,
            "retainage_pct": pct_ret,
            "retainage_pct_source": pct_source,
            "retainage_held_cents": _held(total, pct_ret),
        })
    return rows


def compute_application(job: dict, sov_lines: list, prev_application,
                        entries: dict, period_no: int) -> dict:
    """The G702 summary + G703 rows for one period. prev_application is
    the previous closed period's frozen application (or None)."""
    figures = contract_figures(job)
    prev_lines = {}
    if prev_application:
        prev_lines = {r["line_id"]: r
                      for r in prev_application.get("lines", [])}
    rows = compute_lines(job, sov_lines, prev_lines, entries)

    total_completed = sum(r["total_completed_stored_cents"] for r in rows)
    retainage_total = sum(r["retainage_held_cents"] for r in rows)
    earned_less_retainage = total_completed - retainage_total
    previous_certificates = 0
    if prev_application:
        previous_certificates = prev_application.get(
            "total_earned_less_retainage_cents", 0) or 0
    contract_sum = figures["contract_sum_to_date_cents"]
    app = {
        "format": FORMAT_LABEL,
        "application_no": period_no,
        "job_id": job["id"],
        "job_number": job.get("job_number"),
        "customer_name": job.get("customer_name"),
        # G702 lines 1-3: contract sums
        "original_contract_cents": figures["original_contract_cents"],
        "change_orders_cents": figures["change_orders_cents"],
        "contract_sum_to_date_cents": contract_sum,
        # G702 line 4-9
        "total_completed_stored_cents": total_completed,
        "retainage_total_cents": retainage_total,
        "total_earned_less_retainage_cents": earned_less_retainage,
        "less_previous_certificates_cents": previous_certificates,
        "current_payment_due_cents": earned_less_retainage
                                     - previous_certificates,
        # G702 line 9: balance to finish, including retainage
        "balance_to_finish_cents": contract_sum - total_completed,
        "lines": rows,
        "sum_check": sov_sum_check(sov_lines, contract_sum),
        "inputs": {
            "change_orders": figures["change_orders"],
            "previous_application_no": prev_application.get(
                "application_no") if prev_application else None,
            "entries": {k: dict(v) for k, v in (entries or {}).items()},
        },
    }
    for key in ("original_contract_cents", "change_orders_cents",
                "contract_sum_to_date_cents", "total_completed_stored_cents",
                "retainage_total_cents",
                "total_earned_less_retainage_cents",
                "less_previous_certificates_cents",
                "current_payment_due_cents", "balance_to_finish_cents"):
        app[key[:-6]] = cents_to_dollars(app[key])
    return app


# ---------------- persistence ----------------
async def get_sov(tenant_id: str, job_id: str):
    return await sovs_coll(tenant_id).find_one({"job_id": job_id})


async def save_sov(tenant_id: str, user: dict, job: dict, lines: list):
    """Create/replace the job's schedule of values. Line ids are kept
    stable across saves (matched by item_no) so period history and
    carry-forward survive an SOV edit."""
    existing = await get_sov(tenant_id, job["id"])
    old_by_item = {}
    if existing:
        old_by_item = {l.get("item_no"): l for l in existing.get("lines", [])}
    clean = []
    for raw in lines:
        item_no = str(raw.get("item_no", "")).strip()
        if not item_no:
            raise ValueError("Every schedule line needs an item number")
        if any(l["item_no"] == item_no for l in clean):
            raise ValueError(f"Duplicate item number {item_no}")
        scheduled = raw.get("scheduled_value_cents")
        if scheduled is None:
            scheduled = int(round(float(raw.get("scheduled_value", 0)) * 100))
        old = old_by_item.get(item_no)
        clean.append({
            "id": old["id"] if old else new_id(),
            "item_no": item_no,
            "description": str(raw.get("description", "")).strip(),
            "scheduled_value_cents": int(scheduled),
            "phase_id": raw.get("phase_id") or None,
            "retainage_pct": raw.get("retainage_pct"),
        })
    figures = contract_figures(job)
    check = sov_sum_check(clean, figures["contract_sum_to_date_cents"])
    if existing:
        await sovs_coll(tenant_id).update_one(
            {"job_id": job["id"]},
            {"$set": {"lines": clean, "updated_at": now_iso()}})
        doc = await get_sov(tenant_id, job["id"])
    else:
        doc = {"id": new_id(), "job_id": job["id"], "lines": clean,
               "created_at": now_iso(), "updated_at": now_iso()}
        await sovs_coll(tenant_id).insert_one(doc)
        doc = {k: v for k, v in doc.items() if k != "_id"}
    doc["sum_check"] = check
    return doc


# ---------------- optional mode + estimate seed ----------------
def seed_lines_from_estimate(job: dict) -> list:
    """Pre-fill SOV lines from the job's estimate instead of a blank
    sheet (owner addition): one line per phase when the job is phased
    (scheduled value = the phase's budget total), else one line per
    non-zero estimate category, else a single contract line. The user
    edits from there; the sum-check warns until the lines reconcile
    to the contract sum."""
    lines = []
    phases = [p for p in job.get("phases") or []
              if sum((p.get("budget_cents") or {}).values()) > 0]
    if phases:
        for i, p in enumerate(phases, 1):
            lines.append({
                "item_no": f"{i}.00",
                "description": p.get("name", f"Phase {i}"),
                "scheduled_value_cents":
                    sum((p.get("budget_cents") or {}).values()),
                "phase_id": p.get("id"),
                "retainage_pct": None,
            })
        return lines
    costs = job.get("estimated_costs_cents") or {}
    for cat in ("material", "labor", "equipment", "subcontractor",
                "other"):
        cents = costs.get(cat, 0) or 0
        if cents > 0:
            lines.append({
                "item_no": f"{len(lines) + 1}.00",
                "description": cat.capitalize(),
                "scheduled_value_cents": cents,
                "phase_id": None,
                "retainage_pct": None,
            })
    if not lines:
        lines.append({
            "item_no": "1.00", "description": "Contract work",
            "scheduled_value_cents":
                job.get("estimated_revenue_cents", 0) or 0,
            "phase_id": None, "retainage_pct": None,
        })
    return lines


async def set_mode(tenant_id: str, user: dict, job: dict,
                   enabled: bool) -> dict:
    """AIA-style billing is an OPTIONAL per-job mode, off by default.
    Turning it on seeds the schedule of values from the estimate when
    the job has none, so it can be tried without ceremony. Turning it
    off only hides the mode — periods and applications are kept."""
    await Scoped(db.jobs, tenant_id).update_one(
        {"id": job["id"]}, {"$set": {"aia_enabled": bool(enabled)}})
    job["aia_enabled"] = bool(enabled)
    seeded = False
    sov = await get_sov(tenant_id, job["id"])
    if enabled and not sov:
        sov = await save_sov(tenant_id, user, job,
                             seed_lines_from_estimate(job))
        seeded = True
    return {"aia_enabled": bool(enabled), "seeded": seeded, "sov": sov}


# ---------------- period history ----------------
def period_history(periods: list) -> list:
    """Closed periods in order, each with its frozen application's
    per-line changes since the previous application — the trail an
    owner or architect reads."""
    out = []
    prev_rows = {}
    for p in sorted((p for p in periods if p.get("status") == "closed"),
                    key=lambda x: x["period_no"]):
        app = p.get("application") or {}
        lines = []
        for r in app.get("lines", []):
            prev_total = prev_rows.get(r["line_id"], {}).get(
                "total_completed_stored_cents", 0)
            lines.append({
                "line_id": r["line_id"], "item_no": r["item_no"],
                "description": r["description"],
                "previous_total_cents": prev_total,
                "this_period_cents": r["this_period_cents"],
                "stored_cents": r["stored_cents"],
                "total_cents": r["total_completed_stored_cents"],
                "delta_total_cents":
                    r["total_completed_stored_cents"] - prev_total,
                "pct_complete": r["pct_complete"],
                "retainage_held_cents": r["retainage_held_cents"],
            })
        out.append({
            "period_id": p["id"], "period_no": p["period_no"],
            "label": p.get("label", ""), "closed_at": p.get("closed_at"),
            "invoice_id": p.get("invoice_id"),
            "billing": p.get("billing"),
            "total_completed_stored_cents":
                app.get("total_completed_stored_cents"),
            "current_payment_due_cents":
                app.get("current_payment_due_cents"),
            "lines": lines,
        })
        prev_rows = {r["line_id"]: r for r in app.get("lines", [])}
    return out


async def list_periods(tenant_id: str, job_id: str) -> list:
    return await periods_coll(tenant_id).find_list(
        {"job_id": job_id}, sort=[("period_no", 1)])


async def get_period(tenant_id: str, period_id: str):
    return await periods_coll(tenant_id).find_one({"id": period_id})


async def open_period(tenant_id: str, job: dict, label: str = "",
                      period_from: str = "", period_to: str = "") -> dict:
    periods = await list_periods(tenant_id, job["id"])
    for p in periods:
        if p.get("status") == "open":
            raise ValueError(
                f"Period {p['period_no']} is still open — close it before "
                f"opening the next one")
    last = periods[-1] if periods else None
    doc = {
        "id": new_id(), "job_id": job["id"],
        "period_no": len(periods) + 1,
        # copy-forward: the new period's working draft starts from the
        # last application — its figures become the locked "previous"
        # columns; this-period columns start fresh.
        "copied_from_application_no": last["period_no"] if last else None,
        "label": label or f"Period {len(periods) + 1}",
        "period_from": period_from, "period_to": period_to,
        "status": "open", "entries": {},
        "application": None, "invoice_id": None, "billing": None,
        "created_at": now_iso(), "closed_at": None,
    }
    await periods_coll(tenant_id).insert_one(doc)
    return {k: v for k, v in doc.items() if k != "_id"}


async def set_entries(tenant_id: str, period: dict, entries: dict,
                      sov_lines: list) -> dict:
    if period.get("status") != "open":
        raise ValueError("This period is closed — its figures are frozen")
    known = {l["id"] for l in sov_lines}
    clean = {}
    for line_id, entry in (entries or {}).items():
        if line_id not in known:
            raise ValueError(f"Unknown schedule line {line_id}")
        clean[line_id] = {
            "this_period_cents": int(entry.get("this_period_cents", 0) or 0),
            "stored_cents": int(entry.get("stored_cents", 0) or 0),
        }
    await periods_coll(tenant_id).update_one(
        {"id": period["id"]}, {"$set": {"entries": clean}})
    period["entries"] = clean
    return period


async def last_closed(tenant_id: str, job_id: str):
    closed = [p for p in await list_periods(tenant_id, job_id)
              if p.get("status") == "closed"]
    if not closed:
        return None, None
    last = closed[-1]
    return last, last.get("application")


async def live_application(tenant_id: str, job: dict) -> dict:
    """The current application: the open period's figures computed
    live, or — with no open period — the last closed application (or a
    zero draft for period 1)."""
    sov = await get_sov(tenant_id, job["id"])
    if not sov:
        return {"has_sov": False, "period": None, "application": None}
    _, prev_app = await last_closed(tenant_id, job["id"])
    periods = await list_periods(tenant_id, job["id"])
    open_p = next((p for p in periods if p.get("status") == "open"), None)
    if open_p:
        app = compute_application(job, sov["lines"], prev_app,
                                  open_p.get("entries", {}),
                                  open_p["period_no"])
        return {"has_sov": True, "period": open_p, "application": app,
                "draft": True}
    if prev_app:
        return {"has_sov": True, "period": None, "application": prev_app,
                "draft": False}
    app = compute_application(job, sov["lines"], None, {}, 1)
    return {"has_sov": True, "period": None, "application": app,
            "draft": True}


# ---------------- period close + billing ----------------
async def ensure_retainage_account(tenant_id: str) -> None:
    """Idempotent — make sure every account an AIA close posts to
    exists in the TENANT's chart (ledger invariant I3 reads the tenant
    collection). Tenants seeded before the static chart gained 1210 /
    4000 heal here on first close; mirrors the retainage lane's
    ensure_accounts posture."""
    acc = Scoped(db.accounts, tenant_id)
    existing = {a["code"] for a in await acc.find_list()}
    for code, name, typ, nb in (
            (RET_RECEIVABLE, "Retainage Receivable", "asset", "debit"),
            (REVENUE_ACCOUNT, "Job Contract Revenue", "revenue",
             "credit")):
        if code not in existing:
            await acc.insert_one({
                "id": new_id(), "code": code, "name": name,
                "type": typ, "normal_balance": nb, "parent_code": None,
                "is_system": True, "archived": False,
                "description": None, "created_at": now_iso(),
            })


async def post_period_invoice(tenant_id: str, user: dict, job: dict,
                              period: dict, application: dict,
                              prev_application) -> dict:
    """Post the period's billing as an invoice in the books. The
    invoice certifies THIS period's work: gross = the period's growth
    in completed & stored, held = the period's growth in retainage,
    net due = the application's current payment due. RECORDING ONLY —
    no payment is initiated or moved."""
    prev_completed = prev_application.get(
        "total_completed_stored_cents", 0) if prev_application else 0
    prev_retainage = prev_application.get(
        "retainage_total_cents", 0) if prev_application else 0
    gross = application["total_completed_stored_cents"] - prev_completed
    held = application["retainage_total_cents"] - prev_retainage
    due = application["current_payment_due_cents"]
    if gross <= 0:
        return {"posted": False,
                "reason": "No new completed or stored value this period "
                          "— nothing to bill"}
    if due != gross - held:  # defensive: the G702 chain must reconcile
        raise ValueError("Application figures do not reconcile "
                         "(due != gross - retainage)")
    inv_id = new_id()
    if held > 0:
        await ensure_retainage_account(tenant_id)
        jl = []
        if due > 0:
            jl.append(P.dr(AR_ACCOUNT, due,
                           "Pay application — net of retainage"))
        jl.append(P.dr(RET_RECEIVABLE, held, "Retainage held"))
        jl.append(P.cr(REVENUE_ACCOUNT, gross, "Revenue — progress billing"))
        entry = await L.post_journal_entry(db, tenant_id, user, {
            "entry_date": today_str(), "source": "invoice_issue",
            "source_ref": inv_id,
            "memo": f"{job['job_number']} — {FORMAT_LABEL} application "
                    f"#{period['period_no']}",
            "lines": jl,
        }, status="posted")
    else:
        entry = await L.post_journal_entry(db, tenant_id, user,
            P.build_invoice_issue(
                revenue_lines=[{"revenue_code": REVENUE_ACCOUNT,
                                "amount_cents": gross}],
                tax_cents=0, cogs_cents=0, gross_cents=gross,
                entry_date=today_str(), source_ref=inv_id,
                memo=f"{job['job_number']} — {FORMAT_LABEL} application "
                     f"#{period['period_no']}"),
            status="posted")
    doc = {
        "id": inv_id, "job_id": job["id"],
        "client_name": job.get("customer_name"),
        "lines": [{"kind": "aia", "revenue_code": REVENUE_ACCOUNT,
                   "name": f"{FORMAT_LABEL} — application "
                           f"#{period['period_no']} "
                           f"({job['job_number']})",
                   "amount": cents_to_dollars(gross)}],
        "subtotal_cents": gross, "tax_cents": 0, "total_cents": gross,
        "retainage_held_cents": held, "net_due_cents": due,
        "aia_period_id": period["id"],
        "aia_application_no": period["period_no"],
        "status": "issued", "due_date": None,
        "issue_entry": entry["number"], "paid_entry": None,
        "created_at": now_iso(),
    }
    await Scoped(db.invoices, tenant_id).insert_one(doc)
    doc.pop("_id", None)
    return {"posted": True, "invoice": doc}


async def close_period(tenant_id: str, user: dict, job: dict,
                       period: dict) -> dict:
    """Freeze the period's application and post its billing. A closed
    period's application never moves again: later SOV or entry edits
    only affect open periods."""
    if period.get("status") != "open":
        raise ValueError("This period is already closed")
    sov = await get_sov(tenant_id, job["id"])
    if not sov:
        raise ValueError("This job has no schedule of values")
    _, prev_app = await last_closed(tenant_id, job["id"])
    app = compute_application(job, sov["lines"], prev_app,
                              period.get("entries", {}),
                              period["period_no"])
    app["frozen_at"] = now_iso()
    billing = await post_period_invoice(tenant_id, user, job, period,
                                        app, prev_app)
    await periods_coll(tenant_id).update_one(
        {"id": period["id"]},
        {"$set": {"status": "closed", "closed_at": now_iso(),
                  "application": app,
                  "invoice_id": billing.get("invoice", {}).get("id")
                  if billing.get("posted") else None,
                  "billing": {k: v for k, v in billing.items()
                              if k != "invoice"}}})
    period.update({"status": "closed", "closed_at": now_iso(),
                   "application": app,
                   "invoice_id": billing.get("invoice", {}).get("id")
                   if billing.get("posted") else None,
                   "billing": {k: v for k, v in billing.items()
                               if k != "invoice"}})
    await L.audit(db, tenant_id, user, "job.aia_period_close",
                  {"job": job.get("job_number"),
                   "application_no": period["period_no"],
                   "current_due": app["current_payment_due"]})
    return period


# ---------------- document (house PDF pattern) ----------------
def document_lines(job: dict, application: dict, period: dict) -> list:
    """Plain-text layout of the application + continuation sheet, in
    the standard G702/G703 arrangement, honestly labeled."""
    a = application
    lines = [
        "APPLICATION AND CERTIFICATE FOR PAYMENT",
        FORMAT_LABEL + " — not an official AIA form",
        "",
        f"Project: {job.get('job_number', '')} — "
        f"{job.get('customer_name', '')}",
        f"Application No: {a['application_no']}"
        + (f"   Period: {period.get('label', '')}" if period else ""),
        "",
        "CONTRACTOR'S APPLICATION FOR PAYMENT (summary)",
        f"  1. Original contract sum ............ "
        f"{_money(a['original_contract_cents']):>14}",
        f"  2. Net change by approved change orders "
        f"{_money(a['change_orders_cents']):>11}",
        f"  3. Contract sum to date (1 + 2) ....... "
        f"{_money(a['contract_sum_to_date_cents']):>14}",
        f"  4. Total completed & stored to date ... "
        f"{_money(a['total_completed_stored_cents']):>14}",
        f"  5. Retainage held .................... "
        f"{_money(a['retainage_total_cents']):>14}",
        f"  6. Total earned less retainage (4 - 5)  "
        f"{_money(a['total_earned_less_retainage_cents']):>14}",
        f"  7. Less previous certificates ......... "
        f"{_money(a['less_previous_certificates_cents']):>14}",
        f"  8. Current payment due (6 - 7) ........ "
        f"{_money(a['current_payment_due_cents']):>14}",
        f"  9. Balance to finish, incl. retainage . "
        f"{_money(a['balance_to_finish_cents']):>14}",
        "",
    ]
    if a["sum_check"].get("warning"):
        lines += [f"NOTE: {a['sum_check']['warning']}", ""]
    lines += [
        "CONTINUATION SHEET (schedule of values detail)",
        f"{'Item':<6}{'Description':<26}{'Scheduled':>11}{'Prev':>11}"
        f"{'This per':>10}{'Stored*':>10}{'Total':>11}{'%':>7}"
        f"{'Balance':>11}{'Retain':>10}",
    ]
    for r in a["lines"]:
        lines.append(
            f"{r['item_no']:<6}{r['description'][:25]:<26}"
            f"{_money(r['scheduled_value_cents']):>11}"
            f"{_money(r['previous_completed_cents']):>11}"
            f"{_money(r['this_period_cents']):>10}"
            f"{_money(r['stored_cents']):>10}"
            f"{_money(r['total_completed_stored_cents']):>11}"
            f"{r['pct_complete']:>6.1f}%"
            f"{_money(r['balance_to_finish_cents']):>11}"
            f"{_money(r['retainage_held_cents']):>10}")
    lines += [
        f"{'':<6}{'TOTALS':<26}"
        f"{_money(a['sum_check']['sov_total_cents']):>11}"
        f"{'':>11}{'':>10}{'':>10}"
        f"{_money(a['total_completed_stored_cents']):>11}{'':>7}"
        f"{_money(a['balance_to_finish_cents']):>11}"
        f"{_money(a['retainage_total_cents']):>10}",
        "",
        "Figures are computed from the job's own contract, approved "
        "change orders,",
        "schedule of values and period entries. Retainage is recorded "
        "and shown;",
        "release is between the contractor and their customer.",
        "* Stored = materials/equipment stored this period and not "
        "included in",
        "previous applications or this period's completed work (the "
        "standard",
        "continuation-sheet definition).",
    ]
    return lines


def pdf_bytes(title: str, lines: list) -> bytes:
    """Minimal dependency-free PDF — the house pattern (copied from
    routers/invoice_delivery._pdf_bytes via the WIP lane)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(") \
                .replace(")", "\\)")[:150]
    all_lines = [title, ""] + lines
    pages, chunk = [], []
    for ln in all_lines:
        chunk.append(ln)
        if len(chunk) >= 46:
            pages.append(chunk)
            chunk = []
    if chunk or not pages:
        pages.append(chunk)
    n = 3 + len(pages) * 2
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]

    def w(data: bytes):
        offsets.append(buf.tell())
        buf.write(data)
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(pages)))
    w(b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n")
    w(f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {len(pages)}>>endobj\n"
      .encode())
    w(b"3 0 obj<</Type/Font/Subtype/Type1/BaseFont/Courier>>endobj\n")
    for i, pg in enumerate(pages):
        text = "BT /F1 7 Tf 30 770 Td 13 TL " + " ".join(
            f"({esc(ln)}) Tj T*" for ln in pg) + " ET"
        stream = text.encode("latin-1", "replace")
        w(f"{4 + i * 2} 0 obj<</Type/Page/Parent 2 0 R"
          f"/MediaBox[0 0 612 792]/Contents {5 + i * 2} 0 R"
          f"/Resources<</Font<</F1 3 0 R>>>>>>endobj\n".encode())
        w(f"{5 + i * 2} 0 obj<</Length {len(stream)}>>stream\n".encode()
          + stream + b"\nendstream endobj\n")
    xref = buf.tell()
    buf.write(f"xref\n0 {n + 1}\n0000000000 65535 f \n".encode())
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer<</Size {n + 1}/Root 1 0 R>>\nstartxref\n{xref}\n"
              f"%%EOF".encode())
    return buf.getvalue()
