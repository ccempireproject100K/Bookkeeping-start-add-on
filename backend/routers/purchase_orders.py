import uuid

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from lib.auth import Principal, require
from lib.dates import now_iso, today_iso
from lib.db import db
from lib.mailer import send_email
from lib.money import create_txn
from lib.repo import Scoped
from lib.stock import load_product, post_movement
from models.inventory import OutboxEmail, POApprovalIn, POCreate, POEmailIn, POLineIn, POStatusUpdate, PurchaseOrder, ReceiveIn

router = APIRouter()

TRANSITIONS = {"draft": {"ordered", "cancelled"}, "ordered": {"received", "cancelled"}, "partial": {"received", "cancelled"}}


def needs_approval(p: Principal, doc: dict) -> bool:
    """Orders above the owner's limit can't be ordered/emailed until an owner approves them."""
    limit = float(p.settings.get("po_approval_limit") or 0)
    return doc["status"] == "draft" and limit > 0 and doc["total"] > limit and not doc.get("approved_at")


def to_po(p: Principal, doc: dict) -> PurchaseOrder:
    return PurchaseOrder(**{k: v for k, v in doc.items() if k != "tenant_id"}, awaiting_approval=needs_approval(p, doc))


def guard_approval(p: Principal, doc: dict) -> None:
    if needs_approval(p, doc):
        raise HTTPException(403, f"{doc['number']} is over the ${float(p.settings['po_approval_limit']):,.2f} limit and needs owner approval first")


async def create_po(p: Principal, supplier: dict, lines: list[POLineIn], expected_date: str | None, notes: str) -> PurchaseOrder:
    out_lines = []
    for ln in lines:
        prod = await Scoped("products", p).find_one({"id": ln.product_id})
        if not prod:
            raise HTTPException(400, "Product not found")
        if prod["tracking_mode"] == "serial":
            raise HTTPException(400, f"{prod['name']} is serial-tracked — receive it with Restock so each serial is scanned")
        out_lines.append({"product_id": prod["id"], "sku": prod.get("sku"), "name": prod["name"], "quantity": ln.quantity, "unit_cost": ln.unit_cost, "received_qty": 0})
    t = await db.tenants.find_one_and_update({"id": p.tenant_id}, {"$inc": {"po_seq": 1}}, return_document=ReturnDocument.AFTER)
    doc = {"id": str(uuid.uuid4()), "number": f"PO-{t['po_seq']:05d}", "supplier_id": supplier["id"], "supplier_name": supplier["name"],
           "status": "draft", "lines": out_lines, "total": round(sum(l["quantity"] * l["unit_cost"] for l in out_lines), 2),
           "expected_date": expected_date, "notes": notes, "created_by": p.name, "created_at": now_iso(),
           "ordered_at": None, "received_at": None}
    return to_po(p, await Scoped("purchase_orders", p).insert(doc))


@router.get("/purchase-orders", response_model=list[PurchaseOrder])
async def list_pos(status: str = "", p: Principal = Depends(require("po:read"))):
    return [to_po(p, d) for d in await Scoped("purchase_orders", p).find({"status": status} if status else {}, sort=[("created_at", -1)])]


@router.post("/purchase-orders", response_model=PurchaseOrder)
async def create_purchase_order(body: POCreate, p: Principal = Depends(require("po:write"))):
    supplier = await Scoped("suppliers", p).find_one({"id": body.supplier_id})
    if not supplier:
        raise HTTPException(400, "Supplier not found")
    return await create_po(p, supplier, body.lines, body.expected_date, body.notes)


