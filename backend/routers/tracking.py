"""Lot & serial tracking API (Top-20 #17).

Resurrects — as a clean rewrite — the lot/serial layer that used to live
in the orphaned ``routers/movements.py`` (unimportable on main: it depends
on ``models.inventory``, ``lib.stock``, ``lib.ops`` and ``lib.repo``, none
of which exist anymore). Everything here runs on the current stack
(``lib.base.Scoped``, integer cents, tenant scoping).

Endpoints (mounted under /api):
- POST /inventory/receive            receive serials or a lot for a tracked product
- POST /inventory/assign             consume specific serials / draw from a lot (FEFO default)
- POST /inventory/return             bring tracked stock back in
- POST /inventory/transfer           move assigned units to another job/customer
- GET  /inventory/serial/{serial}    one unit + its full story + warranty status
- GET  /inventory/units              serial units (filters: product_id, status)
- GET  /inventory/lots               lots (filter: product_id)
- GET  /inventory/at                 parts at a site (job/customer) + CSV export
- GET  /inventory/expiring           lots expiring within N days (use-first list)
- GET  /inventory/warranty           warranty report across units
- POST /inventory/recalls            flag a serial range / lot as recalled
- GET  /inventory/recalls/{id}       a recall + who holds the affected units
- POST /inventory/scan               scan-to-move: lookup / receive / assign by code

Scan captures accept ``client_key`` — the #13 offline queue's item id —
and are idempotent on it, so a queued scan that retries returns the
original result instead of double-posting.
"""
import csv
import io
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from db import db
from lib import stocktrack as ST
from lib.auth import require, get_current_user
from lib.base import Scoped, new_id, now_iso, today_str, to_cents

router = APIRouter(prefix="/inventory", tags=["inventory-tracking"])

READ = Depends(get_current_user)
WRITE = Depends(require("ops:manage"))


# ---------------------------------------------------------------- helpers

def _clean(doc):
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("tenant_id", None)
    return doc


def warranty_status(unit: dict, today: str | None = None) -> str:
    """covered | expiring_soon | out_of_warranty | not_installed | no_warranty."""
    if not unit.get("warranty_start"):
        return "not_installed"
    until = unit.get("warranty_until")
    if not until:
        return "no_warranty"
    today = today or today_str()
    if until < today:
        return "out_of_warranty"
    if until <= (date.fromisoformat(today) + timedelta(days=30)).isoformat():
        return "expiring_soon"
    return "covered"


def unit_view(unit: dict) -> dict:
    u = _clean(unit)
    u["warranty_status"] = warranty_status(u)
    return u


def _where(unit: dict) -> str:
    """Plain-words location of a unit, for duplicate-guard warnings."""
    if unit.get("status") == "assigned":
        bits = ["already installed"]
        if unit.get("job_id"):
            bits.append(f"on job {unit['job_id']}")
        if unit.get("customer_id"):
            bits.append(f"for customer {unit['customer_id']}")
        if unit.get("assigned_date"):
            bits.append(f"since {unit['assigned_date']}")
        return " ".join(bits)
    if unit.get("status") == "sold":
        return "already sold"
    return "already in stock"


async def _validate_serials_free(user, product, serials):
    """Duplicate-serial guard: a serial may exist once per tenant, ever."""
    seen = set()
    for s in serials:
        n = ST.norm_serial(s)
        if not n:
            raise HTTPException(422, "Serial numbers can't be blank")
        if n in seen:
            raise HTTPException(422, f"Serial {n} appears twice in this receipt")
        seen.add(n)
    for s in serials:
        existing = await ST.find_unit(user, s)
        if existing:
            raise HTTPException(
                409,
                f"Serial {ST.norm_serial(s)} is {_where(existing)} "
                f"({existing.get('product_name') or product['name']}) — "
                "the same serial can never be received twice.")


async def _load_units_for_assign(user, product, serials):
    docs = []
    for s in serials:
        u = await ST.find_unit(user, s)
        if not u or u["product_id"] != product["id"]:
            raise HTTPException(404, f"Serial {ST.norm_serial(s)} not found for this product")
        if u.get("recalled"):
            raise HTTPException(409, f"Serial {u['serial']} is under recall and can't be assigned")
        if u["status"] == "assigned":
            raise HTTPException(409, f"Serial {u['serial']} is {_where(u)} — it can't be assigned twice.")
        if u["status"] != "in_stock":
            raise HTTPException(409, f"Serial {u['serial']} is {u['status']} and can't be assigned")
        docs.append(u)
    return docs


