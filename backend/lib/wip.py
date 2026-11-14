"""WIP schedule — work in progress (owner-approved 2026-11-14, job-costing
ask #5 — the owner's better-package: live computation, month-end
snapshots, MoM view, catch-up flag, by-phase rows, plain-language story
note computed from the job's own figures; no LLM anywhere in this file).

The schedule bonding companies and banks demand, and the one contractors
rebuild by hand every month-end (Strategies Group: "rebuilt by hand every
month-end and takes days rather than hours"). For every open job:

  contract value   estimated revenue incl. APPROVED change orders —
                   exactly the est_rev math of routers/jobs._profit
  estimated costs  estimated costs incl. approved change orders
  costs to date    sum of job_costs
  % complete       cost-to-cost: costs to date / estimated total costs
  earned revenue   contract x % complete (% capped at 100 for revenue;
                   the raw % is reported alongside, never hidden)
  billed to date   sum of the job's invoice subtotals — exactly the
                   actual_revenue math of jobs._profit, so the two
                   surfaces always agree
  over/under       billed - earned  (+ = over-billed, - = under-billed)

HONEST STATES: a job with no estimated costs has no % complete (None,
rendered "—") and no earned revenue — never a fake 0% or a divide by
zero. A job whose costs pass its estimate shows % > 100 plainly and its
note says so.

PHASES: a job may carry `phases: [{id, name, budget_cents: {category:
cents}}]` (the shape fixed by feat/job-phase-costing, lib/jobphases).
When present, each job row breaks into phase rows: phase % complete is
the phase's own cost-to-cost; the phase's contract share is allocated by
its share of the job's estimated costs (phase budget / job estimated
costs), and phase earned = share x % complete. Invoices carry no phase
dimension in the tree, so billed / over-under are JOB-level only — phase
rows report billed as None rather than inventing a split.

STORY NOTE: deterministic sentences built only from figures on the row.
It names the largest category variance driver (actual vs the category's
estimated share), the billing position vs earned revenue, an over-100%
cost position, or — when none of those is true — that the job is
tracking to plan.

CATCH-UP FLAG: against the tenant's most recent snapshot, a job whose
% complete jumped by >= CATCHUP_JUMP_PCT points while its billed-to-date
did not move at all is flagged `billing_behind` ("work is ahead of
billing"). Jobs absent from the snapshot are new, not behind.
"""
from db import db
from lib.base import Scoped, new_id, now_iso, cents_to_dollars

CATEGORIES = ("material", "labor", "equipment", "subcontractor", "other")
OPEN_STATUSES = ("estimate", "active", "completed")   # not "closed"
CATCHUP_JUMP_PCT = 5.0     # % complete points that count as a "jump"
NOTE_VARIANCE_PCT = 10.0   # category variance worth naming in the note


# ---------------- pure per-job math ----------------

def _estimates(job: dict) -> tuple:
    """(contract_cents, est_cost_cents, est_by_cat) incl. approved COs —
    the same totals jobs._profit computes. Change-order cost deltas are
    single totals (not per-category), so they land in the total only."""
    est_rev = job.get("estimated_revenue_cents", 0) or 0
    est_by_cat = {c: (job.get("estimated_costs_cents") or {}).get(c, 0)
                  for c in CATEGORIES}
    est_cost = sum(est_by_cat.values())
    for co in job.get("change_orders", []):
        if co.get("approved"):
            est_rev += co.get("revenue_delta_cents", 0) or 0
            est_cost += co.get("cost_delta_cents", 0) or 0
    return est_rev, est_cost, est_by_cat


def pct_complete(costs_cents: int, est_cost_cents: int):
    """Cost-to-cost % complete. None when there is no estimate to divide
    by (honest state); raw value otherwise — may exceed 100."""
    if not est_cost_cents or est_cost_cents <= 0:
        return None
    return round(costs_cents / est_cost_cents * 100, 2)


def _earned(contract_cents: int, pct):
    """Earned revenue: contract x % complete, % capped at 100 — revenue
    is never earned past the contract; the excess is a cost overrun,
    reported as such. None when % complete is unknown."""
    if pct is None:
        return None
    return int(round(contract_cents * min(pct, 100.0) / 100.0))