async def receive_lines(p: Principal, doc: dict, qty: dict[str, int], cost: dict[str, float], close: bool) -> dict:
    """Post restock movements + one expense per category for what actually arrived; track backorders per line.
    Optimistic lock on `rev` so two people receiving the same PO can't double-post."""
    lines = [dict(l, received_qty=l.get("received_qty", 0)) for l in doc["lines"]]
    for l in lines:
        q = qty.get(l["product_id"], 0)
        if q > l["quantity"] - l["received_qty"]:
            raise HTTPException(400, f"{l['name']}: only {l['quantity'] - l['received_qty']} still outstanding")
    if not any(qty.get(l["product_id"], 0) for l in lines) and not close:
        raise HTTPException(400, "Enter at least one received quantity")
    now = now_iso()
    for l in lines:
        l["received_qty"] += qty.get(l["product_id"], 0)
    complete = all(l["received_qty"] >= l["quantity"] for l in lines)
    status = "received" if complete or close else "partial"
    patch = {"lines": lines, "status": status, "rev": doc.get("rev", 0) + 1, **({"received_at": now} if status == "received" else {}),
             **({} if doc.get("first_received_at") else {"first_received_at": now})}
    updated = await Scoped("purchase_orders", p).update({"id": doc["id"], "rev": doc.get("rev", 0) if "rev" in doc else None, "status": doc["status"]} if "rev" in doc
                                                        else {"id": doc["id"], "rev": {"$exists": False}, "status": doc["status"]}, {"$set": patch})
    if not updated:
        raise HTTPException(409, "Order was modified concurrently, refresh and retry")
    by_cat: dict[str, tuple[float, list[str]]] = {}
    for l in lines:
        q = qty.get(l["product_id"], 0)
        if q <= 0:
            continue
        prod = await load_product(p, l["product_id"])
        uc = cost.get(l["product_id"], l["unit_cost"])
        mv = await post_movement(p, prod, "restock", q, unit_cost=uc, supplier_name=doc["supplier_name"],
                                 lot_number=doc["number"] if prod["tracking_mode"] == "lot" else None,
                                 note=f"Received on {doc['number']}" + ("" if complete else " (partial)"), update_avg_cost=True)
        cat = p.settings["expense_category_retail" if prod["type"] == "retail" else "expense_category_supply"]
        amt, ids = by_cat.get(cat, (0.0, []))
        by_cat[cat] = (amt + q * uc, ids + [mv["id"]])
    for cat, (amt, ids) in by_cat.items():
        if amt > 0:
            await create_txn(p, "expense", cat, amt, today_iso(p.settings.get("timezone")), f"{doc['number']} · {doc['supplier_name']}", source="inventory", movement_ids=ids, capitalized=True)
    return updated


@router.post("/purchase-orders/{id}/status", response_model=PurchaseOrder)
async def update_status(id: str, body: POStatusUpdate, p: Principal = Depends(require("po:write"))):
    pos = Scoped("purchase_orders", p)
    doc = await pos.find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Purchase order not found")
    if body.status not in TRANSITIONS.get(doc["status"], set()):
        raise HTTPException(400, f"Cannot move a {doc['status']} order to {body.status}")
    if body.status == "received":  # receive everything still outstanding
        rest = {l["product_id"]: l["quantity"] - l.get("received_qty", 0) for l in doc["lines"]}
        return to_po(p, await receive_lines(p, doc, rest, {}, close=True))
    if body.status == "ordered":
        guard_approval(p, doc)
    now = now_iso()
    patch: dict = {"status": body.status, **({"ordered_at": now} if body.status == "ordered" else {})}
    updated = await pos.update({"id": id, "status": doc["status"]}, {"$set": patch})
    if not updated:
        raise HTTPException(409, "Order was modified concurrently, refresh and retry")
    return to_po(p, updated)


