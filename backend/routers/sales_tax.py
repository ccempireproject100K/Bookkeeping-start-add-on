"""Sales-tax jurisdictions, calculation support, liability, and
return PREPARATION.

What this module does:
- stores the jurisdictions the business collects in — now with stacked
  rate components (state/county/city/district) and full rate HISTORY,
  so a rate edit never rewrites the past;
- reports per-period liability keyed on the accounting date
  (issue_date / sale_date), including POS sales, with tax reversals
  from refunds and credit notes netted out;
- reconciles the computed liability against GL account 2100;
- builds per-state return-preparation worksheets (gross → taxable →
  tax → amount due) with drill-down to source documents, CSV export,
  and a "record payment" action for after the owner files;
- monitors economic-nexus thresholds per state and WARNS at 80%/100%.

What it never does: file, submit, or remit anything to a tax
authority. Preparation only — the owner (or their CPA) files in the
state portal. (Owner ruling 2026-10-20; QuickBooks observes the same
line.) Every figure traces to a calculation snapshot stored on the
source document at issue time, never recomputed live.
"""
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from db import db
from lib.auth import require
from lib.base import new_id, now_iso, today_str, to_cents
from lib import ledger as L
from lib import posting as P
from lib import sales_tax_engine as STE

router = APIRouter(prefix="/sales-tax", tags=["sales-tax"])

PREPARATION_ONLY = (
    "Preparation only: this app calculates sales tax and prepares "
    "return worksheets. It does not file returns or remit payments — "
    "you file in your state's portal, then record the payment here.")

ACTIVITY_CAP = 20000


def _jur_coll():
    return db.tax_jurisdictions


def _reg_coll():
    return db.sales_tax_registrations


def _pay_coll():
    return db.sales_tax_payments


# ------------------------------------------------------------------
# Jurisdictions (v2: components + rate history, backwards compatible)
# ------------------------------------------------------------------
class ComponentIn(BaseModel):
    level: str = Field("state", max_length=20)   # state|county|city|district|combined
    name: str = Field(..., max_length=80)
    rate_pct: float = Field(..., ge=0, le=30)


class JurisdictionIn(BaseModel):
    name: str = Field(..., max_length=80)      # e.g. "Travis County, TX"
    state: str = Field(..., max_length=2)     # 2-letter code
    rate_pct: Optional[float] = Field(default=None, ge=0, le=30)
    components: Optional[List[ComponentIn]] = None
    sourcing: Optional[str] = None             # origin | destination
    filing_frequency: str = "monthly"          # monthly|quarterly|annual
    filing_due_day: int = Field(20, ge=1, le=31)
    registration_id: Optional[str] = None
    notes: Optional[str] = Field(default="", max_length=200)


class JurisdictionPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    rate_pct: Optional[float] = Field(default=None, ge=0, le=30)
    components: Optional[List[ComponentIn]] = None
    effective_from: Optional[str] = None       # for a rate change; default today
    sourcing: Optional[str] = None
    filing_frequency: Optional[str] = None
    filing_due_day: Optional[int] = Field(default=None, ge=1, le=31)
    registration_id: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=200)
    active: Optional[bool] = None


def _components_or_422(body) -> list:
    """Normalise a create/patch payload to a component list."""
    if body.components:
        comps = [{"level": c.level, "name": c.name.strip(),
                  "rate_pct": round(float(c.rate_pct), 4)}
                 for c in body.components if c.name.strip()]
        if not comps:
            raise HTTPException(422, "At least one rate component is required")
        return comps
    if body.rate_pct is not None:
        return [{"level": "combined",
                 "name": getattr(body, "name", None) or getattr(body, "state", "") or "",
                 "rate_pct": round(float(body.rate_pct), 4)}]
    raise HTTPException(422, "Provide rate_pct or components")


def _jur_view(doc: dict) -> dict:
    """Read-view of a jurisdiction: guarantees components + combined rate
    for legacy docs created before v2."""
    out = dict(doc)
    comps, _ = STE.resolve_components_as_of(out, today_str())
    out["components"] = comps
    out["rate_pct"] = STE.combined_rate_pct(comps)
    return out


@router.get("/jurisdictions")
async def list_jurisdictions(user: dict = Depends(require("tax:read"))):
    rows = await _jur_coll().find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0}
    ).sort("name", 1).to_list(200)
    return [_jur_view(r) for r in rows]


