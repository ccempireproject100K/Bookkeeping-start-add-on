"""Welcome gift: two free, keep-forever assets inside onboarding.

Owner-approved 2026-10-31: a new joiner's onboarding hands them two
tangible assets, free, theirs to keep —

* Gift A, "Clean your first file, free": the tenant uploads a messy
  customer / vendor / item file and takes back the cleaned file plus a
  duplicate report. This runs the SAME engine path as the DataClean
  Studio (lib/dataclean cleaning + linkage, via the importer mapping
  the keyed API uses) — but session-authenticated, so a brand-new
  tenant needs no DataClean API key and no paid plan.
* Gift B, "Your first quarterly estimate, in writing": a short form
  (net profit so far, filing status, state, W-2 wages) runs the merged
  tax engine (lib/taxengine.py — the math is never reimplemented here)
  and stores a downloadable estimate packet: totals, per-type
  breakdown, quarterly schedule, workpapers trace.

Honesty rules (hard, mirroring the engine's own):
* A jurisdiction the engine reports NOT_IMPLEMENTED stays visibly
  NOT_IMPLEMENTED — its note ships verbatim in the response and the
  packet, and the missing part is never presented as $0.
* A state with no engine module at all gets the federal computation
  only, labelled PARTIAL with the same explicit exclusion note, so the
  packet never silently pretends to be a full state estimate.
* When the engine is not built in an environment, the step answers
  503 with code "engine_not_built": an honest unavailable state,
  never a crash and never a fabricated zero. No delivery is recorded.

Deliveries are recorded per tenant (metadata for the clean — the
caller's rows themselves are never stored, the Studio rule; the full
engine result for the estimate, like db.tax_estimates stores) and
logged to the audit trail. The onboarding checklist reads the
deliveries, so a gift step completes when the asset is in the
tenant's hands and stays re-runnable afterwards.
"""
import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from db import db
from lib.audit import append_audit
from lib.auth import get_current_user, require
from lib.base import Scoped, new_id, now_iso
from lib.dataclean import RESULT_SHAPE
from lib.dataclean import cleaning as _cleaning
from lib.dataclean import linkage as _linkage
from lib.taxengine import TaxEngineUnavailable, run_tax_estimate
from routers.dataclean_api import MAX_DEDUPE_RECORDS, MAX_ROWS, _mapped_rows, _err
from routers.importer import FIELDS

router = APIRouter(prefix="/welcome-gift", tags=["welcome-gift"])

# The tax engine's state registry (tax-engine/src/registry.ts). The
# engine result stays the runtime authority — this list only drives
# the form and the federal-only fallback for states with no module.
ENGINE_STATES = ["CA", "NY", "TX", "FL", "WA", "NV", "SD", "WY", "AK",
                 "TN", "NH", "PA", "IL", "NC", "GA", "AZ"]
STUB_STATES = {"PA", "IL", "NC", "GA", "AZ"}  # engine: NOT_IMPLEMENTED
FILING_STATUSES = ["single", "married_filing_jointly",
                   "married_filing_separately", "head_of_household"]

GIFTS = ("clean", "estimate")


def _deliveries(user):
    return Scoped(db.welcome_gift_deliveries, user["tenant_id"])


async def gift_state(user) -> dict:
    """Per-gift completion, read by the onboarding checklist too."""
    out = {}
    try:
        docs = await _deliveries(user).find_list({}, limit=100000)
    except Exception:
        docs = []
    for gift in GIFTS:
        mine = [d for d in docs if d.get("gift") == gift]
        last = max((d.get("delivered_at") or "" for d in mine), default=None)
        out[gift] = {"done": bool(mine), "deliveries": len(mine),
                     "last_delivered_at": last or None}
    return out


@router.get("")
async def get_welcome_gift(user: dict = Depends(get_current_user)):
    state = await gift_state(user)
    return {"gifts": state,
            "kinds": sorted(FIELDS),
            "engine_states": ENGINE_STATES,
            "stub_states": sorted(STUB_STATES),
            "filing_statuses": FILING_STATUSES}