def story_note(row: dict) -> str:
    """One or two plain sentences, computed from the row's own figures.
    Only statements the numbers support; falls back to tracking-to-plan."""
    parts = []
    name = row.get("job_number", "This job")
    pct = row.get("pct_complete")
    if pct is not None and pct > 100:
        parts.append(
            f"Costs have passed the estimate — {name} is at {pct}% of "
            f"its estimated costs, so the job is running over budget.")
    else:
        # biggest category variance driver: actual vs estimated share
        worst_cat, worst_over = None, 0.0
        for cat in CATEGORIES:
            est = row.get("estimated_by_category_cents", {}).get(cat, 0)
            act = row.get("actual_by_category_cents", {}).get(cat, 0)
            if est and est > 0:
                over = (act - est) / est * 100
                if over > worst_over:
                    worst_cat, worst_over = cat, over
        if worst_cat and worst_over >= NOTE_VARIANCE_PCT:
            parts.append(
                f"{worst_cat.capitalize()} is the biggest driver — "
                f"running {worst_over:.0f}% over its share of the "
                f"estimate.")
    if row.get("billing_behind"):
        parts.append("Work has moved since the last snapshot but "
                     "billing has not — work is ahead of billing.")
    ou = row.get("over_under_cents")
    if ou is not None and ou != 0:
        amt = f"${abs(ou) / 100:,.2f}"
        if ou < 0:
            parts.append(f"Billing trails earned revenue by {amt}.")
        else:
            parts.append(f"Billed {amt} ahead of earned revenue.")
    if not parts:
        return f"{name} is tracking to plan — costs and billing are in " \
               f"line with the estimate."
    return " ".join(parts[:2])


def _phase_rows(job: dict, costs: list, contract_cents: int,
                est_cost_cents: int) -> list:
    """Phase rows for a job carrying `phases` (jobphases shape). Empty
    list for unphased jobs — the job row renders exactly as before."""
    phases = job.get("phases") or []
    out = []
    for ph in phases:
        pcosts = [c for c in costs if c.get("phase_id") == ph.get("id")]
        actual = sum(c["amount_cents"] for c in pcosts)
        budget = sum((ph.get("budget_cents") or {}).values())
        pct = pct_complete(actual, budget)
        share = (budget / est_cost_cents
                 if est_cost_cents and est_cost_cents > 0 else None)
        contract_share = (int(round(contract_cents * share))
                          if share is not None else None)
        out.append({
            "phase_id": ph.get("id"), "name": ph.get("name", ""),
            "budget_cents": budget,
            "costs_to_date_cents": actual,
            "pct_complete": pct,
            "contract_share_cents": contract_share,
            "earned_revenue_cents": (_earned(contract_share, pct)
                                     if contract_share is not None
                                     else None),
            # invoices carry no phase dimension — job-level only
            "billed_to_date_cents": None,
            "over_under_cents": None,
        })
    return out


def job_wip_row(job: dict, costs: list, invoices: list,
                prev_row: dict = None) -> dict:
    """One job's WIP row. Pure compute over the job doc, its cost lines
    and its invoices. `prev_row` (same job in the latest snapshot) arms
    the catch-up flag."""
    contract, est_cost, est_by_cat = _estimates(job)
    actual_by_cat = {c: 0 for c in CATEGORIES}
    for c in costs:
        actual_by_cat[c["category"]] = \
            actual_by_cat.get(c["category"], 0) + c["amount_cents"]
    costs_to_date = sum(actual_by_cat.values())
    billed = sum(i.get("subtotal_cents", 0) for i in invoices)
    pct = pct_complete(costs_to_date, est_cost)
    earned = _earned(contract, pct)
    over_under = (billed - earned) if earned is not None else None

    billing_behind = False
    if prev_row is not None and pct is not None \
            and prev_row.get("pct_complete") is not None:
        jumped = pct - prev_row["pct_complete"] >= CATCHUP_JUMP_PCT
        billed_still = billed == (prev_row.get("billed_to_date_cents") or 0)
        billing_behind = bool(jumped and billed_still)

    row = {
        "job_id": job["id"], "job_number": job.get("job_number", ""),
        "customer_name": job.get("customer_name", ""),
        "status": job.get("status", ""),
        "contract_cents": contract,
        "estimated_costs_cents": est_cost,
        "estimated_by_category_cents": est_by_cat,
        "costs_to_date_cents": costs_to_date,
        "actual_by_category_cents": actual_by_cat,
        "pct_complete": pct,
        "pct_complete_raw": pct,   # uncapped; >100 is shown honestly
        "earned_revenue_cents": earned,
        "billed_to_date_cents": billed,
        "over_under_cents": over_under,
        "over_billed": (over_under or 0) > 0 if over_under is not None
                       else None,
        "billing_behind": billing_behind,
        "phases": _phase_rows(job, costs, contract, est_cost),
    }
    row["note"] = story_note(row)
    return row


def roll_up(rows: list) -> dict:
    """Tenant-wide totals over schedule rows."""
    t = {"jobs": len(rows), "contract_cents": 0, "estimated_costs_cents": 0,
         "costs_to_date_cents": 0, "earned_revenue_cents": 0,
         "billed_to_date_cents": 0, "over_under_cents": 0,
         "flagged": 0}
    for r in rows:
        t["contract_cents"] += r["contract_cents"]
        t["estimated_costs_cents"] += r["estimated_costs_cents"]
        t["costs_to_date_cents"] += r["costs_to_date_cents"]
        t["earned_revenue_cents"] += r["earned_revenue_cents"] or 0
        t["billed_to_date_cents"] += r["billed_to_date_cents"]
        t["over_under_cents"] += r["over_under_cents"] or 0
        t["flagged"] += 1 if r.get("billing_behind") else 0
    t["pct_complete"] = pct_complete(t["costs_to_date_cents"],
                                     t["estimated_costs_cents"])
    return t


