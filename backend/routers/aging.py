"""AR & AP aging reports (0-30 / 31-60 / 61-90 / 90+) with CSV export."""
from fastapi import APIRouter, Depends, Response
from datetime import date

from db import db
from lib.base import Scoped, cents_to_dollars, today_str
from lib.auth import require

router = APIRouter(prefix="/aging", tags=["aging"])

BUCKETS = ["0-30", "31-60", "61-90", "90+"]


def _bucket(ref_date: str, as_of: str) -> str:
    try:
        days = (date.fromisoformat(as_of) - date.fromisoformat(ref_date)).days
    except Exception:
        days = 0
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def _summarize(items):
    totals = {b: 0 for b in BUCKETS}
    for it in items:
        totals[it["bucket"]] += it["amount_cents"]
    return {
        "items": items,
        "buckets": {b: cents_to_dollars(totals[b]) for b in BUCKETS},
        "total": cents_to_dollars(sum(totals.values())),
        "total_cents": sum(totals.values()),
    }


async def _ar(tenant_id, as_of):
    invoices = await Scoped(db.invoices, tenant_id).find_list({"status": "issued"})
    out = []
    for inv in invoices:
        ref = inv.get("due_date") or (inv.get("created_at") or "")[:10] or as_of
        out.append({
            "name": inv["client_name"], "ref": inv.get("issue_entry"),
            "due_date": ref, "amount_cents": inv["total_cents"],
            "amount": cents_to_dollars(inv["total_cents"]), "bucket": _bucket(ref, as_of),
        })
    return _summarize(out)


async def _ap(tenant_id, as_of):
    bills = await Scoped(db.bills, tenant_id).find_list({"status": "open"})
    out = []
    for b in bills:
        ref = b.get("due_date") or b.get("bill_date") or as_of
        out.append({
            "name": b["vendor"], "ref": b.get("issue_entry"),
            "due_date": ref, "amount_cents": b["amount_cents"],
            "amount": cents_to_dollars(b["amount_cents"]), "bucket": _bucket(ref, as_of),
        })
    return _summarize(out)


@router.get("/ar")
async def ar(as_of: str = None, user: dict = Depends(require("ledger:read"))):
    return await _ar(user["tenant_id"], as_of or today_str())


@router.get("/ap")
async def ap(as_of: str = None, user: dict = Depends(require("ledger:read"))):
    return await _ap(user["tenant_id"], as_of or today_str())


def _csv(title, data):
    rows = [f"{title} Aging", "Name,Reference,Due Date,Bucket,Amount"]
    for it in data["items"]:
        rows.append(f"\"{it['name']}\",{it['ref'] or ''},{it['due_date']},{it['bucket']},{it['amount']:.2f}")
    rows.append("")
    rows.append("Bucket,Total")
    for b, v in data["buckets"].items():
        rows.append(f"{b},{v:.2f}")
    rows.append(f"TOTAL,{data['total']:.2f}")
    return "\n".join(rows)


@router.get("/ar.csv")
async def ar_csv(as_of: str = None, user: dict = Depends(require("ledger:read"))):
    data = await _ar(user["tenant_id"], as_of or today_str())
    return Response(content=_csv("Accounts Receivable", data), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ar_aging.csv"})


@router.get("/ap.csv")
async def ap_csv(as_of: str = None, user: dict = Depends(require("ledger:read"))):
    data = await _ap(user["tenant_id"], as_of or today_str())
    return Response(content=_csv("Accounts Payable", data), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ap_aging.csv"})
