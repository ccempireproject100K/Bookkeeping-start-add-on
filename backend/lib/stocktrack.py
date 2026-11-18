"""Lot & serial tracking core (Top-20 #17 resurrection).

The previous lot/serial implementation lived in the orphaned
``routers/movements.py``, written against a pre-refactor architecture
(``models.inventory``, ``lib.stock``, ``lib.ops``, ``lib.repo`` — none of
which exist on main) and a barbershop-niche schema (``barber_id`` fields).
It could not even be imported. This module is a clean rewrite on the
current architecture (``lib.base.Scoped``, integer cents, tenant scoping),
keeping the parts of the old model that were sound: an append-only
movement ledger, serial units as first-class records, lots with expiry.

Collections:
- ``stock_units``     — one doc per serial-tracked unit.
- ``stock_lots``      — one doc per lot of a lot-tracked product.
- ``stock_movements`` — append-only story for units and lots.
- ``stock_recalls``   — recall criteria + affected unit ids.
- ``stock_scan_log``  — idempotency records for scan captures
  (``client_key`` doubles as the offline-queue idempotency key).

Non-tracked products never touch these collections; their inventory
behavior stays exactly what ``products.py`` / ``pos.py`` do today.
"""
from datetime import date, timedelta

from fastapi import HTTPException

from db import db
from lib.base import Scoped, new_id, now_iso, today_str

TRACKING_MODES = ("none", "serial", "lot")


def norm_serial(s: str) -> str:
    return (s or "").strip().upper()


def units(user):
    return Scoped(db.stock_units, user["tenant_id"])


def lots(user):
    return Scoped(db.stock_lots, user["tenant_id"])


def movements(user):
    return Scoped(db.stock_movements, user["tenant_id"])


def recalls(user):
    return Scoped(db.stock_recalls, user["tenant_id"])


def scan_log(user):
    return Scoped(db.stock_scan_log, user["tenant_id"])


def add_months(iso_date: str, months: int) -> str:
    """Calendar-month addition for warranty math (no dateutil dependency)."""
    y, m, d = (int(x) for x in iso_date[:10].split("-"))
    total = (y * 12 + (m - 1)) + int(months)
    ny, nm = total // 12, total % 12 + 1
    # clamp the day to the target month's length
    if nm == 12:
        nxt = date(ny + 1, 1, 1)
    else:
        nxt = date(ny, nm + 1, 1)
    last_day = (nxt - timedelta(days=1)).day
    return date(ny, nm, min(d, last_day)).isoformat()


async def get_product(user, product_id: str) -> dict:
    prod = await Scoped(db.products, user["tenant_id"]).find_one({"id": product_id})
    if not prod:
        raise HTTPException(404, "Product not found")
    return prod


def tracking_mode(prod: dict) -> str:
    mode = prod.get("tracking_mode") or "none"
    return mode if mode in TRACKING_MODES else "none"


async def record_movement(user, *, product, mtype, quantity, serials=None, lot=None,
                          job_id=None, customer_id=None, note="", source_ref=None,
                          client_key=None) -> dict:
    doc = {
        "id": new_id(), "product_id": product["id"], "product_name": product.get("name"),
        "type": mtype, "quantity": int(quantity),
        "serials": [norm_serial(s) for s in (serials or [])],
        "lot_id": lot["id"] if lot else None,
        "lot_number": lot["lot_number"] if lot else None,
        "job_id": job_id, "customer_id": customer_id,
        "note": note or "", "source_ref": source_ref, "client_key": client_key,
        "performed_by": user.get("email"), "created_at": now_iso(),
    }
    await movements(user).insert_one(doc)
    return doc


