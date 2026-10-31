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
from lib.audit import append_audit
from lib.auth import require
from lib.dataclean import cleaning as _cleaning
from lib.dataclean import mess as _mess
from lib.dataclean import schema as _cleanschema
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


class CleanupPreviewIn(BaseModel):
    kind: str
    csv_text: str
    mapping: Optional[list] = None        # override of the auto-map


@router.post("/cleanup-preview")
async def cleanup_preview(body: CleanupPreviewIn,
                          user: dict = Depends(require("ops:manage"))):
    """Cleanup stage preview — shown BEFORE anything imports: detected
    column types, before/after normalization samples per field, the file
    mess report, and the schema check. Imports nothing."""
    if body.kind not in FIELDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(FIELDS)}")
    headers, rows = _parse(body.csv_text)
    mapping = body.mapping or auto_map(headers, body.kind)
    mapped = _apply(rows, mapping)
    column_types = {f: _mess.detect_column_type([d.get(f) for d in mapped])
                    for f in FIELDS[body.kind]}
    preview_changes, change_count = {}, 0
    for idx, d in enumerate(mapped[:50]):
        _, changes = _cleaning.clean_row(d, body.kind)
        for ch in changes:
            change_count += 1
            bucket = preview_changes.setdefault(ch["field"], [])
            if len(bucket) < 5:
                bucket.append({"row": idx + 2, **ch})
    sv = _cleanschema.validate_rows(mapped, body.kind)
    return {"kind": body.kind, "rows": len(mapped), "mapping": mapping,
            "column_types": column_types,
            "normalization_preview": preview_changes,
            "changes_in_first_50_rows": change_count,
            "mess_report": _mess.mess_report(mapped),
            "schema": {"blocking": sv["blocking"],
                       "row_errors": sv["row_errors"][:20],
                       "row_error_count": len(sv["row_errors"])},
            "can_import": not sv["blocking"]}


class RunIn(BaseModel):
    kind: str
    csv_text: str
    mapping: Optional[list] = None        # override of the auto-map
    save_profile_name: Optional[str] = None
    email_summary: bool = True
    cleanup: bool = True                  # dataclean stage before insert
    override_schema: bool = False         # import despite blocking checks (audited)
    override_reason: Optional[str] = None  # the human's stated reason; audited with the override


@router.post("/run")
async def run_import(body: RunIn, user: dict = Depends(require("ops:manage"))):
    if body.kind not in FIELDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(FIELDS)}")
    headers, rows = _parse(body.csv_text)
    mapping = body.mapping or auto_map(headers, body.kind)
    mapped = _apply(rows, mapping)
    cleanup_info = {"applied": False, "changes": 0, "changes_by_rule": {},
                    "schema_blocking": [],
                    "schema_override": False, "row_error_count": 0}
    if body.cleanup:
        cleaned, change_count = [], 0
        by_rule: dict = {}
        for d in mapped:
            cd, ch = _cleaning.clean_row(d, body.kind)
            cleaned.append(cd)
            change_count += len(ch)
            for c in ch:
                by_rule[c["rule"]] = by_rule.get(c["rule"], 0) + 1
        mapped = cleaned
        cleanup_info["changes_by_rule"] = by_rule
        sv = _cleanschema.validate_rows(mapped, body.kind)
        cleanup_info.update({"applied": True, "changes": change_count,
                             "schema_blocking": sv["blocking"],
                             "row_error_count": len(sv["row_errors"])})
        if sv["blocking"] and not body.override_schema:
            raise HTTPException(
                status_code=422,
                detail="This file failed its pre-import checks: "
                       + " ".join(sv["blocking"])
                       + " Fix the file, or re-run with override_schema=true "
                         "to import anyway (the override is recorded).")
        if sv["blocking"] and body.override_schema:
            cleanup_info["schema_override"] = True
            await append_audit(db, {
                "tenant_id": user["tenant_id"], "id": new_id(), "at": now_iso(),
                "user_id": user["id"], "action": "import.schema_override",
                "entity": body.kind, "entity_id": None,
                "details": {"blocking_errors": sv["blocking"],
                            "rows": len(mapped),
                            "reason": (body.override_reason or "").strip() or None}})
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
        if d.get("name_key"):
            doc["name_key"] = d["name_key"]
        if d.get("address_parts"):
            doc["address_parts"] = d["address_parts"]
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
            "email_status": email_status, "cleanup": cleanup_info}