async def _check_idem(user, client_key):
    if not client_key:
        return None
    prior = await ST.scan_log(user).find_one({"client_key": client_key})
    return prior.get("result") if prior else None


async def _store_idem(user, client_key, result):
    if client_key:
        await ST.scan_log(user).insert_one({
            "id": new_id(), "client_key": client_key, "result": result,
            "created_at": now_iso(),
        })


# ---------------------------------------------------------------- bodies

class ReceiveBody(BaseModel):
    product_id: str
    quantity: int
    unit_cost: Optional[float] = None
    purchase_order_id: Optional[str] = None
    serials: Optional[List[str]] = None
    lot_number: Optional[str] = None
    expiry_date: Optional[str] = None
    note: str = ""
    client_key: Optional[str] = None


class AssignBody(BaseModel):
    product_id: str
    quantity: Optional[int] = None
    serials: Optional[List[str]] = None
    lot_id: Optional[str] = None
    job_id: Optional[str] = None
    customer_id: Optional[str] = None
    note: str = ""
    client_key: Optional[str] = None


class ReturnBody(BaseModel):
    product_id: str
    quantity: Optional[int] = None
    serials: Optional[List[str]] = None
    lot_id: Optional[str] = None
    lot_number: Optional[str] = None
    note: str = ""
    client_key: Optional[str] = None


class TransferBody(BaseModel):
    serials: List[str]
    job_id: Optional[str] = None
    customer_id: Optional[str] = None
    note: str = ""


class RecallBody(BaseModel):
    reason: str
    product_id: Optional[str] = None
    lot_number: Optional[str] = None
    serial_prefix: Optional[str] = None
    serial_from: Optional[str] = None
    serial_to: Optional[str] = None


class ScanBody(BaseModel):
    code: str
    action: str = "lookup"           # lookup | receive | assign
    product_id: Optional[str] = None
    quantity: Optional[int] = None
    serials: Optional[List[str]] = None
    lot_number: Optional[str] = None
    expiry_date: Optional[str] = None
    lot_id: Optional[str] = None
    job_id: Optional[str] = None
    customer_id: Optional[str] = None
    purchase_order_id: Optional[str] = None
    note: str = ""
    client_key: Optional[str] = None


# ---------------------------------------------------------------- receive

async def _do_receive(user, body: ReceiveBody, product: dict, *,
                      qty_already_applied: bool = False) -> dict:
    mode = ST.tracking_mode(product)
    if mode == "none":
        raise HTTPException(400, "This product isn't lot/serial tracked — use the regular restock instead")
    if body.quantity <= 0:
        raise HTTPException(422, "Quantity must be positive")
    cost = to_cents(body.unit_cost) if body.unit_cost is not None else None
    if mode == "serial":
        serials = [ST.norm_serial(s) for s in (body.serials or [])]
        if len(serials) != body.quantity:
            raise HTTPException(
                422, f"Serial-tracked receiving needs exactly one serial per unit: "
                     f"got {len(serials)} serials for {body.quantity} units")
        await _validate_serials_free(user, product, serials)
        created = await ST.receive_units(
            user, product, serials, source_ref=body.purchase_order_id,
            note=body.note, client_key=body.client_key,
            qty_already_applied=qty_already_applied, unit_cost_cents=cost)
        return {"mode": "serial", "units": [unit_view(u) for u in created],
                "quantity": len(created)}
    # lot mode
    if not (body.lot_number or "").strip():
        raise HTTPException(422, "Lot-tracked receiving needs a lot number")
    lot = await ST.receive_lot(
        user, product, body.lot_number, body.quantity,
        expiry_date=body.expiry_date, source_ref=body.purchase_order_id,
        note=body.note, client_key=body.client_key,
        qty_already_applied=qty_already_applied, unit_cost_cents=cost)
    return {"mode": "lot", "lot": _clean(lot), "quantity": body.quantity}


