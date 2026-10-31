"""Guided field-mapping importer (bake-off #16): customers, vendors, items.

Upload a CSV → columns are auto-matched to our fields (synonyms, case and
punctuation-insensitive) → the mapping can be overridden and SAVED per
tenant for reuse (shared store with bake-off #11 import profiles) → run
with live row results: created / duplicate-skipped / per-row errors, plus
an emailed completion summary (best-effort via lib/mailer).

Implemented independently in our stack (behavior study: Akaunting Help
Center import flow). Nothing here posts to the ledger.
"""
import csv
import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.base import Scoped, new_id, now_iso, to_cents
from lib.auth import require
from lib.mailer import send_email

router = APIRouter(prefix="/import", tags=["import"])

FIELDS = {
    "customers": ["name", "email", "phone", "address"],
    "vendors": ["name", "email", "phone", "address"],
    "items": ["name", "price", "sku", "kind"],
}

SYNONYMS = {
    "name": ["name", "customer", "customer name", "client", "vendor", "vendor name",
             "company", "business name", "item", "product", "description", "service"],
    "email": ["email", "e-mail", "email address", "mail"],
    "phone": ["phone", "phone number", "tel", "telephone", "mobile", "cell"],
    "address": ["address", "street", "city", "billing address", "location"],
    "price": ["price", "rate", "amount", "unit price", "cost"],
    "sku": ["sku", "code", "item code", "part number", "number"],
    "kind": ["kind", "type", "category"],
}

COLLS = {"customers": "customers", "vendors": "vendors", "items": "products"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def auto_map(headers: list, kind: str) -> list:
    """[{csv, field, confidence}] — exact synonym = 1.0, contains = 0.7."""
    fields = FIELDS[kind]
    used = set()
    out = []
    for h in headers:
        nh = _norm(h)
        best, conf = None, 0.0
        for f in fields:
            if f in used:
                continue
            for syn in SYNONYMS.get(f, [f]):
                if nh == _norm(syn):
                    best, conf = f, 1.0
                    break
                if _norm(syn) in nh or nh in _norm(syn):
                    if conf < 0.7:
                        best, conf = f, 0.7
            if conf == 1.0:
                break
        if best:
            used.add(best)
        out.append({"csv": h, "field": best, "confidence": conf})
    return out


def _parse(text: str):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=422, detail="CSV has no header row")
    return [h for h in reader.fieldnames], list(reader)


def _apply(rows, mapping: list) -> list:
    """mapping: [{csv, field}] → row dicts keyed by our field names."""
    pairs = [(m["csv"], m["field"]) for m in mapping if m.get("field")]
    out = []
    for r in rows:
        doc = {}
        for csv_col, field in pairs:
            v = (r.get(csv_col) or "").strip()
            if v:
                doc[field] = v
        out.append(doc)
    return out


class PreviewIn(BaseModel):
    kind: str
    csv_text: str


@router.post("/preview")
async def preview(body: PreviewIn, user: dict = Depends(require("ops:manage"))):
    if body.kind not in FIELDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(FIELDS)}")
    headers, rows = _parse(body.csv_text)
    mapping = auto_map(headers, body.kind)
    mapped = _apply(rows, mapping)
    errors = [{"row": i + 2, "error": "missing name"}
              for i, d in enumerate(mapped) if not d.get("name")]
    return {"kind": body.kind, "headers": headers, "mapping": mapping,
            "rows": len(rows), "preview": mapped[:5],
            "error_count": len(errors), "errors": errors[:20]}


class RunIn(BaseModel):
    kind: str
    csv_text: str
    mapping: Optional[list] = None        # override of the auto-map
    save_profile_name: Optional[str] = None
    email_summary: bool = True


@router.post("/run")
async def run_import(body: RunIn, user: dict = Depends(require("ops:manage"))):
    if body.kind not in FIELDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(FIELDS)}")
    headers, rows = _parse(body.csv_text)
    mapping = body.mapping or auto_map(headers, body.kind)
    mapped = _apply(rows, mapping)
    coll = Scoped(getattr(db, COLLS[body.kind]), user["tenant_id"])
    existing = {(_norm(d.get("name"))) for d in await coll.find_list(limit=10000)}
    created, skipped, errors = 0, 0, []
    for i, d in enumerate(mapped):
        if not d.get("name"):
            errors.append({"row": i + 2, "error": "missing name"})
            continue
        if _norm(d["name"]) in existing:
            skipped += 1
            continue
        doc = {"id": new_id(), "created_at": now_iso(), "imported_via": "guided_importer"}
        if body.kind == "items":
            doc.update({"name": d["name"], "sku": d.get("sku", ""),
                        "type": d.get("kind", "service"), "revenue_code": "4020",
                        "price_cents": to_cents(d.get("price") or 0),
                        "unit_cost_cents": 0, "quantity_on_hand": 0,
                        "reorder_point": 0, "commissionable": True,
                        "image_url": None, "tax_category": "general_goods"})
        else:
            doc.update({"name": d["name"], "email": d.get("email", ""),
                        "phone": d.get("phone", ""), "address": d.get("address", "")})
        await coll.insert_one(doc)
        existing.add(_norm(d["name"]))
        created += 1
    if body.save_profile_name:
        await Scoped(db.import_profiles, user["tenant_id"]).insert_one({
            "id": new_id(), "name": body.save_profile_name, "kind": body.kind,
            "column_map": {m["csv"]: m["field"] for m in mapping if m.get("field")},
            "created_at": now_iso()})
    email_status = "not_requested"
    if body.email_summary and user.get("email"):
        status, _err = await send_email(
            user["email"], f"Import complete: {body.kind}",
            f"Your {body.kind} import finished.\n\n"
            f"Created: {created}\nDuplicates skipped: {skipped}\n"
            f"Errors: {len(errors)}\n")
        email_status = status
    return {"kind": body.kind, "created": created, "duplicates_skipped": skipped,
            "errors": errors[:50], "error_count": len(errors),
            "email_status": email_status}
