"""Purchase orders: the missing procurement leg.

PO lifecycle: draft -> approved -> partially_received -> received -> billed
(cancelled any time before billed).

- Receiving posts DR 1300 (Inventory) / CR 2020 (Goods Received Not Invoiced)
  and updates product quantities with moving-average cost.
- Converting to bill clears GRNI: DR 2020 / CR 2010 (Accounts Payable)
  via the standard bill flow.
"""
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib import ledger as L
from lib import posting as P
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars

router = APIRouter(prefix="/purchase-orders", tags=["purchase-orders"])

GRNI_CODE = "2020"  # Goods Received Not Invoiced
INV_CODE = "1300"  # Inventory — Retail

STATUSES = ["draft", "approved", "partially_received", "received", "billed", "cancelled"]


def _pos(user):
    return Scoped(db.purchase_orders, user["tenant_id"])


async def _ensure_grni(user):
    """Create the GRNI accrual account for this tenant if missing."""
    acc = await Scoped(db.accounts, user["tenant_id"]).find_one({"code": GRNI_CODE})
    if not acc:
        await Scoped(db.accounts, user["tenant_id"]).insert_one({
            "id": new_id(), "code": GRNI_CODE,
            "name": "Goods Received Not Invoiced", "type": "liability",
            "normal_balance": "credit", "parent_code": None,
            "is_system": True, "archived": False, "created_at": now_iso(),
        })


def _totals(lines):
    return sum(l["quantity"] * l["unit_cost_cents"] for l in lines)


def _enrich(po):
    po = dict(po)
    po.pop("_id", None)
    po["total_cents"] = _totals(po.get("lines", []))
    po["received_cents"] = sum(l.get("received_qty", 0) * l["unit_cost_cents"]
                               for l in po.get("lines", []))
    return po


class POLineIn(BaseModel):
    product_id: str
    quantity: int
    unit_cost: float


class POCreate(BaseModel):
    vendor: str
    vendor_id: Optional[str] = None
    expected_date: Optional[str] = None
    notes: str = ""
    lines: List[POLineIn]


@router.get("")
async def list_pos(status: str = "", user: dict = Depends(require("ledger:read"))):
    q = {"status": status} if status in STATUSES else {}
    rows = await _pos(user).find_list(q, sort=[("created_at", -1)], limit=500)
    return [_enrich(r) for r in rows]


@router.post("")
async def create_po(body: POCreate, user: dict = Depends(require("ops:manage"))):
    if not body.vendor.strip():
        raise HTTPException(422, "Vendor is required")
    if not body.lines:
        raise HTTPException(422, "Add at least one line")
    lines = []
    for ln in body.lines:
        if ln.quantity <= 0 or ln.unit_cost < 0:
            raise HTTPException(422, "Quantities must be positive")
        prod = await Scoped(db.products, user["tenant_id"]).find_one({"id": ln.product_id})
        if not prod:
            raise HTTPException(404, f"Product not found: {ln.product_id}")
        lines.append({"product_id": ln.product_id, "product_name": prod.get("name"),
                      "quantity": ln.quantity, "received_qty": 0,
                      "unit_cost_cents": to_cents(ln.unit_cost)})
    doc = {"id": new_id(), "number": f"PO-{(await _pos(user).count_documents({})) + 1:04d}",
           "vendor": body.vendor.strip(), "vendor_id": body.vendor_id,
           "expected_date": body.expected_date, "notes": body.notes.strip(),
           "lines": lines, "status": "draft", "bill_id": None,
           "created_at": now_iso()}
    await _pos(user).insert_one(doc)
    await audit(user, "po.create", "purchase_order", doc["id"],
                {"vendor": doc["vendor"], "total_cents": _totals(lines)})
    return _enrich(doc)


@router.get("/{po_id}")
async def get_po(po_id: str, user: dict = Depends(require("ledger:read"))):
    po = await _pos(user).find_one({"id": po_id})
    if not po:
        raise HTTPException(404, "Purchase order not found")
    return _enrich(po)


@router.post("/{po_id}/approve")
async def approve_po(po_id: str, user: dict = Depends(require("ops:manage"))):
    po = await _pos(user).find_one({"id": po_id})
    if not po:
        raise HTTPException(404, "Purchase order not found")
    if po["status"] != "draft":
        raise HTTPException(409, "Only draft POs can be approved")
    await _pos(user).update_one({"id": po_id}, {"$set": {"status": "approved"}})
    await audit(user, "po.approve", "purchase_order", po_id, {})
    return _enrich(await _pos(user).find_one({"id": po_id}))


class ReceiveLine(BaseModel):
    product_id: str
    quantity: int


class ReceiveBody(BaseModel):
    lines: List[ReceiveLine]
    receive_date: Optional[str] = None


