"""Year-end tax packet (standout extra B): one assembly per tenant + year.

What exists today are separate pieces — the P&L and balance sheet reports
and, in PR #147 (NOT merged), the tax engine's estimate workpapers. This
router assembles what is ON MAIN into a single client-ready packet:
profit & loss, balance sheet, trial balance summary, and a tax section.

TAX SECTION: PR #147 MERGED during this build (main 388fbbb7), so the
section activates for real: POST /tax-packet/{year}/tax-estimate runs the
merged TypeScript engine (via lib/taxengine.py's Node bridge — the tax
math is never reimplemented in Python) on the packet's net profit plus
the owner's tax profile, stores the full TaxResult (breakdown, quarterly
schedule, workpapers) in db.tax_estimates, and the packet includes it.
Before an estimate is computed, the section carries the engine's inputs
with status "awaiting_tax_engine" so the packet is still complete.
"""
import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from db import db
from lib import ledger as L
from lib.base import Scoped, now_iso
from lib.auth import require

router = APIRouter(prefix="/tax-packet", tags=["tax-packet"])


async def build_packet(user, year: int) -> dict:
    tid = user["tenant_id"]
    tenant = await db.tenants.find_one({"id": tid}, {"_id": 0}) or {}
    jan1 = f"{year}-01-01"
    dec31 = f"{year}-12-31"

    year_bals = await L._balances(db, tid, as_of=dec31, date_from=jan1)
    cum_bals = await L._balances(db, tid, as_of=dec31)

    def rows(bals, types):
        out = []
        for code in sorted(bals):
            t = L.ACCOUNT_TYPE_OF.get(code, "")
            if t not in types:
                continue
            v = bals[code]
            net = L._net_balance(code, v["debit"], v["credit"])
            if net:
                out.append({"code": code, "name": L.NAME_OF.get(code, code),
                            "type": t, "amount_cents": net})
        return out

    pl = rows(year_bals, ("revenue", "expense", "cogs", "other_income", "other_expense"))
    bs = rows(cum_bals, ("asset", "liability", "equity"))
    revenue = sum(r["amount_cents"] for r in pl if r["type"] == "revenue")
    expenses = sum(r["amount_cents"] for r in pl
                   if r["type"] in ("expense", "cogs", "other_expense"))
    other_inc = sum(r["amount_cents"] for r in pl if r["type"] == "other_income")
    net_profit = revenue + other_inc - expenses
    tb = await L.trial_balance(db, tid, dec31)

    # Tax section: merged estimate doc if one exists, else the interface.
    estimate = None
    try:
        estimate = await Scoped(db.tax_estimates, tid).find_one({"year": year})
    except Exception:
        estimate = None
    if estimate:
        tax_section = {"status": "included", "source": "tax_estimates",
                       "estimate": {k: v for k, v in estimate.items()
                                    if k not in ("_id", "tenant_id")}}
    else:
        tax_section = {
            "status": "awaiting_tax_engine",
            "note": ("No tax estimate has been computed for this year yet. "
                     "POST /tax-packet/{year}/tax-estimate with the owner's "
                     "tax profile runs the merged tax engine and this "
                     "section fills in with its workpapers."),
            "engine_inputs": {
                "year": year,
                "net_profit_cents": net_profit,
                "entity_name": tenant.get("name"),
                "niche_id": tenant.get("niche_id"),
            },
        }

    return {
        "packet": "year_end_tax_packet",
        "year": year,
        "tenant": {"id": tid, "name": tenant.get("name"),
                   "niche_id": tenant.get("niche_id")},
        "generated_at": now_iso(),
        "profit_and_loss": {"rows": pl, "revenue_cents": revenue,
                            "expenses_cents": expenses,
                            "net_profit_cents": net_profit},
        "balance_sheet": {"as_of": dec31, "rows": bs},
        "trial_balance": {"debit_cents": tb["total_debit"],
                          "credit_cents": tb["total_credit"],
                          "balanced": tb["balanced"]},
        "tax": tax_section,
    }


@router.get("/{year}")
async def get_packet(year: int, user: dict = Depends(require("ledger:read"))):
    if year < 2000 or year > 2100:
        raise HTTPException(status_code=422, detail="Year out of range")
    return await build_packet(user, year)