async def bump_product_qty(user, product_id: str, delta: int, unit_cost_cents=None):
    """Adjust the product's quantity_on_hand (and moving-average cost on
    inbound moves that carry a cost) exactly as the PO-receive path does."""
    coll = Scoped(db.products, user["tenant_id"])
    prod = await coll.find_one({"id": product_id})
    old_qty = int(prod.get("quantity_on_hand") or 0)
    new_qty = old_qty + int(delta)
    updates = {"quantity_on_hand": new_qty}
    if delta > 0 and unit_cost_cents is not None:
        old_cost = int(prod.get("unit_cost_cents") or 0)
        updates["unit_cost_cents"] = (
            round((old_qty * old_cost + delta * int(unit_cost_cents)) / new_qty)
            if new_qty else int(unit_cost_cents))
    await coll.update_one({"id": product_id}, {"$set": updates})
    return new_qty


async def receive_units(user, product, serials, *, source_ref=None, job_id=None,
                        customer_id=None, note="", client_key=None,
                        qty_already_applied=False, unit_cost_cents=None) -> list[dict]:
    """Create serial unit records for a receive. Caller validates first."""
    today = today_str()
    created = []
    for s in serials:
        doc = {
            "id": new_id(), "product_id": product["id"],
            "product_name": product.get("name"), "serial": norm_serial(s),
            "status": "in_stock", "job_id": None, "customer_id": None,
            "received_date": today, "received_ref": source_ref,
            "assigned_date": None, "warranty_start": None, "warranty_until": None,
            "recalled": False, "recall_id": None,
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        await units(user).insert_one(doc)
        created.append(doc)
    if not qty_already_applied:
        await bump_product_qty(user, product["id"], len(serials),
                               unit_cost_cents=unit_cost_cents)
    await record_movement(user, product=product, mtype="receive",
                          quantity=len(serials), serials=serials,
                          job_id=job_id, customer_id=customer_id, note=note,
                          source_ref=source_ref, client_key=client_key)
    return created


async def receive_lot(user, product, lot_number, quantity, *, expiry_date=None,
                      source_ref=None, note="", client_key=None,
                      qty_already_applied=False, unit_cost_cents=None) -> dict:
    lot_number = (lot_number or "").strip()
    existing = await lots(user).find_one(
        {"product_id": product["id"], "lot_number": lot_number})
    if existing:
        await lots(user).update_one(
            {"id": existing["id"]},
            {"$inc": {"quantity_on_hand": int(quantity)},
             "$set": {"updated_at": now_iso(),
                      **({"expiry_date": expiry_date} if expiry_date else {})}})
        lot = await lots(user).find_one({"id": existing["id"]})
    else:
        lot = {
            "id": new_id(), "product_id": product["id"],
            "product_name": product.get("name"), "lot_number": lot_number,
            "quantity_on_hand": int(quantity), "expiry_date": expiry_date,
            "received_date": today_str(), "received_ref": source_ref,
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        await lots(user).insert_one(lot)
    if not qty_already_applied:
        await bump_product_qty(user, product["id"], int(quantity),
                               unit_cost_cents=unit_cost_cents)
    await record_movement(user, product=product, mtype="receive",
                          quantity=int(quantity), lot=lot, note=note,
                          source_ref=source_ref, client_key=client_key)
    return lot


async def find_unit(user, serial: str) -> dict | None:
    return await units(user).find_one({"serial": norm_serial(serial)})


async def pick_lot_fefo(user, product_id: str, lot_id: str | None = None) -> dict:
    """Earliest-expiry lot with stock; lots without an expiry go last."""
    if lot_id:
        lot = await lots(user).find_one({"id": lot_id, "product_id": product_id})
        if not lot:
            raise HTTPException(404, "Lot not found for this product")
        return lot
    rows = await lots(user).find_list(
        {"product_id": product_id, "quantity_on_hand": {"$gt": 0}},
        sort=[("received_date", 1)], limit=500)
    if not rows:
        raise HTTPException(409, "No lot stock available for this product")
    rows.sort(key=lambda r: (r.get("expiry_date") is None, r.get("expiry_date") or "",
                             r.get("received_date") or ""))
    return rows[0]