@router.post("/jurisdictions")
async def create_jurisdiction(body: JurisdictionIn,
                              user: dict = Depends(require("tax:write"))):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name is required")
    dupe = await _jur_coll().find_one({
        "tenant_id": user["tenant_id"], "name": name, "active": True})
    if dupe:
        raise HTTPException(409, "An active jurisdiction with that name exists")
    state = body.state.strip().upper()
    comps = _components_or_422(body)
    combined = STE.combined_rate_pct(comps)
    today = today_str()
    doc = {
        "id": new_id(), "tenant_id": user["tenant_id"], "name": name,
        "state": state, "rate_pct": combined, "components": comps,
        "rate_history": [{
            "effective_from": today, "components": comps,
            "rate_pct": combined, "note": "initial rate",
        }],
        "sourcing": body.sourcing or STE.sourcing_for_state(state),
        "filing_frequency": body.filing_frequency,
        "filing_due_day": body.filing_due_day,
        "registration_id": body.registration_id,
        "notes": (body.notes or "").strip(), "active": True,
        "created_at": now_iso(),
    }
    await _jur_coll().insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.patch("/jurisdictions/{jid}")
async def update_jurisdiction(jid: str, body: JurisdictionPatch,
                              user: dict = Depends(require("tax:write"))):
    current = await _jur_coll().find_one(
        {"id": jid, "tenant_id": user["tenant_id"]}, {"_id": 0})
    if not current:
        raise HTTPException(404, "Jurisdiction not found")
    patch = {}
    push = {}
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "Name can't be blank")
        patch["name"] = body.name.strip()
    if body.notes is not None:
        patch["notes"] = body.notes.strip()
    if body.active is not None:
        patch["active"] = body.active
    if body.sourcing is not None:
        patch["sourcing"] = body.sourcing
    if body.filing_frequency is not None:
        patch["filing_frequency"] = body.filing_frequency
    if body.filing_due_day is not None:
        patch["filing_due_day"] = body.filing_due_day
    if body.registration_id is not None:
        patch["registration_id"] = body.registration_id
    if body.components is not None or body.rate_pct is not None:
        # A rate change APPENDS history — it never rewrites it. Past
        # invoices keep their snapshots; past periods keep their rates.
        comps = _components_or_422(body)
        combined = STE.combined_rate_pct(comps)
        patch["components"] = comps
        patch["rate_pct"] = combined
        push["rate_history"] = {
            "effective_from": body.effective_from or today_str(),
            "components": comps, "rate_pct": combined,
            "note": "rate updated",
        }
    if not patch and not push:
        raise HTTPException(400, "Nothing to update")
    update = {}
    if patch:
        update["$set"] = patch
    if push:
        update["$push"] = push
    r = await _jur_coll().update_one(
        {"id": jid, "tenant_id": user["tenant_id"]}, update)
    if not r.matched_count:
        raise HTTPException(404, "Jurisdiction not found")
    doc = await _jur_coll().find_one({"id": jid}, {"_id": 0})
    return _jur_view(doc)