@router.post("/{po_id}/receive")
async def receive_po(po_id: str, body: ReceiveBody,
                     user: dict = Depends(require("ops:manage"))):
    po = await _pos(user).find_one({"id": po_id})
    if not po:
        raise HTTPException(404, "Purchase order not found")
    if po["status"] not in ("approved", "partially_received"):
        raise HTTPException(409, "PO must be approved before receiving")
    if not body.lines:
        raise HTTPException(422, "Nothing to receive")
    await _ensure_grni(user)
    rdate = body.receive_date or today_str()
    tid = user["tenant_id"]
    total_cents = 0
    for rl in body.lines:
        if rl.quantity <= 0:
            raise HTTPException(422, "Receive quantities must be positive")
        line = next((l for l in po["lines"] if l["product_id"] == rl.product_id), None)
        if not line:
            raise HTTPException(404, f"Line not on PO: {rl.product_id}")
        if line["received_qty"] + rl.quantity > line["quantity"]:
            raise HTTPException(409, f"Cannot receive more than ordered for {line['product_name']}")
        # update product qty + moving-average cost
        prod = await Scoped(db.products, tid).find_one({"id": rl.product_id})
        old_qty = int(prod.get("quantity_on_hand") or 0)
        old_cost = int(prod.get("unit_cost_cents") or 0)
        new_qty = old_qty + rl.quantity
        new_cost = round((old_qty * old_cost + rl.quantity * line["unit_cost_cents"]) / new_qty) if new_qty else line["unit_cost_cents"]
        await Scoped(db.products, tid).update_one(
            {"id": rl.product_id},
            {"$set": {"quantity_on_hand": new_qty, "unit_cost_cents": new_cost}})
        line["received_qty"] += rl.quantity
        total_cents += rl.quantity * line["unit_cost_cents"]
    # DR Inventory / CR GRNI
    if total_cents > 0:
        await L.post_journal_entry(db, tid, user, {
            "entry_date": rdate, "source": "po_receive", "source_ref": po_id,
            "memo": f"PO {po['number']} received — {po['vendor']}",
            "lines": [P.dr(INV_CODE, total_cents, "Inventory received"),
                      P.cr(GRNI_CODE, total_cents, "Goods received not invoiced")],
        }, status="posted")
    fully = all(l["received_qty"] >= l["quantity"] for l in po["lines"])
    await _pos(user).update_one(
        {"id": po_id},
        {"$set": {"lines": po["lines"],
                  "status": "received" if fully else "partially_received"}})
    await audit(user, "po.receive", "purchase_order", po_id,
                {"received_cents": total_cents})
    return _enrich(await _pos(user).find_one({"id": po_id}))


class ConvertBody(BaseModel):
    bill_date: Optional[str] = None
    due_date: Optional[str] = None


@router.post("/{po_id}/convert-to-bill")
async def convert_to_bill(po_id: str, body: ConvertBody,
                          user: dict = Depends(require("ops:manage"))):
    from routers.bills import BillBody, BillLine, create_bill
    po = await _pos(user).find_one({"id": po_id})
    if not po:
        raise HTTPException(404, "Purchase order not found")
    if po["status"] not in ("received", "partially_received"):
        raise HTTPException(409, "Receive goods before converting to a bill")
    if po.get("bill_id"):
        raise HTTPException(409, "PO already converted to a bill")
    recv_lines = [l for l in po["lines"] if l.get("received_qty", 0) > 0]
    if not recv_lines:
        raise HTTPException(409, "Nothing received yet")
    # DR GRNI (clear accrual) / CR AP — via the standard bill flow
    bill = await create_bill(BillBody(
        vendor=po["vendor"], vendor_id=po.get("vendor_id"),
        bill_date=body.bill_date, due_date=body.due_date,
        lines=[BillLine(expense_code=GRNI_CODE,
                        description=f"PO {po['number']} — {l['product_name']} x{l['received_qty']}",
                        amount=l["received_qty"] * l["unit_cost_cents"] / 100)
               for l in recv_lines]), user)
    await _pos(user).update_one({"id": po_id}, {"$set": {"status": "billed",
                                                        "bill_id": bill["id"]}})
    await audit(user, "po.convert_to_bill", "purchase_order", po_id,
                {"bill_id": bill["id"]})
    return {"po": _enrich(await _pos(user).find_one({"id": po_id})), "bill": bill}


@router.post("/{po_id}/cancel")
async def cancel_po(po_id: str, user: dict = Depends(require("ops:manage"))):
    po = await _pos(user).find_one({"id": po_id})
    if not po:
        raise HTTPException(404, "Purchase order not found")
    if po["status"] in ("billed", "cancelled"):
        raise HTTPException(409, "Cannot cancel this PO")
    if po["status"] in ("received", "partially_received"):
        raise HTTPException(409, "Goods already received — adjust inventory instead")
    await _pos(user).update_one({"id": po_id}, {"$set": {"status": "cancelled"}})
    await audit(user, "po.cancel", "purchase_order", po_id, {})
    return _enrich(await _pos(user).find_one({"id": po_id}))