async def _record_delivery(user, doc: dict) -> dict:
    doc = {"id": new_id(), "delivered_at": now_iso(),
           "delivered_by": user.get("email"), **doc}
    await _deliveries(user).insert_one(doc)
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor": user.get("id", ""), "actor_email": user.get("email", ""),
        "action": f"welcome_gift.{doc['gift']}_delivered",
        "entity": "welcome_gift", "entity_id": doc["id"],
        "details": {k: doc.get(k) for k in
                    ("gift", "kind", "rows", "changes", "duplicate_pairs",
                     "status", "requested_jurisdiction",
                     "computed_jurisdiction", "total_liability_cents")},
        "created_at": now_iso(),
    })
    doc.pop("_id", None)
    doc.pop("tenant_id", None)
    return doc


# ------------------------------------------------------------ gift A: clean

class GiftCleanIn(BaseModel):
    kind: str
    csv_text: str


@router.post("/clean")
async def gift_clean(body: GiftCleanIn,
                     user: dict = Depends(require("ops:manage"))):
    """Clean one uploaded file with the Studio's engine path and hand
    it back: cleaned records + every change + the duplicate report.
    Free for every tenant — no DataClean key, no plan gate. Only the
    delivery metadata is stored; the rows live in this response."""
    mapped, _mapping = _mapped_rows(body.kind, None, body.csv_text, None)
    if len(mapped) > MAX_ROWS:
        raise _err(413, "payload_too_large",
                   f"at most {MAX_ROWS} rows per file", max_rows=MAX_ROWS)
    records, changes = [], []
    for idx, d in enumerate(mapped):
        cleaned, ch = _cleaning.clean_row(d, body.kind)
        records.append(cleaned)
        changes += [{"row": idx + 2, **c} for c in ch]
    by_rule = {}
    for c in changes:
        by_rule[c["rule"]] = by_rule.get(c["rule"], 0) + 1

    # Duplicate scan over the cleaned records, projected onto the
    # kind's flat fields — the same projection the Studio sends, so
    # engine helper keys (name_key, address_parts) never reach the
    # linkage features (the PR #176 crash class).
    fields = FIELDS[body.kind]
    batch = []
    for i, r in enumerate(records[:MAX_DEDUPE_RECORDS]):
        e = {}
        for f in fields:
            v = r.get(f)
            if v is not None and v != "":
                e[f] = str(v).strip()
        e["id"] = str(r.get("id") or f"row-{i + 1}")
        batch.append(e)
    scan = _linkage.suggest_duplicates(batch)

    delivery = await _record_delivery(user, {
        "gift": "clean", "kind": body.kind, "rows": len(mapped),
        "changes": len(changes), "duplicate_pairs": len(scan["pairs"]),
    })
    return {"shape": RESULT_SHAPE, "gift": "clean", "kind": body.kind,
            "rows": len(mapped), "records": records, "changes": changes,
            "changes_by_rule": by_rule,
            "dedupe": {"engine": scan["engine"],
                       "engine_note": scan.get("engine_note"),
                       "pairs": scan["pairs"],
                       "scanned": len(batch),
                       "truncated": len(records) > MAX_DEDUPE_RECORDS},
            "delivery": delivery}


# --------------------------------------------------------- gift B: estimate

class GiftEstimateIn(BaseModel):
    net_profit: float                       # dollars, year-to-date so far
    filing_status: str = "single"
    state: Optional[str] = None             # 2-letter, or None = federal only
    w2_wages: Optional[float] = None
    w2_social_security_wages: Optional[float] = None
    payments_already_made: Optional[float] = None