@router.get("/jurisdictions/{jid}/rate-history")
async def jurisdiction_rate_history(jid: str,
                                    user: dict = Depends(require("tax:read"))):
    doc = await _jur_coll().find_one(
        {"id": jid, "tenant_id": user["tenant_id"]}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Jurisdiction not found")
    history = doc.get("rate_history") or []
    if not history:
        comps, eff = STE.resolve_components_as_of(doc, today_str())
        history = [{"effective_from": eff, "components": comps,
                    "rate_pct": STE.combined_rate_pct(comps),
                    "note": "legacy flat rate (pre-history)"}]
    return {"jurisdiction_id": jid,
            "history": sorted(history,
                              key=lambda h: h.get("effective_from", ""),
                              reverse=True)}


# ------------------------------------------------------------------
# Period activity: one normalisation pass shared by liability,
# worksheets and the nexus monitor. Everything keys on the accounting
# date (issue_date / sale_date), never created_at.
# ------------------------------------------------------------------
async def _jur_map(tenant_id):
    rows = await _jur_coll().find(
        {"tenant_id": tenant_id}, {"_id": 0}).to_list(500)
    return {j["id"]: j for j in rows}


def _in_period(day, start, end):
    return bool(day) and start <= day < end


async def _activity_rows(tenant_id, start, end):
    """Normalised (invoices, pos_sales, reversals) for [start, end).

    Reversals are periodised by when the RETURN happened, not by the
    original sale's date: a refund in March reverses March liability
    even if the invoice was issued in January.
    """
    jur = await _jur_map(tenant_id)
    inv_rows, pos_rows, rev_rows = [], [], []
    truncated = False

    invs = await db.invoices.find(
        {"tenant_id": tenant_id}, {"_id": 0}).to_list(ACTIVITY_CAP)
    truncated = truncated or len(invs) >= ACTIVITY_CAP
    for inv in invs:
        snap = inv.get("tax_snapshot") or {}
        jid = snap.get("jurisdiction_id") or inv.get("tax_jurisdiction_id")
        jdoc = jur.get(jid) if jid else None
        state = snap.get("state") or (jdoc or {}).get("state")
        # Tax adjustments (refund reversals) periodise on their own date.
        for adj in inv.get("tax_adjustments") or []:
            if _in_period(adj.get("date"), start, end):
                rev_rows.append({
                    "kind": "refund_reversal", "id": inv["id"],
                    "date": adj.get("date"), "state": state,
                    "jurisdiction_id": jid,
                    "tax_cents": int(adj.get("cents", 0) or 0),
                    "sales_cents": 0, "sales_taxable_cents": 0,
                    "label": inv.get("client_name", ""),
                })
        day = inv.get("issue_date") or (inv.get("created_at") or "")[:10]
        if not _in_period(day, start, end):
            continue
        if snap:
            inv_rows.append({
                "kind": "invoice", "id": inv["id"], "date": day,
                "label": inv.get("client_name", ""),
                "state": state, "jurisdiction_id": jid,
                "gross_cents": int(inv.get("subtotal_cents", 0) or 0),
                "taxable_cents": int(snap.get("taxable_sales_cents", 0) or 0),
                "exempt_cents": int(snap.get("exempt_sales_cents", 0) or 0),
                "nontaxable_cents": int(snap.get("nontaxable_sales_cents", 0) or 0),
                "facilitated_sales_cents": int(snap.get("facilitated_sales_cents", 0) or 0),
                "facilitator_tax_cents": int(snap.get("facilitator_tax_cents", 0) or 0),
                "tax_collected_cents": int(inv.get("tax_cents", 0) or 0),
                "tax_calculated_cents": int(snap.get("tax_cents", 0) or 0),
                "engine": snap.get("engine", "manual"),
                "legacy": False,
            })
        else:
            # Pre-engine invoice: the old roll-up's assumption stands
            # (subtotal was called taxable) and is flagged legacy.
            inv_rows.append({
                "kind": "invoice", "id": inv["id"], "date": day,
                "label": inv.get("client_name", ""),
                "state": state, "jurisdiction_id": jid,
                "gross_cents": int(inv.get("subtotal_cents", 0) or 0),
                "taxable_cents": int(inv.get("subtotal_cents", 0) or 0),
                "exempt_cents": 0, "nontaxable_cents": 0,
                "facilitated_sales_cents": 0, "facilitator_tax_cents": 0,
                "tax_collected_cents": int(inv.get("tax_cents", 0) or 0),
                "tax_calculated_cents": int(inv.get("tax_cents", 0) or 0),
                "engine": "legacy", "legacy": True,
            })

    sales = await db.sales.find(
        {"tenant_id": tenant_id}, {"_id": 0}).to_list(ACTIVITY_CAP)
    truncated = truncated or len(sales) >= ACTIVITY_CAP
    for s in sales:
        day = s.get("sale_date") or (s.get("created_at") or "")[:10]
        if not _in_period(day, start, end):
            continue
        snap = s.get("tax_snapshot") or {}
        jid = s.get("tax_jurisdiction_id")
        jdoc = jur.get(jid) if jid else None
        gross = int(s.get("subtotal_cents", 0) or 0) - int(s.get("discount_cents", 0) or 0)
        pos_rows.append({
            "kind": "pos", "id": s["id"], "date": day, "label": "POS sale",
            "state": (jdoc or {}).get("state"),
            "jurisdiction_id": jid,
            "gross_cents": gross,
            "taxable_cents": int(snap.get("taxable_cents", gross) or 0),
            "exempt_cents": 0, "nontaxable_cents": 0,
            "facilitated_sales_cents": 0, "facilitator_tax_cents": 0,
            "tax_collected_cents": int(s.get("tax_cents", 0) or 0),
            "tax_calculated_cents": int(s.get("tax_cents", 0) or 0),
            "engine": snap.get("engine", "legacy"),
            "legacy": not bool(snap),
        })

    credits = await db.customer_credits.find(
        {"tenant_id": tenant_id}, {"_id": 0}).to_list(ACTIVITY_CAP)
    for c in credits:
        if c.get("source") != "credit_note":
            continue
        reversal = int(c.get("tax_reversal_cents", 0) or 0)
        if not reversal:
            continue
        day = c.get("note_date") or (c.get("created_at") or "")[:10]
        if not _in_period(day, start, end):
            continue
        jid = c.get("tax_jurisdiction_id")
        jdoc = jur.get(jid) if jid else None
        amount = int(c.get("amount_cents", 0) or 0)
        rev_rows.append({
            "kind": "credit_note_reversal", "id": c["id"], "date": day,
            "state": (jdoc or {}).get("state"), "jurisdiction_id": jid,
            "tax_cents": reversal,
            "sales_cents": amount,
            "sales_taxable_cents": amount if c.get("taxable", True) else 0,
            "label": c.get("client_name", ""),
        })
    return inv_rows, pos_rows, rev_rows, truncated


def _empty_bucket(jid, jur):
    jdoc = jur.get(jid) if jid else None
    return {
        "jurisdiction_id": jid or "unassigned",
        "name": (jdoc or {}).get("name", "Unassigned"),
        "state": (jdoc or {}).get("state", ""),
        "rate_pct": (jdoc or {}).get("rate_pct"),
        "invoice_count": 0, "pos_count": 0,
        "gross_sales_cents": 0, "taxable_sales_cents": 0,
        "exempt_sales_cents": 0, "nontaxable_sales_cents": 0,
        "facilitated_sales_cents": 0, "facilitator_tax_cents": 0,
        "tax_collected_cents": 0, "tax_reversed_cents": 0,
        "returns_sales_cents": 0, "legacy_count": 0,
    }


def _fold(bucket, row):
    bucket["gross_sales_cents"] += row["gross_cents"]
    bucket["taxable_sales_cents"] += row["taxable_cents"]
    bucket["exempt_sales_cents"] += row["exempt_cents"]
    bucket["nontaxable_sales_cents"] += row["nontaxable_cents"]
    bucket["facilitated_sales_cents"] += row["facilitated_sales_cents"]
    bucket["facilitator_tax_cents"] += row["facilitator_tax_cents"]
    bucket["tax_collected_cents"] += row["tax_collected_cents"]
    if row["kind"] == "invoice":
        bucket["invoice_count"] += 1
    elif row["kind"] == "pos":
        bucket["pos_count"] += 1
    if row.get("legacy"):
        bucket["legacy_count"] += 1


# ------------------------------------------------------------------
# Liability (fixed base: issue_date, POS included, reversals netted)
# ------------------------------------------------------------------
@router.get("/liability")
async def liability(year: int = Query(default_factory=lambda: date.today().year),
                    month: Optional[int] = Query(default=None, ge=1, le=12),
                    user: dict = Depends(require("tax:read"))):
    start, end, _key = STE.period_bounds(year, month=month)
    jur = await _jur_map(user["tenant_id"])
    inv_rows, pos_rows, rev_rows, truncated = await _activity_rows(
        user["tenant_id"], start, end)
    buckets: dict = {}
    for row in inv_rows + pos_rows:
        b = buckets.setdefault(row["jurisdiction_id"] or "unassigned",
                               _empty_bucket(row["jurisdiction_id"], jur))
        _fold(b, row)
    for rev in rev_rows:
        b = buckets.setdefault(rev["jurisdiction_id"] or "unassigned",
                               _empty_bucket(rev["jurisdiction_id"], jur))
        b["tax_reversed_cents"] += rev["tax_cents"]
        b["returns_sales_cents"] += rev["sales_cents"]
    rows = sorted(buckets.values(), key=lambda r: r["name"])
    for r in rows:
        r["net_tax_cents"] = r["tax_collected_cents"] - r["tax_reversed_cents"]
    totals = {
        "invoice_count": sum(r["invoice_count"] for r in rows),
        "pos_count": sum(r["pos_count"] for r in rows),
        "gross_sales_cents": sum(r["gross_sales_cents"] for r in rows),
        "taxable_sales_cents": sum(r["taxable_sales_cents"] for r in rows),
        "exempt_sales_cents": sum(r["exempt_sales_cents"] for r in rows),
        "nontaxable_sales_cents": sum(r["nontaxable_sales_cents"] for r in rows),
        "facilitated_sales_cents": sum(r["facilitated_sales_cents"] for r in rows),
        "tax_collected_cents": sum(r["tax_collected_cents"] for r in rows),
        "tax_reversed_cents": sum(r["tax_reversed_cents"] for r in rows),
        "net_tax_cents": sum(r["net_tax_cents"] for r in rows),
    }
    return {"year": year, "month": month, "period_start": start,
            "period_end": end, "jurisdictions": rows, "totals": totals,
            "truncated": truncated, "note": PREPARATION_ONLY}


# ------------------------------------------------------------------
# GL 2100 reconciliation
# ------------------------------------------------------------------
@router.get("/reconciliation")
async def reconciliation(year: int = Query(default_factory=lambda: date.today().year),
                         month: Optional[int] = Query(default=None, ge=1, le=12),
                         user: dict = Depends(require("tax:read"))):
    """Does the computed liability tie to the ledger? GL 2100 movement
    for the period (credits accrued − debits for reversals/payments)
    vs the document-computed movement. Variance is surfaced, never
    hidden."""
    tenant_id = user["tenant_id"]
    start, end, key = STE.period_bounds(year, month=month)
    lines = await db.journal_lines.find(
        {"tenant_id": tenant_id, "status": "posted", "account_code": "2100"},
        {"_id": 0}).to_list(ACTIVITY_CAP)
    opening = credits = debits = 0
    for ln in lines:
        day = ln.get("entry_date", "")
        net = int(ln.get("credit", 0) or 0) - int(ln.get("debit", 0) or 0)
        if day < start:
            opening += net
        elif day < end:
            credits += int(ln.get("credit", 0) or 0)
            debits += int(ln.get("debit", 0) or 0)
    gl_movement = credits - debits

    inv_rows, pos_rows, rev_rows, _t = await _activity_rows(tenant_id, start, end)
    collected = sum(r["tax_collected_cents"] for r in inv_rows + pos_rows)
    reversed_ = sum(r["tax_cents"] for r in rev_rows)
    payments = await _pay_coll().find(
        {"tenant_id": tenant_id}, {"_id": 0}).to_list(2000)
    paid = sum(int(p.get("amount_cents", 0) or 0) for p in payments
               if _in_period(p.get("payment_date"), start, end))
    expected = collected - reversed_ - paid
    return {
        "period_key": key, "period_start": start, "period_end": end,
        "account_code": "2100",
        "gl": {"opening_cents": opening, "credits_cents": credits,
               "debits_cents": debits, "movement_cents": gl_movement,
               "closing_cents": opening + gl_movement},
        "computed": {"tax_collected_cents": collected,
                     "tax_reversed_cents": reversed_,
                     "payments_cents": paid,
                     "expected_movement_cents": expected},
        "variance_cents": gl_movement - expected,
        "ties_out": gl_movement == expected,
    }


# ------------------------------------------------------------------
# Return-preparation worksheets (preparation only — never filing)
# ------------------------------------------------------------------
async def build_worksheet(tenant_id, state, start, end, period_key):
    """The worksheet a human (or their CPA) types into the state portal:
    gross → deductions → taxable → tax → amount due, each line traced
    to source documents via the drill-down lists."""
    state = (state or "").upper()
    inv_rows, pos_rows, rev_rows, truncated = await _activity_rows(
        tenant_id, start, end)
    rows = [r for r in inv_rows + pos_rows if (r.get("state") or "") == state]
    revs = [r for r in rev_rows if (r.get("state") or "") == state]

    gross = sum(r["gross_cents"] for r in rows)
    exempt = sum(r["exempt_cents"] for r in rows)
    nontaxable = sum(r["nontaxable_cents"] for r in rows)
    facilitated = sum(r["facilitated_sales_cents"] for r in rows)
    facilitator_tax = sum(r["facilitator_tax_cents"] for r in rows)
    taxable = sum(r["taxable_cents"] for r in rows)
    returns_sales = sum(r["sales_cents"] for r in revs)
    returns_taxable = sum(r["sales_taxable_cents"] for r in revs)
    tax_calculated = sum(r["tax_calculated_cents"] for r in rows)
    tax_collected = sum(r["tax_collected_cents"] for r in rows)
    tax_reversed = sum(r["tax_cents"] for r in revs)
    net_tax = tax_collected - tax_reversed

    payments = await _pay_coll().find(
        {"tenant_id": tenant_id, "state": state}, {"_id": 0}).to_list(2000)
    prepayments = sum(int(p.get("amount_cents", 0) or 0) for p in payments
                      if p.get("period_key") == period_key
                      and p.get("kind") == "prepayment")
    return_payments = sum(int(p.get("amount_cents", 0) or 0) for p in payments
                          if p.get("period_key") == period_key
                          and p.get("kind") == "return_payment")

    regs = await _reg_coll().find(
        {"tenant_id": tenant_id, "state": state, "active": True},
        {"_id": 0}).to_list(10)
    jur = await _jur_map(tenant_id)
    registered = bool(regs) or any(
        j.get("state") == state and j.get("active") for j in jur.values())

    return {
        "state": state, "period_key": period_key,
        "period_start": start, "period_end": end,
        "registered": registered,
        "lines": {
            "gross_sales_cents": gross,
            "less_exempt_sales_cents": exempt,
            "less_nontaxable_sales_cents": nontaxable,
            "less_marketplace_facilitated_sales_cents": facilitated,
            "marketplace_facilitator_tax_cents": facilitator_tax,
            "less_returns_sales_cents": returns_sales,
            "taxable_sales_cents": taxable - returns_taxable,
            "tax_calculated_cents": tax_calculated,
            "tax_collected_cents": tax_collected,
            "tax_variance_cents": tax_collected - tax_calculated,
            "less_tax_reversed_cents": tax_reversed,
            "net_tax_cents": net_tax,
            "less_prepayments_cents": prepayments,
            "less_return_payments_cents": return_payments,
            "amount_due_cents": net_tax - prepayments - return_payments,
        },
        "drill_down": {
            "invoices": [{"id": r["id"], "date": r["date"], "label": r["label"],
                          "gross_cents": r["gross_cents"],
                          "taxable_cents": r["taxable_cents"],
                          "tax_cents": r["tax_collected_cents"]}
                         for r in rows if r["kind"] == "invoice"][:200],
            "pos_sales": [{"id": r["id"], "date": r["date"],
                           "gross_cents": r["gross_cents"],
                           "taxable_cents": r["taxable_cents"],
                           "tax_cents": r["tax_collected_cents"]}
                          for r in rows if r["kind"] == "pos"][:200],
            "reversals": [{"id": r["id"], "date": r["date"], "kind": r["kind"],
                           "label": r["label"], "tax_cents": r["tax_cents"],
                           "sales_cents": r["sales_cents"]}
                          for r in revs][:200],
        },
        "counts": {"documents": len(rows), "reversals": len(revs)},
        "truncated": truncated,
        "disclaimer": PREPARATION_ONLY,
        "cpa_note": STE.CPA_NOTE,
    }


def _worksheet_params(year, month, quarter):
    if quarter is not None and month is not None:
        raise HTTPException(422, "Pass month or quarter, not both")
    return STE.period_bounds(year, month=month, quarter=quarter)


@router.get("/worksheet")
async def worksheet(state: str = Query(..., min_length=2, max_length=2),
                    year: int = Query(default_factory=lambda: date.today().year),
                    month: Optional[int] = Query(default=None, ge=1, le=12),
                    quarter: Optional[int] = Query(default=None, ge=1, le=4),
                    user: dict = Depends(require("tax:read"))):
    start, end, key = _worksheet_params(year, month, quarter)
    return await build_worksheet(user["tenant_id"], state.upper(),
                                 start, end, key)


@router.get("/worksheet.csv")
async def worksheet_csv(state: str = Query(..., min_length=2, max_length=2),
                        year: int = Query(default_factory=lambda: date.today().year),
                        month: Optional[int] = Query(default=None, ge=1, le=12),
                        quarter: Optional[int] = Query(default=None, ge=1, le=4),
                        user: dict = Depends(require("tax:read"))):
    import csv
    import io
    start, end, key = _worksheet_params(year, month, quarter)
    ws = await build_worksheet(user["tenant_id"], state.upper(), start, end, key)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Sales-tax return preparation worksheet — NOT a filed return"])
    w.writerow(["State", ws["state"], "Period", ws["period_key"],
                "From", ws["period_start"], "To (exclusive)", ws["period_end"]])
    w.writerow([])
    w.writerow(["Line", "Amount (USD)"])
    for k, v in ws["lines"].items():
        w.writerow([k, f"{v / 100:.2f}"])
    w.writerow([])
    w.writerow(["Source documents"])
    w.writerow(["Kind", "Id", "Date", "Label", "Gross", "Taxable", "Tax"])
    for d in ws["drill_down"]["invoices"]:
        w.writerow(["invoice", d["id"], d["date"], d["label"],
                    f"{d['gross_cents'] / 100:.2f}",
                    f"{d['taxable_cents'] / 100:.2f}",
                    f"{d['tax_cents'] / 100:.2f}"])
    for d in ws["drill_down"]["pos_sales"]:
        w.writerow(["pos", d["id"], d["date"], "POS sale",
                    f"{d['gross_cents'] / 100:.2f}",
                    f"{d['taxable_cents'] / 100:.2f}",
                    f"{d['tax_cents'] / 100:.2f}"])
    for d in ws["drill_down"]["reversals"]:
        w.writerow([d["kind"], d["id"], d["date"], d["label"], "",
                    f"{d['sales_cents'] / 100:.2f}",
                    f"-{d['tax_cents'] / 100:.2f}"])
    w.writerow([])
    w.writerow([PREPARATION_ONLY])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=sales-tax-worksheet-{ws['state']}-{key}.csv"})


# ------------------------------------------------------------------
# Payments — recording only. The owner files + pays in the state
# portal themselves; this posts the bookkeeping entry (Dr 2100 /
# Cr cash) and tracks prepayments against a period.
# ------------------------------------------------------------------
class PaymentIn(BaseModel):
    state: str = Field(..., min_length=2, max_length=2)
    period_key: str = Field(..., max_length=10)   # e.g. "2026-Q1", "2026-03"
    amount: float = Field(..., gt=0)              # dollars
    kind: str = "return_payment"                  # return_payment | prepayment
    payment_method: str = "bank"                  # cash | card | bank
    payment_date: Optional[str] = None            # default today
    memo: Optional[str] = ""


@router.get("/payments")
async def list_payments(state: Optional[str] = None,
                        period_key: Optional[str] = None,
                        user: dict = Depends(require("tax:read"))):
    q = {"tenant_id": user["tenant_id"]}
    if state:
        q["state"] = state.upper()
    if period_key:
        q["period_key"] = period_key
    rows = await _pay_coll().find(q, {"_id": 0}).sort("payment_date", -1).to_list(500)
    return rows


@router.post("/payments")
async def record_payment(body: PaymentIn,
                         user: dict = Depends(require("tax:write"))):
    if body.kind not in ("return_payment", "prepayment"):
        raise HTTPException(422, "kind must be return_payment or prepayment")
    amount = to_cents(body.amount)
    pay_date = body.payment_date or today_str()
    pid = new_id()
    cash = P._cash_account(body.payment_method)
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": pay_date, "source": "sales_tax_payment",
        "source_ref": pid,
        "memo": (f"Sales tax {body.kind.replace('_', ' ')} — "
                 f"{body.state.upper()} {body.period_key}. "
                 f"{body.memo or ''}").strip(),
        "lines": [P.dr("2100", amount, "Sales tax liability settled"),
                  P.cr(cash, amount, "Paid via state portal")],
        "currency": "USD",
    }, status="posted")
    doc = {
        "id": pid, "tenant_id": user["tenant_id"],
        "state": body.state.upper(), "period_key": body.period_key,
        "kind": body.kind, "amount_cents": amount,
        "payment_date": pay_date, "payment_method": body.payment_method,
        "memo": (body.memo or "").strip(),
        "entry_number": entry["number"], "created_at": now_iso(),
        "note": "Recorded from the owner's own portal payment — "
                "this app did not file or remit anything.",
    }
    await _pay_coll().insert_one(doc)
    doc.pop("_id", None)
    return doc