def _packet_csv(packet: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Year-end tax packet", packet["tenant"].get("name") or "", packet["year"]])
    w.writerow(["generated_at", packet["generated_at"]])
    w.writerow([])
    w.writerow(["PROFIT AND LOSS"])
    w.writerow(["code", "name", "type", "amount"])
    for r in packet["profit_and_loss"]["rows"]:
        w.writerow([r["code"], r["name"], r["type"], f"{r['amount_cents'] / 100:.2f}"])
    w.writerow(["", "Net profit", "", f"{packet['profit_and_loss']['net_profit_cents'] / 100:.2f}"])
    w.writerow([])
    w.writerow(["BALANCE SHEET", packet["balance_sheet"]["as_of"]])
    w.writerow(["code", "name", "type", "amount"])
    for r in packet["balance_sheet"]["rows"]:
        w.writerow([r["code"], r["name"], r["type"], f"{r['amount_cents'] / 100:.2f}"])
    w.writerow([])
    w.writerow(["TAX SECTION", packet["tax"]["status"]])
    if packet["tax"]["status"] == "included":
        est = packet["tax"]["estimate"].get("result") or {}
        w.writerow(["total_liability", f"{(est.get('totalLiabilityCents') or 0) / 100:.2f}"])
        for comp in est.get("breakdown", []):
            w.writerow(["component", comp.get("label"), comp.get("type"),
                        f"{(comp.get('amountCents') or 0) / 100:.2f}"])
        for pay in est.get("schedule", []):
            w.writerow(["quarterly_payment", pay.get("dueDate"),
                        f"{(pay.get('amountDueCents') or 0) / 100:.2f}"])
    if packet["tax"]["status"] == "awaiting_tax_engine":
        w.writerow(["note", packet["tax"]["note"]])
        w.writerow(["engine_input_net_profit_cents",
                    packet["tax"]["engine_inputs"]["net_profit_cents"]])
    return buf.getvalue()


def _packet_pdf(packet: dict) -> bytes:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    y = 740

    def line(text, dy=16, size=10):
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(54, y, str(text)[:110])
        y -= dy
        if y < 60:
            c.showPage()
            y = 740

    def money(cents):
        return f"${cents / 100:,.2f}"

    line(f"Year-End Tax Packet — {packet['tenant'].get('name') or ''}", 22, 14)
    line(f"Year: {packet['year']}   Generated: {packet['generated_at']}", 24)
    line("PROFIT AND LOSS", 20, 12)
    for r in packet["profit_and_loss"]["rows"]:
        line(f"  {r['code']}  {r['name']}  {money(r['amount_cents'])}")
    line(f"  Net profit: {money(packet['profit_and_loss']['net_profit_cents'])}", 24)
    line(f"BALANCE SHEET (as of {packet['balance_sheet']['as_of']})", 20, 12)
    for r in packet["balance_sheet"]["rows"]:
        line(f"  {r['code']}  {r['name']}  {money(r['amount_cents'])}")
    line(f"Trial balance balanced: {packet['trial_balance']['balanced']}", 24)
    line("TAX ESTIMATES", 20, 12)
    tax = packet["tax"]
    if tax["status"] == "awaiting_tax_engine":
        line("  No tax estimate computed yet for this year (see JSON packet).")
        line(f"  Engine input — net profit: {money(tax['engine_inputs']['net_profit_cents'])}")
    else:
        est = tax["estimate"].get("result") or {}
        line(f"  Total estimated liability: {money(est.get('totalLiabilityCents') or 0)}"
             f"  (status {est.get('status')})")
        for comp in est.get("breakdown", []):
            line(f"    {comp.get('label')}: {money(comp.get('amountCents') or 0)}")
        for pay in est.get("schedule", []):
            line(f"    Due {pay.get('dueDate')}: {money(pay.get('amountDueCents') or 0)}")
    c.save()
    return buf.getvalue()


@router.get("/{year}/download")
async def download_packet(year: int, format: str = "pdf",
                          user: dict = Depends(require("ledger:read"))):
    if year < 2000 or year > 2100:
        raise HTTPException(status_code=422, detail="Year out of range")
    packet = await build_packet(user, year)
    if format == "csv":
        return Response(content=_packet_csv(packet), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f"attachment; filename=tax-packet-{year}.csv"})
    if format == "pdf":
        return Response(content=_packet_pdf(packet), media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f"attachment; filename=tax-packet-{year}.pdf"})
    raise HTTPException(status_code=422, detail="format must be pdf or csv")


# ---------------- tax estimate via the merged engine ----------------
from pydantic import BaseModel
from typing import Optional
from lib.taxengine import run_tax_estimate, TaxEngineUnavailable
from lib.base import new_id as _new_id


class TaxEstimateIn(BaseModel):
    jurisdiction: str = "US"            # engine keys: US, US-CA, US-TX, ...
    filing_status: str = "single"       # engine FilingStatus values
    w2_wages: Optional[float] = None
    w2_social_security_wages: Optional[float] = None
    prior_year_tax: Optional[float] = None
    prior_year_agi: Optional[float] = None
    payments_already_made: Optional[float] = None


@router.post("/{year}/tax-estimate")
async def compute_tax_estimate(year: int, body: TaxEstimateIn,
                               user: dict = Depends(require("ops:manage"))):
    """Run the merged tax engine on this packet's net profit and store the
    workpapers into the packet. Estimates only — no filing, ever."""
    if year < 2000 or year > 2100:
        raise HTTPException(status_code=422, detail="Year out of range")
    packet = await build_packet(user, year)
    net_cents = packet["profit_and_loss"]["net_profit_cents"]
    profile = {"filingStatus": body.filing_status}
    for src, dst in (("w2_wages", "w2Wages"),
                     ("w2_social_security_wages", "w2SocialSecurityWages"),
                     ("prior_year_tax", "priorYearTax"),
                     ("prior_year_agi", "priorYearAGI"),
                     ("payments_already_made", "paymentsAlreadyMade")):
        v = getattr(body, src)
        if v is not None:
            profile[dst] = v
    try:
        result = await run_tax_estimate(net_cents, body.jurisdiction, profile)
    except TaxEngineUnavailable as e:
        raise HTTPException(status_code=503, detail=f"Tax engine unavailable: {e}")
    doc = {"id": _new_id(), "year": year, "jurisdiction": body.jurisdiction,
           "profile": profile, "net_profit_cents": net_cents,
           "result": result, "computed_at": now_iso(),
           "computed_by": user.get("email")}
    coll = Scoped(db.tax_estimates, user["tenant_id"])
    old = await coll.find_one({"year": year})
    if old:
        await coll.update_one({"id": old["id"]}, {"$set": doc})
        doc["id"] = old["id"]
    else:
        await coll.insert_one(doc)
    doc.pop("_id", None)
    doc.pop("tenant_id", None)
    return {"year": year, "estimate": doc,
            "total_liability_cents": result.get("totalLiabilityCents"),
            "status": result.get("status")}
