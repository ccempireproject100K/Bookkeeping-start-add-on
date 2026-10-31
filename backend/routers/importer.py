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
import hashlib
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
from lib.dataclean.cleaning import KNOWN_RULES
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


# ---------------- saved profiles + learned cleanup rules ----------------
def _profiles(user):
    return Scoped(db.import_profiles, user["tenant_id"])


async def _get_profile(user, profile_id, kind):
    """The tenant's own guided-import profile for this kind, or 404.
    Only profiles saved by this importer carry a `kind`; the bank
    importer's profiles share the collection but are a different shape
    and are never touched here."""
    doc = await _profiles(user).find_one({"id": profile_id})
    if not doc or doc.get("kind") != kind:
        raise HTTPException(status_code=404, detail="import profile not found")
    return doc


def _profile_mapping(profile) -> list:
    """Stored column_map is {csv_header: field}; the importer mapping
    shape is [{csv, field}]."""
    return [{"csv": csv_col, "field": field}
            for csv_col, field in (profile.get("column_map") or {}).items()
            if field]


def _rule_states(profile, overrides):
    """Effective per-rule state for a run: every known rule defaults ON;
    the profile's learned decisions sit underneath; the caller's
    per-run overrides win. Returns (states, disabled) where states is
    {rule: {"enabled": bool, "source": "default"|"profile"|"run"}} —
    the preview shows the user exactly which layer decided each rule."""
    learned = (profile or {}).get("cleanup_rules") or {}
    states, disabled = {}, set()
    for rule in KNOWN_RULES:
        enabled, source = True, "default"
        if rule in learned:
            enabled, source = bool(learned[rule]), "profile"
        if overrides and rule in overrides:
            enabled, source = bool(overrides[rule]), "run"
        states[rule] = {"enabled": enabled, "source": source}
        if not enabled:
            disabled.add(rule)
    return states, disabled


def _validate_rule_map(rules, what="cleanup_rules"):
    if rules is None:
        return {}
    if not isinstance(rules, dict):
        raise HTTPException(status_code=422, detail=f"{what} must be an object")
    bad = [r for r in rules if r not in KNOWN_RULES]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"unknown cleanup rule(s) in {what}: {', '.join(sorted(bad))}")
    return {r: bool(v) for r, v in rules.items()}


@router.get("/profiles")
async def list_profiles(user: dict = Depends(require("ops:manage"))):
    rows = await _profiles(user).find_list(sort=[("name", 1)], limit=200)
    guided = [r for r in rows if r.get("kind")]
    for r in guided:
        r["cleanup_rules"] = r.get("cleanup_rules") or {}
    return {"profiles": guided}


@router.get("/runs")
async def list_import_runs(user: dict = Depends(require("ops:manage"))):
    """Recent import runs for this tenant, newest first — the durable
    record behind the dashboard's files-cleaned metric."""
    runs = await Scoped(db.import_runs, user["tenant_id"]).find_list(
        sort=[("created_at", -1)], limit=50)
    return {"runs": runs}


class CleanupRulesIn(BaseModel):
    cleanup_rules: dict


@router.put("/profiles/{profile_id}/cleanup-rules")
async def set_profile_cleanup_rules(profile_id: str, body: CleanupRulesIn,
                                    user: dict = Depends(require("ops:manage"))):
    """Explicit, inspectable learning: the profile page's per-rule
    switches write here. Merges over the profile's current decisions;
    unknown rule names are rejected, never silently stored."""
    profile = await _profiles(user).find_one({"id": profile_id})
    if not profile or not profile.get("kind"):
        raise HTTPException(status_code=404, detail="import profile not found")
    merged = dict(profile.get("cleanup_rules") or {})
    merged.update(_validate_rule_map(body.cleanup_rules))
    await _profiles(user).update_one({"id": profile_id}, {"$set": {
        "cleanup_rules": merged, "cleanup_rules_updated_at": now_iso(),
        "cleanup_rules_updated_by": user["id"]}})
    await append_audit(db, {
        "tenant_id": user["tenant_id"], "id": new_id(), "at": now_iso(),
        "user_id": user["id"], "action": "import.profile_cleanup_rules",
        "entity": "import_profiles", "entity_id": profile_id,
        "details": {"cleanup_rules": merged}})
    out = await _profiles(user).find_one({"id": profile_id})
    out["cleanup_rules"] = out.get("cleanup_rules") or {}
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
    profile_id: Optional[str] = None      # saved profile: mapping + learned rules
    rule_overrides: Optional[dict] = None  # per-run rule toggles (win over the profile)