def dollars_view(row: dict) -> dict:
    """A row with dollar floats alongside the cents (house display
    convention) — cents stay authoritative."""
    out = dict(row)
    for k in ("contract_cents", "estimated_costs_cents",
              "costs_to_date_cents", "earned_revenue_cents",
              "billed_to_date_cents", "over_under_cents",
              "budget_cents", "contract_share_cents"):
        if out.get(k) is not None:
            out[k[:-6]] = cents_to_dollars(out[k])
    if "phases" in row:
        out["phases"] = [dollars_view(p) for p in row.get("phases", [])]
    return out


# ---------------- schedule assembly (async, tenant-scoped) ----------------

def snapshots_coll(tenant_id: str):
    return Scoped(db.wip_snapshots, tenant_id)


async def latest_snapshot(tenant_id: str):
    snaps = await snapshots_coll(tenant_id).find_list(
        sort=[("taken_at", -1)], limit=1)
    return snaps[0] if snaps else None


async def build_schedule(tenant_id: str, with_flags: bool = True) -> dict:
    """The live schedule: computed from current jobs/costs/invoices on
    every call — always current as costs and invoices land."""
    job_docs = await Scoped(db.jobs, tenant_id).find_list(
        {"status": {"$in": list(OPEN_STATUSES)}},
        sort=[("created_at", 1)], limit=1000)
    prev = {}
    snap = None
    if with_flags:
        snap = await latest_snapshot(tenant_id)
        if snap:
            prev = {r["job_id"]: r for r in snap.get("rows", [])}
    rows = []
    for job in job_docs:
        costs = await Scoped(db.job_costs, tenant_id).find_list(
            {"job_id": job["id"]}, limit=5000)
        invoices = await Scoped(db.invoices, tenant_id).find_list(
            {"job_id": job["id"]}, limit=5000)
        rows.append(job_wip_row(job, costs, invoices, prev.get(job["id"])))
    return {
        "rows": [dollars_view(r) for r in rows],
        "totals": dollars_view(roll_up(rows)),
        "generated_at": now_iso(),
        "latest_snapshot": ({"id": snap["id"], "period": snap.get("period"),
                             "taken_at": snap.get("taken_at")}
                            if snap else None),
    }


async def take_snapshot(tenant_id: str, user: dict, period: str,
                        label: str = "") -> dict:
    """Freeze the current schedule as a month-end snapshot. Rows are
    stored as computed at this instant — later costs/invoices never move
    a saved snapshot."""
    sched = await build_schedule(tenant_id, with_flags=True)
    doc = {
        "id": new_id(), "period": period,
        "label": label or f"Month-end {period}",
        "taken_at": now_iso(), "taken_by": user.get("id"),
        "rows": sched["rows"], "totals": sched["totals"],
    }
    await snapshots_coll(tenant_id).insert_one(doc)
    doc.pop("_id", None)
    return doc


def compare_schedules(base_rows: list, against_rows: list) -> list:
    """Per-job deltas between two row sets (either may be live). Jobs on
    one side only are marked new / no-longer-open; their deltas are
    against zero."""
    base = {r["job_id"]: r for r in base_rows}
    against = {r["job_id"]: r for r in against_rows}
    out = []
    for jid in sorted(set(base) | set(against),
                      key=lambda j: (against.get(j) or base.get(j))
                      .get("job_number", "")):
        b, a = base.get(jid), against.get(jid)
        ref = a or b
        def d(key):
            return (a.get(key) or 0) - (b.get(key) or 0) \
                if (a is not None and b is not None) else None
        pct_delta = None
        if a is not None and b is not None \
                and a.get("pct_complete") is not None \
                and b.get("pct_complete") is not None:
            pct_delta = round(a["pct_complete"] - b["pct_complete"], 2)
        behind = False
        if a is not None and b is not None \
                and a.get("pct_complete") is not None \
                and b.get("pct_complete") is not None:
            behind = bool(
                a["pct_complete"] - b["pct_complete"] >= CATCHUP_JUMP_PCT
                and (a.get("billed_to_date_cents") or 0)
                == (b.get("billed_to_date_cents") or 0))
        out.append({
            "job_id": jid, "job_number": ref.get("job_number", ""),
            "customer_name": ref.get("customer_name", ""),
            "presence": ("both" if a is not None and b is not None
                         else "new" if a is not None else "gone"),
            "pct_complete_delta": pct_delta,
            "costs_delta_cents": d("costs_to_date_cents"),
            "earned_delta_cents": d("earned_revenue_cents"),
            "billed_delta_cents": d("billed_to_date_cents"),
            "over_under_delta_cents": d("over_under_cents"),
            "billing_behind": behind,
            "base": b, "against": a,
        })
    return out