# ------------------------------------------------------------------
# State registrations + nexus monitor (warnings, never determinations)
# ------------------------------------------------------------------
class RegistrationIn(BaseModel):
    state: str = Field(..., min_length=2, max_length=2)
    registration_id: Optional[str] = None
    registered_on: Optional[str] = None
    filing_frequency: str = "monthly"
    notes: Optional[str] = Field(default="", max_length=200)


class RegistrationPatch(BaseModel):
    registration_id: Optional[str] = None
    filing_frequency: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=200)
    active: Optional[bool] = None


@router.get("/registrations")
async def list_registrations(user: dict = Depends(require("tax:read"))):
    rows = await _reg_coll().find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0}
    ).sort("state", 1).to_list(200)
    return rows


@router.post("/registrations")
async def create_registration(body: RegistrationIn,
                              user: dict = Depends(require("tax:write"))):
    state = body.state.strip().upper()
    dupe = await _reg_coll().find_one({
        "tenant_id": user["tenant_id"], "state": state, "active": True})
    if dupe:
        raise HTTPException(409, f"Already registered in {state}")
    doc = {
        "id": new_id(), "tenant_id": user["tenant_id"], "state": state,
        "registration_id": body.registration_id,
        "registered_on": body.registered_on,
        "filing_frequency": body.filing_frequency,
        "notes": (body.notes or "").strip(), "active": True,
        "created_at": now_iso(),
    }
    await _reg_coll().insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.patch("/registrations/{rid}")