@router.post("/purchase-orders/{id}/approval", response_model=PurchaseOrder)
async def approval(id: str, body: POApprovalIn, p: Principal = Depends(require("po:approve"))):
    """Owner approves (unlocks ordering/emailing) or rejects (cancels) an over-limit draft."""
    doc = await Scoped("purchase_orders", p).find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Purchase order not found")
    if doc["status"] != "draft":
        raise HTTPException(400, "Only drafts can be approved or rejected")
    now = now_iso()
    patch = ({"approved_by": p.name, "approved_at": now} if body.approve
             else {"status": "cancelled", "notes": (doc.get("notes", "") + f"\nRejected by {p.name}: {body.note}").strip()})
    updated = await Scoped("purchase_orders", p).update({"id": id, "status": "draft"}, {"$set": patch})
    if not updated:
        raise HTTPException(409, "Order was modified concurrently, refresh and retry")
    return to_po(p, updated)


@router.post("/purchase-orders/{id}/receive", response_model=PurchaseOrder)
async def receive(id: str, body: ReceiveIn, p: Principal = Depends(require("po:write"))):
    """Partial receiving: post what arrived; the rest stays open as a backorder (or is closed)."""
    doc = await Scoped("purchase_orders", p).find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Purchase order not found")
    if doc["status"] not in ("ordered", "partial"):
        raise HTTPException(400, f"A {doc['status']} order can't be received")
    known = {l["product_id"] for l in doc["lines"]}
    if any(l.product_id not in known for l in body.lines):
        raise HTTPException(400, "Line not on this order")
    qty = {l.product_id: l.quantity for l in body.lines}
    cost = {l.product_id: l.unit_cost for l in body.lines if l.unit_cost is not None}
    return to_po(p, await receive_lines(p, doc, qty, cost, body.close_backorder))


@router.post("/purchase-orders/{id}/email", response_model=OutboxEmail)
async def email_po(id: str, body: POEmailIn, p: Principal = Depends(require("po:write"))):
    """Email the PO to the supplier (generic SMTP; logged to the outbox if SMTP isn't configured)."""
    doc = await Scoped("purchase_orders", p).find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Purchase order not found")
    if doc["status"] not in ("draft", "ordered", "partial"):
        raise HTTPException(400, f"A {doc['status']} order can't be sent")
    guard_approval(p, doc)
    sup = await Scoped("suppliers", p).find_one({"id": doc["supplier_id"]})
    if not sup or not sup.get("email"):
        raise HTTPException(400, "Add an email address to this supplier first")
    lines = [f"  {l['quantity']:>4} × {l['name']}" + (f" (SKU {l['sku']})" if l.get("sku") else "") + f"  @ ${l['unit_cost']:.2f}"
             for l in doc["lines"]]
    text = "\n".join([
        f"Hi {sup.get('contact_name') or sup['name']},", "",
        body.message or f"Please supply the following for {p.tenant_name}:", "",
        f"Purchase order {doc['number']}" + (f" — needed by {doc['expected_date']}" if doc.get("expected_date") else ""),
        *lines, "", f"Order total: ${doc['total']:.2f}", "", f"Thanks,", p.name, p.tenant_name, f"Reply to: {p.email}",
    ])
    subject = f"Purchase order {doc['number']} from {p.tenant_name}"
    status, err = await send_email(sup["email"], subject, text, reply_to=p.email)
    out = {"id": str(uuid.uuid4()), "to": sup["email"], "subject": subject, "body": text, "status": status, "error": err, "created_at": now_iso()}
    await Scoped("email_outbox", p).insert(dict(out))
    patch: dict = {"emailed_at": out["created_at"]}
    if body.mark_ordered and doc["status"] == "draft" and status != "failed":
        patch.update(status="ordered", ordered_at=out["created_at"])
    await Scoped("purchase_orders", p).update({"id": id}, {"$set": patch})
    return OutboxEmail(**out)


@router.delete("/purchase-orders/{id}")
async def delete_po(id: str, p: Principal = Depends(require("po:delete"))):
    pos = Scoped("purchase_orders", p)
    doc = await pos.find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Purchase order not found")
    if doc["status"] != "draft":
        raise HTTPException(400, "Only draft orders can be deleted")
    await pos.delete({"id": id})
    return {"ok": True}