async def _resolve(body, user):
    """Shared preview/run resolution: (profile, mapping, states, disabled)."""
    profile = None
    if getattr(body, "profile_id", None):
        profile = await _get_profile(user, body.profile_id, body.kind)
    if body.mapping:
        mapping = body.mapping
    elif profile:
        mapping = _profile_mapping(profile) or auto_map(
            _parse(body.csv_text)[0], body.kind)
    else:
        mapping = auto_map(_parse(body.csv_text)[0], body.kind)
    states, disabled = _rule_states(
        profile, _validate_rule_map(body.rule_overrides, "rule_overrides")
        if getattr(body, "rule_overrides", None) else None)
    return profile, mapping, states, disabled


def _bucket(changes_with_rows):
    out = {}
    for row_no, ch in changes_with_rows:
        bucket = out.setdefault(ch["field"], [])
        if len(bucket) < 5:
            bucket.append({"row": row_no, **ch})
    return out


@router.post("/cleanup-preview")
async def cleanup_preview(body: CleanupPreviewIn,
                          user: dict = Depends(require("ops:manage"))):
    """Cleanup stage preview — shown BEFORE anything imports: detected
    column types, before/after normalization samples per field, the file
    mess report, and the schema check. With a saved profile, its learned
    rule decisions apply: suppressed changes are reported as HELD (same
    shape, never applied), and `rule_states` names the layer that
    decided each rule. Imports nothing."""
    if body.kind not in FIELDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(FIELDS)}")
    _headers, rows = _parse(body.csv_text)
    profile, mapping, states, disabled = await _resolve(body, user)
    mapped = _apply(rows, mapping)
    column_types = {f: _mess.detect_column_type([d.get(f) for d in mapped])
                    for f in FIELDS[body.kind]}
    applied, held = [], []
    for idx, d in enumerate(mapped[:50]):
        _, ch, hd = _cleaning.clean_row_full(d, body.kind, disabled)
        applied += [(idx + 2, c) for c in ch]
        held += [(idx + 2, c) for c in hd]
    sv = _cleanschema.validate_rows(mapped, body.kind)
    return {"kind": body.kind, "rows": len(mapped), "mapping": mapping,
            "column_types": column_types,
            "normalization_preview": _bucket(applied),
            "changes_in_first_50_rows": len(applied),
            "held_changes": _bucket(held),
            "held_in_first_50_rows": len(held),
            "rule_states": states,
            "profile": ({"id": profile["id"], "name": profile.get("name")}
                        if profile else None),
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
    profile_id: Optional[str] = None      # saved profile: mapping + learned rules
    rule_overrides: Optional[dict] = None  # per-run rule toggles (win over the profile)
    learn_rules: bool = False        # write rule_overrides back into the profile
    file_name: Optional[str] = None  # original file name, kept on the run record


@router.post("/run")
async def _persist_import_run(user, body, run_id, status, mapped_count,
                              created, skipped, error_count, cleanup_info):
    """One durable row per import attempt that reached the cleanup
    stage (Phase 2): this is what makes the dashboard's "files cleaned"
    metric real. Blocked attempts are recorded too, as `blocked` —
    they are evidence, but never counted as cleaned files."""
    await Scoped(db.import_runs, user["tenant_id"]).insert_one({
        "id": run_id, "kind": body.kind, "status": status,
        "file_name": body.file_name,
        "file_hash": hashlib.sha256(body.csv_text.encode("utf-8")).hexdigest(),
        "rows_total": mapped_count, "rows_created": created,
        "rows_skipped": skipped, "rows_errors": error_count,
        "cleanup_applied": bool(cleanup_info.get("applied")),
        "changes": cleanup_info.get("changes", 0),
        "changes_by_rule": cleanup_info.get("changes_by_rule", {}),
        "held_changes": cleanup_info.get("held_changes", 0),
        "schema_blocking": cleanup_info.get("schema_blocking", []),
        "schema_override": bool(cleanup_info.get("schema_override")),
        "override_reason": ((body.override_reason or "").strip() or None)
        if cleanup_info.get("schema_override") else None,
        "profile_id": body.profile_id,
        "profile_name": cleanup_info.get("profile_name"),
        "created_at": now_iso(), "created_by": user["id"]})


async def run_import(body: RunIn, user: dict = Depends(require("ops:manage"))):
    if body.kind not in FIELDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(FIELDS)}")
    _headers, rows = _parse(body.csv_text)
    profile, mapping, states, disabled = await _resolve(body, user)
    mapped = _apply(rows, mapping)
    run_id = new_id()
    cleanup_info = {"applied": False, "changes": 0, "changes_by_rule": {},
                    "held_changes": 0, "held_by_rule": {},
                    "rules_applied": [], "rules_held": [],
                    "rule_states": states,
                    "profile_name": profile.get("name") if profile else None,
                    "schema_blocking": [],
                    "schema_override": False, "row_error_count": 0}
    if body.cleanup:
        cleaned, change_count, held_count = [], 0, 0
        by_rule: dict = {}
        held_by_rule: dict = {}
        for d in mapped:
            cd, ch, hd = _cleaning.clean_row_full(d, body.kind, disabled)
            cleaned.append(cd)
            change_count += len(ch)
            held_count += len(hd)
            for c in ch:
                by_rule[c["rule"]] = by_rule.get(c["rule"], 0) + 1
            for c in hd:
                held_by_rule[c["rule"]] = held_by_rule.get(c["rule"], 0) + 1
        mapped = cleaned
        cleanup_info["changes_by_rule"] = by_rule
        cleanup_info["held_by_rule"] = held_by_rule
        cleanup_info["rules_applied"] = sorted(by_rule)
        cleanup_info["rules_held"] = sorted(held_by_rule)
        sv = _cleanschema.validate_rows(mapped, body.kind)
        cleanup_info.update({"applied": True, "changes": change_count,
                             "held_changes": held_count,
                             "schema_blocking": sv["blocking"],
                             "row_error_count": len(sv["row_errors"])})
        if sv["blocking"] and not body.override_schema:
            await _persist_import_run(user, body, run_id, "blocked",
                                      len(mapped), 0, 0,
                                      len(sv["row_errors"]), cleanup_info)
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
    if body.learn_rules and profile and body.rule_overrides:
        # Explicit learning only: the user ticked "save these choices"
        # AND made explicit per-rule choices this run. Defaults that
        # merely happened to apply are never learned.
        learned = dict(profile.get("cleanup_rules") or {})
        learned.update(_validate_rule_map(body.rule_overrides, "rule_overrides"))
        await _profiles(user).update_one({"id": profile["id"]}, {"$set": {
            "cleanup_rules": learned,
            "cleanup_rules_updated_at": now_iso(),
            "cleanup_rules_updated_by": user["id"]}})
        await append_audit(db, {
            "tenant_id": user["tenant_id"], "id": new_id(), "at": now_iso(),
            "user_id": user["id"], "action": "import.profile_learn",
            "entity": "import_profiles", "entity_id": profile["id"],
            "details": {"cleanup_rules": learned, "via_run": run_id}})
    await _persist_import_run(user, body, run_id, "completed", len(mapped),
                              created, skipped, len(errors), cleanup_info)
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
            "email_status": email_status, "cleanup": cleanup_info,
            "import_run_id": run_id}
