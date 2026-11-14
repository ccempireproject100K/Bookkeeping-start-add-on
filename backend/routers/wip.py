"""WIP schedule endpoints (job-costing ask #5). The compute lives in
lib/wip.py; this router is registered BEFORE routers/jobs in server.py
so /jobs/wip* wins over /jobs/{job_id}.

Export PDF uses the house dependency-free PDF pattern (the same
_pdf_bytes shape as routers/invoice_delivery's dispute packet), copied
here so this lane carries no cross-branch dependency.
"""
import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from lib import wip as W
from lib.auth import require
from lib.base import cents_to_dollars

router = APIRouter(prefix="/jobs", tags=["jobs-wip"])

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _money(cents) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _pct(p) -> str:
    return "—" if p is None else f"{p}%"


# ---------------- live schedule ----------------

@router.get("/wip")
async def live_schedule(user: dict = Depends(require("ledger:read"))):
    """The live WIP schedule — computed on demand from current jobs,
    costs and invoices; always current, no rebuild step."""
    return await W.build_schedule(user["tenant_id"])


# ---------------- snapshots ----------------

class SnapshotBody(BaseModel):
    period: str                       # "YYYY-MM" — the month being closed
    label: Optional[str] = ""


@router.post("/wip/snapshots")
async def create_snapshot(body: SnapshotBody,
                          user: dict = Depends(require("ops:manage"))):
    if not PERIOD_RE.match(body.period or ""):
        raise HTTPException(status_code=422,
                            detail="period must be YYYY-MM")
    return await W.take_snapshot(user["tenant_id"], user, body.period,
                                 body.label or "")


@router.get("/wip/snapshots")
async def list_snapshots(user: dict = Depends(require("ledger:read"))):
    snaps = await W.snapshots_coll(user["tenant_id"]).find_list(
        sort=[("taken_at", -1)], limit=200)
    return [{"id": s["id"], "period": s.get("period"),
             "label": s.get("label"), "taken_at": s.get("taken_at"),
             "totals": s.get("totals")} for s in snaps]


@router.get("/wip/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: str,
                       user: dict = Depends(require("ledger:read"))):
    snap = await W.snapshots_coll(user["tenant_id"]).find_one(
        {"id": snapshot_id})
    if not snap:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    snap.pop("_id", None)
    return snap


# ---------------- month-over-month ----------------

async def _rows_for(tenant_id: str, ref: str) -> list:
    if ref == "live":
        sched = await W.build_schedule(tenant_id, with_flags=False)
        return sched["rows"]
    snap = await W.snapshots_coll(tenant_id).find_one({"id": ref})
    if not snap:
        raise HTTPException(status_code=404,
                            detail=f"Snapshot {ref} not found")
    return snap.get("rows", [])


@router.get("/wip/compare")
async def compare(base: str, against: str = "live",
                  user: dict = Depends(require("ledger:read"))):
    """base = snapshot id (the earlier month); against = snapshot id or
    'live'. Per-job deltas + the catch-up flag between the two."""
    tenant_id = user["tenant_id"]
    base_rows = await _rows_for(tenant_id, base)
    against_rows = await _rows_for(tenant_id, against)
    return {"base": base, "against": against,
            "rows": W.compare_schedules(base_rows, against_rows)}


# ---------------- export ----------------