async def update_registration(rid: str, body: RegistrationPatch,
                              user: dict = Depends(require("tax:write"))):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(400, "Nothing to update")
    r = await _reg_coll().update_one(
        {"id": rid, "tenant_id": user["tenant_id"]}, {"$set": patch})
    if not r.matched_count:
        raise HTTPException(404, "Registration not found")
    return await _reg_coll().find_one({"id": rid}, {"_id": 0})


@router.get("/nexus")
async def nexus_monitor(user: dict = Depends(require("tax:read"))):
    """Per-state sales vs economic-nexus thresholds, current + prior
    calendar year (the standard measurement window). WARNINGS ONLY:
    nexus also turns on physical presence and facts this app cannot
    see — this never presents a determination as fact."""
    tenant_id = user["tenant_id"]
    today = date.today()
    start = f"{today.year - 1}-01-01"
    end = (today + timedelta(days=1)).isoformat()
    inv_rows, pos_rows, _revs, truncated = await _activity_rows(
        tenant_id, start, end)
    jur = await _jur_map(tenant_id)
    registered_states = {
        j.get("state") for j in jur.values() if j.get("active") and j.get("state")}
    for reg in await _reg_coll().find(
            {"tenant_id": tenant_id, "active": True}, {"_id": 0}).to_list(200):
        registered_states.add(reg.get("state"))

    per_state: dict = {}
    unattributed = 0
    for row in inv_rows + pos_rows:
        st = row.get("state")
        if not st:
            unattributed += row["gross_cents"]
            continue
        agg = per_state.setdefault(st, {
            "state": st, "direct_sales_cents": 0,
            "facilitated_sales_cents": 0, "transactions": 0,
            "taxable_sales_cents": 0})
        agg["facilitated_sales_cents"] += row["facilitated_sales_cents"]
        agg["direct_sales_cents"] += row["gross_cents"] - row["facilitated_sales_cents"]
        agg["taxable_sales_cents"] += row["taxable_cents"]
        agg["transactions"] += 1
    for st in registered_states:
        per_state.setdefault(st, {
            "state": st, "direct_sales_cents": 0, "facilitated_sales_cents": 0,
            "transactions": 0, "taxable_sales_cents": 0})

    states = []
    for st, agg in sorted(per_state.items()):
        cfg = STE.nexus_threshold_for(st)
        total = agg["direct_sales_cents"] + agg["facilitated_sales_cents"]
        threshold = cfg["sales_threshold_cents"]
        txn_threshold = cfg["transaction_threshold"]
        sales_ratio = (total / threshold) if threshold else 0.0
        crossed = total >= threshold
        if txn_threshold:
            txn_met = agg["transactions"] > txn_threshold
            crossed = (crossed and txn_met) if cfg["transaction_test_mode"] == "and" \
                else (crossed or txn_met)
        registered = st in registered_states
        if registered:
            status = "registered"
        elif crossed:
            status = "threshold_crossed"
        elif sales_ratio >= 0.8:
            status = "approaching"
        else:
            status = "below_threshold"
        states.append({
            **agg,
            "total_sales_cents": total,
            "registered": registered,
            "threshold": {
                "sales_threshold_cents": threshold,
                "transaction_threshold": txn_threshold,
                "transaction_test_mode": cfg["transaction_test_mode"],
                "basis": cfg["basis"],
            },
            "pct_of_threshold": round(sales_ratio * 100, 1),
            "status": status,
            "warning": (
                None if registered or status == "below_threshold" else
                ("Sales into {st} appear to have crossed the economic-nexus "
                 "threshold in the config below. This is a monitoring "
                 "warning, not a determination — nexus also depends on "
                 "physical presence and state-specific rules. Talk to "
                 "your CPA about registering before collecting.").format(st=st)
                if status == "threshold_crossed" else
                (f"Sales into {st} are at {round(sales_ratio * 100)}% of the "
                 "economic-nexus threshold in the config below — keep an "
                 "eye on it. Warning only, not a determination.")),
        })
    return {
        "window": {"start": start, "end": end,
                   "basis": "current + prior calendar year"},
        "states": states,
        "unattributed_sales_cents": unattributed,
        "threshold_config": {
            "version": STE.NEXUS_CONFIG_VERSION,
            "verified_as_of": STE.NEXUS_VERIFIED_AS_OF,
            "source": STE.NEXUS_SOURCE,
        },
        "truncated": truncated,
        "disclaimer": (
            "Monitoring aid only — not a nexus determination and not tax "
            "advice. Nexus can arise from physical presence (employees, "
            "contractors, inventory, traveling crews) at any sales level; "
            "registering and filing duties are yours to confirm with "
            "your CPA."),
    }