@router.post("/receive")
async def receive(body: ReceiveBody, user: dict = WRITE):
    prior = await _check_idem(user, body.client_key)
    if prior is not None:
        return prior
    product = await ST.get_product(user, body.product_id)
    result = await _do_receive(user, body, product)
    await _store_idem(user, body.client_key, result)
    return result


# ---------------------------------------------------------------- assign

async def _do_assign(user, body: AssignBody, product: dict) -> dict:
    mode = ST.tracking_mode(product)
    if mode == "none":
        raise HTTPException(400, "This product isn't lot/serial tracked")
    if not body.job_id and not body.customer_id:
        raise HTTPException(422, "Assign to a job or a customer")
    today = today_str()
    if mode == "serial":
        serials = [ST.norm_serial(s) for s in (body.serials or [])]
        if not serials:
            raise HTTPException(422, "Pick the specific serials to assign")
        docs = await _load_units_for_assign(user, product, serials)
        months = int(product.get("warranty_months") or 0)
        out = []
        for u in docs:
            updates = {"status": "assigned", "job_id": body.job_id,
                       "customer_id": body.customer_id, "assigned_date": today,
                       "warranty_start": today,
                       "warranty_until": ST.add_months(today, months) if months else None,
                       "updated_at": now_iso()}
            await ST.units(user).update_one({"id": u["id"]}, {"$set": updates})
            out.append(unit_view({**u, **updates}))
        await ST.bump_product_qty(user, product["id"], -len(docs))
        await ST.record_movement(user, product=product, mtype="assign",
                                 quantity=-len(docs), serials=serials,
                                 job_id=body.job_id, customer_id=body.customer_id,
                                 note=body.note, client_key=body.client_key)
        return {"mode": "serial", "units": out, "quantity": len(docs)}
    # lot mode — FEFO unless a specific lot is named
    qty = int(body.quantity or 0)
    if qty <= 0:
        raise HTTPException(422, "Quantity must be positive")
    lot = await ST.pick_lot_fefo(user, product["id"], body.lot_id)
    if int(lot["quantity_on_hand"]) < qty:
        raise HTTPException(
            409, f"Lot {lot['lot_number']} only has {lot['quantity_on_hand']} on hand")
    await ST.lots(user).update_one(
        {"id": lot["id"]},
        {"$inc": {"quantity_on_hand": -qty}, "$set": {"updated_at": now_iso()}})
    await ST.bump_product_qty(user, product["id"], -qty)
    await ST.record_movement(user, product=product, mtype="assign", quantity=-qty,
                             lot=lot, job_id=body.job_id, customer_id=body.customer_id,
                             note=body.note, client_key=body.client_key)
    return {"mode": "lot", "lot": _clean(await ST.lots(user).find_one({"id": lot["id"]})),
            "quantity": qty}


@router.post("/assign")
async def assign(body: AssignBody, user: dict = WRITE):
    prior = await _check_idem(user, body.client_key)
    if prior is not None:
        return prior
    product = await ST.get_product(user, body.product_id)
    result = await _do_assign(user, body, product)
    await _store_idem(user, body.client_key, result)
    return result


# ---------------------------------------------------------------- return / transfer