def _resolve_jurisdiction(state: Optional[str]):
    """(computed_jurisdiction, fallback_note) for a requested state.

    Registry states run as US-XX (the engine itself reports stub
    states as PARTIAL/NOT_IMPLEMENTED). A state with no engine module
    falls back to the federal computation, explicitly labelled — the
    same exclusion wording discipline the engine uses for its stubs.
    """
    if not state:
        return "US", None
    code = state.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise _err(422, "invalid_state", "state must be a 2-letter code")
    if code in ENGINE_STATES:
        return f"US-{code}", None
    note = (f"State tax for US-{code} is NOT_IMPLEMENTED in the estimate "
            f"engine and is EXCLUDED from the total below — the total is "
            f"federal only. A missing module is never reported as a $0 "
            f"state liability.")
    return "US", note


@router.post("/estimate")
async def gift_estimate(body: GiftEstimateIn,
                        user: dict = Depends(require("ops:manage"))):
    """Run the merged tax engine on the joiner's own figures and store
    the packet. Estimates only — no filing, ever."""
    if body.filing_status not in FILING_STATUSES:
        raise _err(422, "invalid_filing_status",
                   f"filing_status must be one of {FILING_STATUSES}")
    computed, fallback_note = _resolve_jurisdiction(body.state)
    requested = f"US-{body.state.strip().upper()}" if body.state else "US"
    profile = {"filingStatus": body.filing_status}
    for src, dst in (("w2_wages", "w2Wages"),
                     ("w2_social_security_wages", "w2SocialSecurityWages"),
                     ("payments_already_made", "paymentsAlreadyMade")):
        v = getattr(body, src)
        if v is not None:
            profile[dst] = v
    net_cents = int(round(body.net_profit * 100))
    try:
        result = await run_tax_estimate(net_cents, computed, profile)
    except TaxEngineUnavailable as e:
        msg = str(e)
        code = "engine_not_built" if "engine_not_built" in msg else \
            "engine_unavailable"
        raise _err(503, code,
                   f"The tax estimate engine is unavailable right now: {msg}")
    if fallback_note:
        # Federal-only fallback: the engine computed 'US'; present the
        # result as PARTIAL with the exclusion note up front, exactly
        # like the engine presents its own stub states.
        result = {**result, "status": "PARTIAL",
                  "notes": [fallback_note, *(result.get("notes") or [])]}
    delivery = await _record_delivery(user, {
        "gift": "estimate",
        "inputs": {"net_profit_cents": net_cents,
                   "filing_status": body.filing_status,
                   "state": body.state.strip().upper() if body.state else None,
                   "w2_wages": body.w2_wages,
                   "w2_social_security_wages": body.w2_social_security_wages,
                   "payments_already_made": body.payments_already_made},
        "requested_jurisdiction": requested,
        "computed_jurisdiction": computed,
        "status": result.get("status"),
        "total_liability_cents": result.get("totalLiabilityCents"),
        "result": result,
    })
    return {"gift": "estimate", "delivery_id": delivery["id"],
            "requested_jurisdiction": requested,
            "computed_jurisdiction": computed,
            "status": result.get("status"),
            "total_liability_cents": result.get("totalLiabilityCents"),
            "result": result, "delivery": delivery}


# ------------------------------------------------------- estimate packet

