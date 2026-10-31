"""DataClean external API (Phase 3) — /api/v1/dataclean/*.

The standalone, versioned product surface over the SAME framework-free
engine the internal console uses (lib/dataclean/). Nothing here forks
the engine or its shapes:

* cleanup preview returns the internal /import/cleanup-preview payload
  (same keys, all rules default-on, no profile layer);
* cleanup clean returns clean_row's records + explainable changes;
* normalize returns the engine's per-value result dicts VERBATIM —
  the versioned `dataclean.result/1` shape, untouched;
* dedupe scan returns linkage's pairs with the same evidence dicts
  the internal /dedupe stores.

Auth is a DataClean API key (lib/dataclean_api.py): the tenant comes
from the key record, never from the request. Every compute call is
rate-limited per key and metered (one dataclean_api_usage row per
call) — metering is a foundation only; nothing here charges anything.

The playground endpoint is the one unauthenticated route: it runs the
real engine on a small file under hard caps and writes NOTHING to any
collection — no prospect data is persisted, by construction (the
handler never touches the database).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db import db
from lib import dataclean_api as dca
from lib.base import Scoped
from lib.dataclean import RESULT_SHAPE
from lib.dataclean import cleaning as _cleaning
from lib.dataclean import linkage as _linkage
from lib.dataclean import mess as _mess
from lib.dataclean import normalize as _normalize
from lib.dataclean import addresses as _addresses
from lib.dataclean import schema as _cleanschema
from routers.importer import FIELDS, _apply, _bucket, _parse, auto_map

router = APIRouter(prefix="/v1/dataclean", tags=["dataclean-api"])

MAX_ROWS = 5000          # clean/preview payload ceiling (engine ~1 ms/row)
MAX_DEDUPE_RECORDS = 500  # linkage over submitted records
MAX_NORMALIZE_VALUES = 1000

ENDPOINTS = [
    {"method": "POST", "path": "/api/v1/dataclean/cleanup/preview",
     "auth": "api_key", "metered": True},
    {"method": "POST", "path": "/api/v1/dataclean/cleanup/clean",
     "auth": "api_key", "metered": True},
    {"method": "POST", "path": "/api/v1/dataclean/dedupe/scan",
     "auth": "api_key", "metered": True},
    {"method": "POST", "path": "/api/v1/dataclean/normalize",
     "auth": "api_key", "metered": True},
    {"method": "GET", "path": "/api/v1/dataclean/usage",
     "auth": "api_key", "metered": False},
    {"method": "GET", "path": "/api/v1/dataclean/pricing",
     "auth": "none", "metered": False},
    {"method": "POST", "path": "/api/v1/dataclean/playground/preview",
     "auth": "none", "metered": False},
]


def _err(status, code, message, **extra):
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status, detail=detail)


# ------------------------------------------------------------ payload glue

def _mapped_rows(kind: str, rows, csv_text, mapping):
    """(mapped_rows, mapping_used) from either payload form.

    rows: list of dicts already keyed by our field names (strict —
    an unknown field is a 422, never silently dropped).
    csv_text: parsed + auto-mapped exactly like the internal importer.
    """
    if kind not in FIELDS:
        raise _err(422, "unknown_kind",
                   f"kind must be one of {sorted(FIELDS)}")
    if rows is not None and csv_text is not None:
        raise _err(422, "invalid_payload",
                   "send either rows or csv_text, not both")
    if rows is not None:
        if not isinstance(rows, list) or not all(
                isinstance(r, dict) for r in rows):
            raise _err(422, "invalid_payload", "rows must be a list of objects")
        allowed = set(FIELDS[kind])
        mapped = []
        for r in rows:
            unknown = sorted(set(r) - allowed)
            if unknown:
                raise _err(422, "unknown_field",
                           f"unknown field(s) for kind '{kind}': "
                           f"{', '.join(unknown)}",
                           allowed_fields=sorted(allowed))
            doc = {}
            for k, v in r.items():
                if v is None:
                    continue
                s = str(v).strip()
                if s:
                    doc[k] = s
            mapped.append(doc)
        identity = [{"csv": f, "field": f, "confidence": 1.0}
                    for f in FIELDS[kind] if any(f in r for r in rows)]
        return mapped, identity
    if csv_text is not None:
        _headers, raw = _parse(csv_text)
        used = mapping or auto_map(_headers, kind)
        return _apply(raw, used), used
    raise _err(422, "invalid_payload", "send rows or csv_text")


def _preview_payload(kind, mapped, mapping):
    """The internal cleanup-preview computation, verbatim keys."""
    column_types = {f: _mess.detect_column_type([d.get(f) for d in mapped])
                    for f in FIELDS[kind]}
    applied = []
    for idx, d in enumerate(mapped[:50]):
        _, ch = _cleaning.clean_row(d, kind)
        applied += [(idx + 2, c) for c in ch]
    sv = _cleanschema.validate_rows(mapped, kind)
    states = {rule: {"enabled": True, "source": "default"}
              for rule in _cleaning.KNOWN_RULES}
    return {"kind": kind, "rows": len(mapped), "mapping": mapping,
            "column_types": column_types,
            "normalization_preview": _bucket(applied),
            "changes_in_first_50_rows": len(applied),
            "held_changes": {}, "held_in_first_50_rows": 0,
            "rule_states": states, "profile": None,
            "mess_report": _mess.mess_report(mapped),
            "schema": {"blocking": sv["blocking"],
                       "row_errors": sv["row_errors"][:20],
                       "row_error_count": len(sv["row_errors"])},
            "can_import": not sv["blocking"]}


class CleanupIn(BaseModel):
    kind: str
    rows: Optional[list] = None
    csv_text: Optional[str] = None
    mapping: Optional[list] = None


async def _compute(actor, endpoint, body: CleanupIn, fn):
    mapped, mapping = _mapped_rows(body.kind, body.rows, body.csv_text,
                                   body.mapping)
    if len(mapped) > MAX_ROWS:
        raise _err(413, "payload_too_large",
                   f"at most {MAX_ROWS} rows per call",
                   max_rows=MAX_ROWS)
    await dca.enforce_limits(actor, endpoint, len(mapped))
    out = fn(body.kind, mapped, mapping)
    out["shape"] = RESULT_SHAPE
    out["api_version"] = "v1"
    await dca.record_usage(actor, endpoint, len(mapped), "ok")
    return out


@router.post("/cleanup/preview")
async def cleanup_preview(body: CleanupIn,
                          actor: dict = Depends(dca.get_dataclean_actor)):
    """Mess report + before/after samples + schema verdict for a file
    or row set. Cleans nothing, stores nothing of the caller's data —
    the only write is the usage-metering row."""
    return await _compute(actor, "cleanup/preview", body, _preview_payload)


@router.post("/cleanup/clean")
async def cleanup_clean(body: CleanupIn,
                        actor: dict = Depends(dca.get_dataclean_actor)):
    """The cleaned records + every change, explainable per field."""
    def _clean(kind, mapped, _mapping):
        records, changes = [], []
        for idx, d in enumerate(mapped):
            cleaned, ch = _cleaning.clean_row(d, kind)
            records.append(cleaned)
            changes += [{"row": idx + 2, **c} for c in ch]
        by_rule = {}
        for c in changes:
            by_rule[c["rule"]] = by_rule.get(c["rule"], 0) + 1
        return {"kind": kind, "rows": len(mapped), "records": records,
                "changes": changes, "changes_by_rule": by_rule}
    return await _compute(actor, "cleanup/clean", body, _clean)


class DedupeIn(BaseModel):
    records: list


@router.post("/dedupe/scan")
async def dedupe_scan(body: DedupeIn,
                      actor: dict = Depends(dca.get_dataclean_actor)):
    """Duplicate-candidate pairs over SUBMITTED records, with the
    engine's evidence shape ({name_similarity, email_match,
    phone_match, address_similarity, why}). Suggestions only — this
    surface never merges anything."""
    records = body.records
    if not all(isinstance(r, dict) for r in records):
        raise _err(422, "invalid_payload", "records must be a list of objects")
    if len(records) > MAX_DEDUPE_RECORDS:
        raise _err(413, "payload_too_large",
                   f"at most {MAX_DEDUPE_RECORDS} records per scan",
                   max_records=MAX_DEDUPE_RECORDS)
    entities = []
    for i, r in enumerate(records):
        e = {k: (str(v).strip() if v is not None else None)
             for k, v in r.items()}
        e["id"] = str(r.get("id") or f"row-{i + 1}")
        entities.append(e)
    await dca.enforce_limits(actor, "dedupe/scan", len(entities))
    result = _linkage.suggest_duplicates(entities)
    await dca.record_usage(actor, "dedupe/scan", len(entities), "ok")
    return {"shape": RESULT_SHAPE, "api_version": "v1",
            "records": len(entities), "engine": result["engine"],
            "engine_note": result.get("engine_note"),
            "pairs": result["pairs"]}


class NormalizeIn(BaseModel):
    type: str
    values: list


_NORMALIZERS = {
    "date": _normalize.normalize_date,
    "phone": _normalize.normalize_phone,
    "address": _addresses.parse_address,
}


@router.post("/normalize")
async def normalize(body: NormalizeIn,
                    actor: dict = Depends(dca.get_dataclean_actor)):
    """Per-value normalization, returning the engine's result dicts
    verbatim (shape dataclean.result/1): {shape, original, normalized,
    ok, rule, confidence, notes}. Unparseable values come back with
    ok=false and the original preserved — never invented."""
    fn = _NORMALIZERS.get(body.type)
    if fn is None:
        raise _err(422, "unknown_type",
                   f"type must be one of {sorted(_NORMALIZERS)}")
    if len(body.values) > MAX_NORMALIZE_VALUES:
        raise _err(413, "payload_too_large",
                   f"at most {MAX_NORMALIZE_VALUES} values per call",
                   max_values=MAX_NORMALIZE_VALUES)
    await dca.enforce_limits(actor, "normalize", len(body.values))
    results = [fn(v) for v in body.values]
    await dca.record_usage(actor, "normalize", len(body.values), "ok")
    return {"shape": RESULT_SHAPE, "api_version": "v1", "type": body.type,
            "results": results}


@router.get("/usage")
async def own_usage(actor: dict = Depends(dca.get_dataclean_actor)):
    """This key's own usage, counted from the durable metering rows."""
    rows = await Scoped(db.dataclean_api_usage, actor["tenant_id"]).find_list(
        {"key_id": actor["api_key_id"]}, limit=100000)
    by_status, by_endpoint = {}, {}
    month_rows = 0
    from datetime import datetime, timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_endpoint[r["endpoint"]] = by_endpoint.get(r["endpoint"], 0) + 1
        if r["status"] == "ok" and (r.get("created_at") or "")[:7] == month:
            month_rows += r.get("rows_processed", 0)
    return {"key": dca.public_key_view(actor["_key_doc"]),
            "calls_recorded": len(rows), "by_status": by_status,
            "by_endpoint": by_endpoint, "rows_this_month": month_rows}