@router.post("/return")
async def stock_return(body: ReturnBody, user: dict = WRITE):
    prior = await _check_idem(user, body.client_key)
    if prior is not None:
        return prior
    product = await ST.get_product(user, body.product_id)
    mode = ST.tracking_mode(product)
    if mode == "none":
        raise HTTPException(400, "This product isn't lot/serial tracked")
    if mode == "serial":
        serials = [ST.norm_serial(s) for s in (body.serials or [])]
        if not serials:
            raise HTTPException(422, "Name the serials coming back")
        out = []
        for s in serials:
            u = await ST.find_unit(user, s)
            if not u or u["product_id"] != product["id"]:
                raise HTTPException(404, f"Serial {s} not found for this product")
            if u["status"] != "assigned":
                raise HTTPException(409, f"Serial {s} is {u['status']} — only installed units can be returned")
            updates = {"status": "in_stock", "job_id": None, "customer_id": None,
                       "assigned_date": None, "warranty_start": None,
                       "warranty_until": None, "updated_at": now_iso()}
            await ST.units(user).update_one({"id": u["id"]}, {"$set": updates})
            out.append(unit_view({**u, **updates}))
        await ST.bump_product_qty(user, product["id"], len(out))
        await ST.record_movement(user, product=product, mtype="return",
                                 quantity=len(out), serials=serials,
                                 note=body.note, client_key=body.client_key)
        result = {"mode": "serial", "units": out, "quantity": len(out)}
    else:
        qty = int(body.quantity or 0)
        if qty <= 0:
            raise HTTPException(422, "Quantity must be positive")
        lot = None
        if body.lot_id:
            lot = await ST.lots(user).find_one({"id": body.lot_id, "product_id": product["id"]})
        elif body.lot_number:
            lot = await ST.lots(user).find_one(
                {"product_id": product["id"], "lot_number": body.lot_number.strip()})
        if not lot:
            raise HTTPException(404, "Say which lot the units go back into")
        await ST.lots(user).update_one(
            {"id": lot["id"]},
            {"$inc": {"quantity_on_hand": qty}, "$set": {"updated_at": now_iso()}})
        await ST.bump_product_qty(user, product["id"], qty)
        await ST.record_movement(user, product=product, mtype="return", quantity=qty,
                                 lot=lot, note=body.note, client_key=body.client_key)
        result = {"mode": "lot",
                  "lot": _clean(await ST.lots(user).find_one({"id": lot["id"]})),
                  "quantity": qty}
    await _store_idem(user, body.client_key, result)
    return result


@router.post("/transfer")
async def transfer(body: TransferBody, user: dict = WRITE):
    if not body.job_id and not body.customer_id:
        raise HTTPException(422, "Transfer to a job or a customer")
    out = []
    first_product = None
    for s in body.serials:
        u = await ST.find_unit(user, s)
        if not u:
            raise HTTPException(404, f"Serial {ST.norm_serial(s)} not found")
        if u["status"] != "assigned":
            raise HTTPException(409, f"Serial {u['serial']} is {u['status']} — only installed units transfer")
        first_product = first_product or u
        updates = {"job_id": body.job_id, "customer_id": body.customer_id,
                   "updated_at": now_iso()}
        await ST.units(user).update_one({"id": u["id"]}, {"$set": updates})
        out.append(unit_view({**u, **updates}))
    prod = await ST.get_product(user, first_product["product_id"]) if first_product else {}
    await ST.record_movement(user, product=prod or {"id": "", "name": ""}, mtype="transfer",
                             quantity=0, serials=body.serials, job_id=body.job_id,
                             customer_id=body.customer_id, note=body.note)
    return {"units": out, "quantity": len(out)}


# ---------------------------------------------------------------- queries

@router.get("/serial/{serial}")
async def serial_story(serial: str, user: dict = READ):
    u = await ST.find_unit(user, serial)
    if not u:
        raise HTTPException(404, f"Serial {ST.norm_serial(serial)} not found")
    story = await ST.movements(user).find_list(
        {"serials": u["serial"]}, sort=[("created_at", 1)], limit=500)
    return {"unit": unit_view(u), "story": [_clean(m) for m in story]}


@router.get("/units")
async def list_units(product_id: str = "", status: str = "", user: dict = READ):
    q = {}
    if product_id:
        q["product_id"] = product_id
    if status:
        q["status"] = status
    rows = await ST.units(user).find_list(q, sort=[("serial", 1)], limit=5000)
    return [unit_view(r) for r in rows]


@router.get("/lots")
async def list_lots(product_id: str = "", user: dict = READ):
    q = {"product_id": product_id} if product_id else {}
    rows = await ST.lots(user).find_list(q, sort=[("expiry_date", 1)], limit=5000)
    return [_clean(r) for r in rows]