def _schedule_lines(title: str, sched: dict) -> list:
    lines = [title, f"Generated: {sched.get('generated_at', '')}", ""]
    hdr = (f"{'Job':<10} {'Customer':<22} {'Contract':>12} {'% Done':>7} "
           f"{'Earned':>12} {'Billed':>12} {'Over/Under':>12}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in sched["rows"]:
        lines.append(
            f"{r['job_number']:<10} {r['customer_name'][:22]:<22} "
            f"{_money(r['contract_cents']):>12} {_pct(r['pct_complete']):>7} "
            f"{_money(r['earned_revenue_cents']):>12} "
            f"{_money(r['billed_to_date_cents']):>12} "
            f"{_money(r['over_under_cents']):>12}")
        lines.append(f"    {r['note']}")
        for p in r.get("phases", []):
            lines.append(
                f"    phase {p['name']:<18} budget "
                f"{_money(p['budget_cents'])}  % done "
                f"{_pct(p['pct_complete'])}  earned "
                f"{_money(p['earned_revenue_cents'])}")
    t = sched["totals"]
    lines.append("-" * len(hdr))
    lines.append(
        f"{'TOTAL':<10} {'':<22} {_money(t['contract_cents']):>12} "
        f"{_pct(t['pct_complete']):>7} "
        f"{_money(t['earned_revenue_cents']):>12} "
        f"{_money(t['billed_to_date_cents']):>12} "
        f"{_money(t['over_under_cents']):>12}")
    return lines


def _csv_text(sched: dict) -> str:
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["job_number", "customer", "status", "contract",
                "estimated_costs", "costs_to_date", "pct_complete",
                "earned_revenue", "billed_to_date", "over_under",
                "billing_behind", "note"])
    for r in sched["rows"]:
        w.writerow([r["job_number"], r["customer_name"], r["status"],
                    cents_to_dollars(r["contract_cents"]),
                    cents_to_dollars(r["estimated_costs_cents"]),
                    cents_to_dollars(r["costs_to_date_cents"]),
                    "" if r["pct_complete"] is None else r["pct_complete"],
                    "" if r["earned_revenue_cents"] is None
                    else cents_to_dollars(r["earned_revenue_cents"]),
                    cents_to_dollars(r["billed_to_date_cents"]),
                    "" if r["over_under_cents"] is None
                    else cents_to_dollars(r["over_under_cents"]),
                    bool(r.get("billing_behind")), r["note"]])
        for p in r.get("phases", []):
            w.writerow([r["job_number"], f"  phase: {p['name']}", "",
                        "" if p["contract_share_cents"] is None
                        else cents_to_dollars(p["contract_share_cents"]),
                        cents_to_dollars(p["budget_cents"]),
                        cents_to_dollars(p["costs_to_date_cents"]),
                        "" if p["pct_complete"] is None
                        else p["pct_complete"],
                        "" if p["earned_revenue_cents"] is None
                        else cents_to_dollars(p["earned_revenue_cents"]),
                        "", "", "", ""])
    t = sched["totals"]
    w.writerow(["TOTAL", "", "", cents_to_dollars(t["contract_cents"]),
                cents_to_dollars(t["estimated_costs_cents"]),
                cents_to_dollars(t["costs_to_date_cents"]),
                "" if t["pct_complete"] is None else t["pct_complete"],
                cents_to_dollars(t["earned_revenue_cents"]),
                cents_to_dollars(t["billed_to_date_cents"]),
                cents_to_dollars(t["over_under_cents"]),
                t["flagged"], ""])
    return buf.getvalue()


def _pdf_bytes(title: str, lines: list) -> bytes:
    """Minimal dependency-free PDF — the house pattern (copied from
    routers/invoice_delivery._pdf_bytes, dispute packet #11)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(") \
                .replace(")", "\\)")[:110]
    pages, chunk = [], []
    for ln in lines:
        chunk.append(ln)
        if len(chunk) >= 46:
            pages.append(chunk); chunk = []
    if chunk or not pages:
        pages.append(chunk)
    n = 3 + len(pages) * 2
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]

    def w(data: bytes):
        offsets.append(buf.tell()); buf.write(data)
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(pages)))
    w(b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n")
    w(f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {len(pages)}>>endobj\n"
      .encode())
    w(b"3 0 obj<</Type/Font/Subtype/Type1/BaseFont/Courier>>endobj\n")
    for i, pg in enumerate(pages):
        text = "BT /F1 8 Tf 40 770 Td 13 TL " + " ".join(
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


@router.get("/wip/export")
async def export(format: str = "csv", snapshot_id: Optional[str] = None,
                 user: dict = Depends(require("ledger:read"))):
    tenant_id = user["tenant_id"]
    if snapshot_id:
        snap = await W.snapshots_coll(tenant_id).find_one(
            {"id": snapshot_id})
        if not snap:
            raise HTTPException(status_code=404,
                                detail="Snapshot not found")
        sched = {"rows": snap.get("rows", []),
                 "totals": snap.get("totals", {}),
                 "generated_at": snap.get("taken_at", "")}
        stem = f"wip-{snap.get('period', 'snapshot')}"
        title = f"WIP SCHEDULE — {snap.get('label', stem)}"
    else:
        sched = await W.build_schedule(tenant_id)
        stem = "wip-live"
        title = "WIP SCHEDULE — LIVE"
    if format == "csv":
        return Response(content=_csv_text(sched),
                        media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{stem}.csv"'})
    if format == "pdf":
        return Response(content=_pdf_bytes(title, _schedule_lines(title,
                                                                 sched)),
                        media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{stem}.pdf"'})
    raise HTTPException(status_code=422, detail="format must be csv or pdf")