@router.get("/pricing")
async def pricing():
    """Public pricing/plan config for the storefront. Values are
    configuration (placeholders until the owner sets real prices);
    nothing is sold or charged by this endpoint."""
    return dca.pricing_config()


@router.get("/")
async def surface_index():
    return {"name": "DataClean API", "version": "v1",
            "shape": RESULT_SHAPE, "endpoints": ENDPOINTS}


# --------------------------------------------------------------- playground

class PlaygroundIn(BaseModel):
    kind: str
    csv_text: str


@router.post("/playground/preview")
async def playground_preview(body: PlaygroundIn, request: Request):
    """Public try-a-file demo. Runs the REAL engine (same preview
    computation as the keyed surface) on a small file under hard caps.
    Writes NOTHING: no collection is touched in this handler — the
    prospect's rows exist only for this request. Rate-limited per
    client IP."""
    caps = dca.playground_caps()
    ip = (request.client.host if request.client else "unknown")
    allowed, retry, _ = dca.playground_limiter.check(
        f"playground:{ip}", caps["requests_per_window"],
        caps["window_seconds"])
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited",
                    "message": "Demo rate limit reached — try again shortly",
                    "retry_after_seconds": retry},
            headers={"Retry-After": str(retry)})
    if body.kind not in FIELDS:
        raise _err(422, "unknown_kind",
                   f"kind must be one of {sorted(FIELDS)}")
    if len(body.csv_text.encode("utf-8")) > caps["max_file_bytes"]:
        raise _err(413, "playground_limit",
                   f"demo files are capped at {caps['max_file_bytes']} bytes",
                   caps=caps)
    headers, raw = _parse(body.csv_text)
    if len(raw) > caps["max_rows"]:
        raise _err(413, "playground_limit",
                   f"demo files are capped at {caps['max_rows']} rows",
                   caps=caps)
    if len(headers) > caps["max_columns"]:
        raise _err(413, "playground_limit",
                   f"demo files are capped at {caps['max_columns']} columns",
                   caps=caps)
    mapping = auto_map(headers, body.kind)
    mapped = _apply(raw, mapping)
    out = _preview_payload(body.kind, mapped, mapping)
    return {"demo": True, "persisted": False, "caps": caps,
            "shape": RESULT_SHAPE, "api_version": "v1",
            "kind": out["kind"], "rows": out["rows"],
            "mess_report": out["mess_report"],
            "column_types": out["column_types"],
            "normalization_preview": out["normalization_preview"],
            "changes_in_first_50_rows": out["changes_in_first_50_rows"],
            "schema": out["schema"], "can_import": out["can_import"]}