@router.get("/at")
async def parts_at_site(job_id: str = "", customer_id: str = "",
                        format: str = "", user: dict = READ):
    """Parts-at-a-site report: every tracked part installed at a job/customer."""
    if not job_id and not customer_id:
        raise HTTPException(422, "Ask for a job or a customer")
    q = {"status": "assigned"}
    if job_id:
        q["job_id"] = job_id
    if customer_id:
        q["customer_id"] = customer_id
    unit_rows = [unit_view(u) for u in
                 await ST.units(user).find_list(q, sort=[("assigned_date", 1)], limit=5000)]
    mv_q = {"type": "assign"}
    if job_id:
        mv_q["job_id"] = job_id
    if customer_id:
        mv_q["customer_id"] = customer_id
    lot_moves = await ST.movements(user).find_list(mv_q, sort=[("created_at", 1)], limit=5000)
    lot_rows = []
    for m in lot_moves:
        if m.get("lot_number"):
            lot_rows.append({
                "product_id": m["product_id"], "product_name": m.get("product_name"),
                "lot_number": m["lot_number"], "quantity": -int(m["quantity"]),
                "install_date": (m.get("created_at") or "")[:10],
                "warranty_status": "lot — no per-unit warranty",
            })
    report = {"job_id": job_id or None, "customer_id": customer_id or None,
              "serial_units": unit_rows, "lot_draws": lot_rows,
              "total_units": len(unit_rows) + sum(r["quantity"] for r in lot_rows)}
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["kind", "serial_or_lot", "product", "quantity", "install_date", "warranty_status"])
        for u in unit_rows:
            w.writerow(["serial", u["serial"], u.get("product_name"), 1,
                        u.get("assigned_date") or "", u["warranty_status"]])
        for r in lot_rows:
            w.writerow(["lot", r["lot_number"], r.get("product_name"), r["quantity"],
                        r["install_date"], r["warranty_status"]])
        return Response(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="parts-at-site.csv"'})
    return report


@router.get("/expiring")
async def expiring(days: int = 30, user: dict = READ):
    today = today_str()
    horizon = (date.fromisoformat(today) + timedelta(days=max(days, 0))).isoformat()
    rows = await ST.lots(user).find_list(
        {"quantity_on_hand": {"$gt": 0}, "expiry_date": {"$ne": None}},
        sort=[("expiry_date", 1)], limit=5000)
    out = []
    for r in rows:
        exp = r.get("expiry_date")
        if exp and exp <= horizon:
            d = _clean(r)
            d["expired"] = exp < today
            d["days_left"] = (date.fromisoformat(exp) - date.fromisoformat(today)).days
            out.append(d)
    return out


@router.get("/warranty")
async def warranty_report(status: str = "", user: dict = READ):
    """Warranty clock report: every installed unit with its coverage status."""
    rows = await ST.units(user).find_list(
        {"status": "assigned"}, sort=[("warranty_until", 1)], limit=5000)
    views = [unit_view(r) for r in rows]
    if status:
        views = [v for v in views if v["warranty_status"] == status]
    counts = {}
    for v in views:
        counts[v["warranty_status"]] = counts.get(v["warranty_status"], 0) + 1
    return {"units": views, "counts": counts}


# ---------------------------------------------------------------- recalls

@router.post("/recalls")
async def create_recall(body: RecallBody, user: dict = WRITE):
    if not (body.lot_number or body.serial_prefix or
            (body.serial_from and body.serial_to) or body.product_id):
        raise HTTPException(422, "Give a lot number, a serial range/prefix, or a product to recall")
    q: dict = {}
    if body.product_id:
        q["product_id"] = body.product_id
    candidates = await ST.units(user).find_list(q, limit=10000)
    lo, hi = (ST.norm_serial(body.serial_from or ""),
              ST.norm_serial(body.serial_to or ""))
    prefix = ST.norm_serial(body.serial_prefix or "")
    affected = []
    for u in candidates:
        s = u["serial"]
        if prefix and not s.startswith(prefix):
            continue
        if lo and hi and not (lo <= s <= hi):
            continue
        if body.lot_number:
            continue  # serial units don't carry lots; lot recall handled below
        affected.append(u)
    lot_hits = []
    if body.lot_number:
        lot_q = {"lot_number": body.lot_number.strip()}
        if body.product_id:
            lot_q["product_id"] = body.product_id
        lot_hits = await ST.lots(user).find_list(lot_q, limit=500)
        # units received under that lot on movements
        mvs = await ST.movements(user).find_list(
            {"type": "receive", "lot_number": body.lot_number.strip()}, limit=5000)
        serials_in_lot = {s for m in mvs for s in (m.get("serials") or [])}
        affected = [u for u in candidates if u["serial"] in serials_in_lot]
    recall = {
        "id": new_id(), "reason": body.reason.strip(),
        "product_id": body.product_id, "lot_number": body.lot_number,
        "serial_prefix": body.serial_prefix, "serial_from": body.serial_from,
        "serial_to": body.serial_to,
        "affected_serial_ids": [u["id"] for u in affected],
        "affected_lot_ids": [l["id"] for l in lot_hits],
        "created_at": now_iso(), "created_by": user.get("email"),
    }
    await ST.recalls(user).insert_one(recall)
    for u in affected:
        await ST.units(user).update_one(
            {"id": u["id"]},
            {"$set": {"recalled": True, "recall_id": recall["id"],
                      "updated_at": now_iso()}})
    for l in lot_hits:
        await ST.lots(user).update_one(
            {"id": l["id"]},
            {"$set": {"recalled": True, "recall_id": recall["id"],
                      "updated_at": now_iso()}})
    holders = [unit_view({**u, "recalled": True}) for u in affected]
    return {"recall": _clean(recall), "affected_units": holders,
            "affected_lots": [_clean(l) for l in lot_hits],
            "affected_count": len(holders) + len(lot_hits)}


@router.get("/recalls/{recall_id}")
async def get_recall(recall_id: str, user: dict = READ):
    r = await ST.recalls(user).find_one({"id": recall_id})
    if not r:
        raise HTTPException(404, "Recall not found")
    units_out = []
    for uid in r.get("affected_serial_ids", []):
        u = await ST.units(user).find_one({"id": uid})
        if u:
            units_out.append(unit_view(u))
    lots_out = [_clean(l) for l in await ST.lots(user).find_list(
        {"id": {"$in": r.get("affected_lot_ids", [])}}, limit=500)]
    return {"recall": _clean(r), "affected_units": units_out, "affected_lots": lots_out}


# ---------------------------------------------------------------- scan

@router.post("/scan")
async def scan(body: ScanBody, user: dict = WRITE):
    """Scan-to-move entry point. ``code`` is the scanned string (a serial
    or a lot number); the same payload shape the #13 offline queue carries
    (kind ``stock_scan`` + ``client_key`` idempotency)."""
    prior = await _check_idem(user, body.client_key)
    if prior is not None:
        return {"ok": True, "kind": "stock_scan", "duplicate": True, "result": prior}
    code = ST.norm_serial(body.code)
    if not code:
        raise HTTPException(422, "Empty scan code")
    if body.action == "lookup":
        u = await ST.find_unit(user, code)
        if u:
            story = await ST.movements(user).find_list(
                {"serials": u["serial"]}, sort=[("created_at", 1)], limit=500)
            result = {"found": "serial", "unit": unit_view(u),
                      "story": [_clean(m) for m in story]}
        else:
            lot_rows = await ST.lots(user).find_list({"lot_number": body.code.strip()}, limit=50)
            if not lot_rows:
                # lot numbers aren't case-normalized the same way; try raw
                lot_rows = await ST.lots(user).find_list({"lot_number": code}, limit=50)
            if lot_rows:
                result = {"found": "lot", "lots": [_clean(l) for l in lot_rows]}
            else:
                result = {"found": None}
        return {"ok": True, "kind": "stock_scan", "result": result}
    if not body.product_id:
        raise HTTPException(422, "A receive/assign scan needs the product")
    product = await ST.get_product(user, body.product_id)
    if body.action == "receive":
        serials = body.serials or [code]
        res = await _do_receive(user, ReceiveBody(
            product_id=body.product_id,
            quantity=len(serials) if ST.tracking_mode(product) == "serial"
            else int(body.quantity or 1),
            serials=serials, lot_number=body.lot_number or body.code,
            expiry_date=body.expiry_date,
            purchase_order_id=body.purchase_order_id, note=body.note,
            client_key=body.client_key), product)
    elif body.action == "assign":
        res = await _do_assign(user, AssignBody(
            product_id=body.product_id, quantity=body.quantity,
            serials=body.serials or [code], lot_id=body.lot_id,
            job_id=body.job_id, customer_id=body.customer_id,
            note=body.note, client_key=body.client_key), product)
    else:
        raise HTTPException(422, f"Unknown scan action: {body.action}")
    await _store_idem(user, body.client_key, res)
    return {"ok": True, "kind": "stock_scan", "result": res}