def _money(cents) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def _packet_rows(delivery: dict):
    """(title, [(section, [lines])]) — the packet's full text content,
    shared by the PDF and CSV renderers so they never drift."""
    r = delivery.get("result") or {}
    inp = delivery.get("inputs") or {}
    tenant_name = delivery.get("tenant_name") or ""
    sections = []
    head = [
        f"Business: {tenant_name}" if tenant_name else None,
        f"Estimate year: {r.get('taxYear')}   Generated: {delivery.get('delivered_at')}",
        f"Net profit so far: {_money(inp.get('net_profit_cents'))}"
        f"   Filing status: {inp.get('filing_status')}",
        f"Jurisdiction requested: {delivery.get('requested_jurisdiction')}"
        f"   Computed: {delivery.get('computed_jurisdiction')}",
        "Estimate only — this packet is not a tax filing.",
    ]
    sections.append(("FIRST QUARTERLY ESTIMATE — WELCOME GIFT",
                     [h for h in head if h]))
    status = r.get("status")
    if status and status != "OK":
        note_lines = [f"Status: {status}"]
        note_lines += [f"  {n}" for n in (r.get("notes") or [])]
        sections.append(("READ FIRST — WHAT THIS PACKET DOES NOT INCLUDE",
                         note_lines))
    total_label = "Total estimated liability"
    if status == "PARTIAL":
        total_label = "Total estimated liability (implemented parts only)"
    sections.append(("TOTAL", [f"{total_label}: "
                               f"{_money(r.get('totalLiabilityCents'))}"]))
    breakdown = [f"  {c.get('label')} ({c.get('jurisdiction')}): "
                 f"{_money(c.get('amountCents'))}"
                 for c in (r.get("breakdown") or [])]
    sections.append(("BREAKDOWN BY TAX TYPE", breakdown or ["  (none)"]))
    schedule = [f"  Q{p.get('quarter')}  due {p.get('dueDate')}: "
                f"{_money(p.get('amountDueCents'))}"
                for p in (r.get("schedule") or [])]
    sections.append(("QUARTERLY PAYMENT SCHEDULE",
                     schedule or ["  (no payments scheduled)"]))
    sh = (r.get("safeHarbor") or {}).get("federal") or {}
    if sh:
        sections.append(("SAFE HARBOR (FEDERAL)", [
            f"  Required annual payment: "
            f"{_money(sh.get('requiredAnnualPaymentCents'))}"
            f"   Basis: {sh.get('basis')}"]))
    wp_lines = []
    for wp in (r.get("workpapers") or []):
        wp_lines.append(f"  {wp.get('jurisdiction')} — config "
                        f"{wp.get('configVersion')} (hash "
                        f"{str(wp.get('configHash'))[:12]}…)"
                        + ("  PROVISIONAL" if wp.get("provisional") else ""))
        for step in (wp.get("steps") or []):
            amt = step.get("amountCents")
            wp_lines.append(f"    - {step.get('label')}: "
                            f"{step.get('detail')}"
                            + (f" [{_money(amt)}]" if amt is not None else ""))
    sections.append(("WORKPAPERS — HOW THESE NUMBERS WERE COMPUTED",
                     wp_lines or ["  (none)"]))
    if status == "OK" and (r.get("notes") or []):
        sections.append(("NOTES", [f"  {n}" for n in r["notes"]]))
    return sections


def _packet_csv(delivery: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for title, lines in _packet_rows(delivery):
        w.writerow([title])
        for line in lines:
            w.writerow([line.strip()])
        w.writerow([])
    return buf.getvalue()


def _packet_pdf(delivery: dict) -> bytes:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    y = 740

    def line(text, dy=15, size=9.5):
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(54, y, str(text)[:118])
        y -= dy
        if y < 60:
            c.showPage()
            y = 740

    for i, (title, lines) in enumerate(_packet_rows(delivery)):
        line(title, 20, 13 if i == 0 else 11.5)
        for text in lines:
            line(text)
        line("", 8)
    c.save()
    return buf.getvalue()


@router.get("/estimate/{delivery_id}/download")
async def download_estimate_packet(delivery_id: str, format: str = "pdf",
                                   user: dict = Depends(require("ledger:read"))):
    doc = await _deliveries(user).find_one({"id": delivery_id,
                                            "gift": "estimate"})
    if not doc:
        raise HTTPException(status_code=404, detail="Estimate not found")
    tenant = await db.tenants.find_one({"id": user["tenant_id"]},
                                       {"_id": 0}) or {}
    doc = {**doc, "tenant_name": tenant.get("name")}
    if format == "csv":
        return Response(content=_packet_csv(doc), media_type="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=first-estimate.csv"})
    if format == "pdf":
        return Response(content=_packet_pdf(doc), media_type="application/pdf",
                        headers={"Content-Disposition":
                                 "attachment; filename=first-estimate.pdf"})
    raise HTTPException(status_code=422, detail="format must be pdf or csv")
