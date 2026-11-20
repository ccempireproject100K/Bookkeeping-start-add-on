"""Move in from QuickBooks (Top-20 gap #12).

Guided flow: drop QuickBooks Online CSV exports -> dry-run report (always
first; counts, unmapped rows with reasons, an adjustable account map, and a
trial-balance check) -> apply. Applying is idempotent via per-record source
fingerprints, refuses an unbalanced set outright (nothing half-posts), can
run as a one-click PRACTICE import into a clearly-labeled throwaway
workspace, and finishes with a DataClean (cleanup.py) tidy scan over the
imported data. Attachments can be bulk-matched onto imported records
afterwards; anything unmatched lands in an honest pile the user places by
hand — nothing is silently dropped.
"""
import base64
import os
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import require
from lib import ledger as L
from lib import qboimport as Q
from lib.coa import ACCOUNT_TYPE_OF

router = APIRouter(prefix="/migration", tags=["migration"])

UPLOAD_DIR = Path(__file__).parent / "uploads" / "migration"
TARGET_COLLECTIONS = ("accounts", "customers", "vendors", "products",
                      "journal_entries", "journal_lines", "fiscal_periods",
                      "audit_log")


def runs(user):
    return Scoped(db.migration_runs, user["tenant_id"])


def atts(user):
    return Scoped(db.migration_attachments, user["tenant_id"])


def _public_run(run):
    return {k: v for k, v in run.items() if k not in ("records", "_id")}


async def _get_run(user, run_id):
    run = await runs(user).find_one({"id": run_id})
    if not run:
        raise HTTPException(status_code=404, detail="Migration run not found")
    return run


# ---------------- request bodies ----------------
class FileIn(BaseModel):
    filename: str
    content: str


class DryRunBody(BaseModel):
    files: List[FileIn]


class MappingBody(BaseModel):
    account_map: List[dict]   # [{qbo_name, action: create|map, books_code}]


class ApplyBody(BaseModel):
    practice: bool = False


class AttachmentIn(BaseModel):
    filename: str
    content_base64: Optional[str] = None


class AttachmentsBody(BaseModel):
    files: List[AttachmentIn]


class PlaceBody(BaseModel):
    target_type: str          # journal_entry | customer | vendor
    target_id: str


# ---------------- dry run ----------------
@router.post("/qbo/dry-run")
async def qbo_dry_run(body: DryRunBody, user: dict = Depends(require("ops:manage"))):
    if not body.files:
        raise HTTPException(status_code=422, detail="Drop at least one QuickBooks export file")
    existing = await Scoped(db.accounts, user["tenant_id"]).find_list(limit=5000)
    report, records = Q.dry_run([f.model_dump() for f in body.files], existing,
                                code_universe=ACCOUNT_TYPE_OF)
    run = {
        "id": new_id(), "source": "quickbooks_online", "status": "dry_run",
        "report": report, "account_map": report["account_map"],
        "records": records, "created_at": now_iso(), "applied_at": None,
        "target_tenant": None, "tidy_findings": None, "apply_result": None,
    }
    await runs(user).insert_one(run)
    return {"run_id": run["id"], "status": run["status"], **report}


@router.get("/qbo/runs")
async def list_runs(user: dict = Depends(require("ledger:read"))):
    rows = await runs(user).find_list(sort=[("created_at", -1)], limit=100)
    return [_public_run(r) for r in rows]


@router.get("/qbo/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(require("ledger:read"))):
    return _public_run(await _get_run(user, run_id))


@router.post("/qbo/runs/{run_id}/mapping")
async def set_mapping(run_id: str, body: MappingBody,
                      user: dict = Depends(require("ops:manage"))):
    run = await _get_run(user, run_id)
    if run["status"] not in ("dry_run",):
        raise HTTPException(status_code=409, detail="This run was already applied; start a new dry run to change the mapping")
    known = {m["qbo_name"] for m in run["account_map"]}
    for m in body.account_map:
        if m.get("qbo_name") not in known:
            raise HTTPException(status_code=422, detail=f"Unknown QuickBooks account '{m.get('qbo_name')}'")
        if m.get("action") not in ("create", "map"):
            raise HTTPException(status_code=422, detail="action must be 'create' or 'map'")
        if not m.get("books_code"):
            raise HTTPException(status_code=422, detail="books_code is required for every mapping row")
    await runs(user).update_one({"id": run_id}, {"$set": {"account_map": body.account_map}})
    return {"run_id": run_id, "account_map": body.account_map}


# ---------------- apply ----------------
async def _apply(user, run, target_tenant):
    records = run.get("records") or {}
    amap = {m["qbo_name"].strip().lower(): m for m in run["account_map"]}
    # Preflight: every transaction line must resolve to a ledger-legal code
    # BEFORE anything is created, so a bad map never half-applies.
    prospective = {}
    for a in await Scoped(db.accounts, target_tenant).find_list(limit=5000):
        prospective[(a.get("name") or "").strip().lower()] = a["code"]
    for rec in records.get("accounts", []):
        m = amap.get(rec["qbo_name"].strip().lower())
        if m:
            prospective[rec["qbo_name"].strip().lower()] = m["books_code"]
    for e in records.get("transactions", []):
        for l in e["lines"]:
            code = prospective.get(l["account_name"].strip().lower())
            if not code:
                raise HTTPException(
                    status_code=422,
                    detail=(f"Account '{l['account_name']}' is not mapped. "
                            "Adjust the account map, then apply again — nothing was imported."))
            if code not in ACCOUNT_TYPE_OF:
                raise HTTPException(
                    status_code=422,
                    detail=(f"Account '{l['account_name']}' maps to Books code {code}, "
                            "which is not in the Books chart. Pick a chart code in "
                            "the account map — nothing was imported."))
    result = {"accounts_created": 0, "accounts_mapped": 0, "customers": 0,
              "vendors": 0, "items": 0, "journal_entries": 0,
              "skipped_existing": 0}

    accts = Scoped(db.accounts, target_tenant)
    name_to_code = {}
    for a in await accts.find_list(limit=5000):
        name_to_code[(a.get("name") or "").strip().lower()] = a["code"]

    for rec in records.get("accounts", []):
        m = amap.get(rec["qbo_name"].strip().lower())
        if not m:
            continue
        if m["action"] == "map":
            name_to_code[rec["qbo_name"].strip().lower()] = m["books_code"]
            result["accounts_mapped"] += 1
            continue
        existing = await accts.find_one({"code": m["books_code"]})
        if existing:
            name_to_code[rec["qbo_name"].strip().lower()] = existing["code"]
            result["skipped_existing"] += 1
            continue
        doc = {"id": new_id(), "code": m["books_code"], "name": rec["qbo_name"],
               "type": rec["type"],
               "normal_balance": "debit" if rec["type"] in ("asset", "expense") else "credit",
               "parent_code": None, "is_system": False, "archived": False,
               "description": rec.get("description") or f"Imported from QuickBooks ({rec.get('detail_type') or rec['qbo_type']})",
               "qbo_fingerprint": rec["fingerprint"], "migration_run_id": run["id"],
               "created_at": now_iso()}
        await accts.insert_one(doc)
        name_to_code[rec["qbo_name"].strip().lower()] = doc["code"]
        result["accounts_created"] += 1
    # Existing Books accounts referenced by name also resolve.
    for a in await accts.find_list(limit=5000):
        name_to_code.setdefault((a.get("name") or "").strip().lower(), a["code"])

    async def _import_people(kind, coll_name, extra=None):
        coll = Scoped(getattr(db, coll_name), target_tenant)
        n = 0
        for rec in records.get(kind, []):
            if await coll.find_one({"qbo_fingerprint": rec["fingerprint"]}):
                result["skipped_existing"] += 1
                continue
            doc = {"id": new_id(), "name": rec["name"], "email": rec.get("email"),
                   "phone": rec.get("phone"),
                   "qbo_fingerprint": rec["fingerprint"],
                   "migration_run_id": run["id"], "created_at": now_iso()}
            if coll_name == "customers":
                doc["locations"] = ([{"label": "Primary", "address": rec["address"]}]
                                    if rec.get("address") else [])
            if coll_name == "vendors":
                doc["address"] = rec.get("address")
                doc["is_1099"] = False
                doc["default_expense_code"] = "5700"
            await coll.insert_one(doc)
            n += 1
        result[kind] = n

    await _import_people("customers", "customers")
    await _import_people("vendors", "vendors")

    prods = Scoped(db.products, target_tenant)
    for rec in records.get("items", []):
        if await prods.find_one({"qbo_fingerprint": rec["fingerprint"]}):
            result["skipped_existing"] += 1
            continue
        rev_code = name_to_code.get((rec.get("income_account_name") or "").strip().lower(), "4100")
        doc = {"id": new_id(), "name": rec["name"],
               "type": "retail" if rec["qbo_type"].lower().startswith(("inventory", "non-inventory")) else "supply",
               "revenue_code": rev_code, "price_cents": rec["price_cents"],
               "unit_cost_cents": 0, "quantity_on_hand": 0, "reorder_point": 0,
               "commissionable": True, "image_url": None,
               "sku": rec.get("sku"), "qbo_fingerprint": rec["fingerprint"],
               "migration_run_id": run["id"], "created_at": now_iso()}
        await prods.insert_one(doc)
        result["items"] += 1

    entries_coll = Scoped(db.journal_entries, target_tenant)
    for e in records.get("transactions", []):
        idem = f"qbo:{e['fingerprint']}"
        if await entries_coll.find_one({"idempotency_key": idem}):
            result["skipped_existing"] += 1
            continue
        lines = []
        for l in e["lines"]:
            code = name_to_code.get(l["account_name"].strip().lower())
            if not code:
                raise HTTPException(
                    status_code=422,
                    detail=(f"Account '{l['account_name']}' is not mapped. "
                            "Adjust the account map, then apply again — nothing was posted for this entry."))
            lines.append({"account_code": code, "debit": l["debit"],
                          "credit": l["credit"], "memo": l.get("memo")})
        await L.post_journal_entry(
            db, target_tenant, user,
            {"entry_date": e["entry_date"], "source": "qbo_import",
             "source_ref": e["num"] or None,
             "memo": e.get("memo") or f"QuickBooks {e['txn_type']} import",
             "lines": lines},
            status="posted", idempotency_key=idem)
        result["journal_entries"] += 1
    return result


@router.post("/qbo/runs/{run_id}/apply")
async def qbo_apply(run_id: str, body: ApplyBody,
                    user: dict = Depends(require("ledger:admin"))):
    run = await _get_run(user, run_id)
    tb = (run.get("report") or {}).get("trial_balance") or {}
    if not tb.get("balanced"):
        raise HTTPException(
            status_code=409,
            detail=("This set does not balance — imported debits must equal "
                    f"credits (difference ${abs(tb.get('difference_cents', 0))/100:,.2f}). "
                    "Nothing was posted. Fix the export or the mapping and dry-run again."))
    target = (f"practice:{user['tenant_id']}:{run_id}" if body.practice
              else user["tenant_id"])
    result = await _apply(user, run, target)
    # DataClean hook: scan the target workspace and surface the tidy list.
    tidy = None
    try:
        from routers.cleanup import _scan
        issues = await _scan({"tenant_id": target, "id": user.get("id"),
                              "role": "owner", "email": user.get("email")})
        by_type = {}
        for i in issues:
            by_type[i["type"]] = by_type.get(i["type"], 0) + 1
        tidy = {"count": len(issues), "by_type": by_type,
                "note": "DataClean findings over the imported data — review them in Cleanup."}
    except Exception as exc:  # scan is advisory; the import itself succeeded
        tidy = {"count": None, "error": str(exc)}
    status = "applied_practice" if body.practice else "applied"
    await runs(user).update_one({"id": run_id}, {"$set": {
        "status": status, "applied_at": now_iso(), "target_tenant": target,
        "apply_result": result, "tidy_findings": tidy}})
    return {"run_id": run_id, "status": status, "target_tenant": target,
            "result": result, "tidy_findings": tidy}


# ---------------- practice ----------------
@router.get("/qbo/practice-files")
async def practice_files(user: dict = Depends(require("ledger:read"))):
    return {"files": Q.SAMPLE_FILES,
            "note": "A small, clearly fake QuickBooks export set. Run it as a practice import to rehearse the whole move — it lands in a throwaway workspace and never touches your real books."}


@router.post("/qbo/practice-run")
async def practice_run(user: dict = Depends(require("ledger:admin"))):
    run_id = new_id()
    practice_tenant = f"practice:{user['tenant_id']}:{run_id}"
    # The dry run is computed against the (empty) practice workspace, never
    # the real books — the practice tenant starts blank by construction.
    existing = await Scoped(db.accounts, practice_tenant).find_list(limit=5000)
    report, records = Q.dry_run(Q.SAMPLE_FILES, existing,
                                code_universe=ACCOUNT_TYPE_OF)
    if not report["trial_balance"]["balanced"]:
        raise HTTPException(status_code=500, detail="Bundled practice set failed its own balance check")
    run = {"id": run_id, "source": "quickbooks_online_practice",
           "status": "dry_run", "report": report,
           "account_map": report["account_map"], "records": records,
           "created_at": now_iso(), "applied_at": None, "target_tenant": None,
           "tidy_findings": None, "apply_result": None}
    await runs(user).insert_one(run)
    return await qbo_apply(run_id, ApplyBody(practice=True), user)


@router.delete("/qbo/practice/{run_id}")
async def discard_practice(run_id: str, user: dict = Depends(require("ledger:admin"))):
    run = await _get_run(user, run_id)
    target = run.get("target_tenant") or ""
    if not target.startswith("practice:"):
        raise HTTPException(status_code=400, detail="Only a practice workspace can be discarded")
    deleted = {}
    for name in TARGET_COLLECTIONS:
        res = await getattr(db, name).delete_many({"tenant_id": target})
        deleted[name] = res.deleted_count
    await db.counters.delete_many({"tenant_id": target})
    await runs(user).update_one({"id": run_id}, {"$set": {"status": "practice_discarded"}})
    return {"run_id": run_id, "discarded_tenant": target, "deleted": deleted}


# ---------------- attachments ----------------
def _match_attachment(filename, entries, people):
    """Match by record number in the filename, then by a person/vendor name."""
    tokens = re.findall(r"[A-Za-z]+|\d+", filename)
    numbers = [t for t in tokens if t.isdigit()]
    for e in entries:
        ref = str(e.get("source_ref") or "")
        if ref and (ref in numbers or ref.lower() in filename.lower()):
            return {"type": "journal_entry", "id": e["id"],
                    "label": f"Journal entry {e.get('number') or ref} ({e.get('entry_date')})",
                    "via": "record number"}
    low = filename.lower()
    for p in people:
        name = (p.get("name") or "").strip()
        if name and (name.lower() in low or
                     any(len(w) > 3 and w in low for w in name.lower().split())):
            return {"type": p["_kind"], "id": p["id"], "label": name, "via": "name in filename"}
    return None


@router.post("/qbo/runs/{run_id}/attachments")
async def add_attachments(run_id: str, body: AttachmentsBody,
                          user: dict = Depends(require("ops:manage"))):
    run = await _get_run(user, run_id)
    target = run.get("target_tenant") or user["tenant_id"]
    entries = await Scoped(db.journal_entries, target).find_list(
        {"migration_run_id": run_id}, limit=5000) or \
        await Scoped(db.journal_entries, target).find_list({"source": "qbo_import"}, limit=5000)
    people = []
    for coll_name, kind in (("customers", "customer"), ("vendors", "vendor")):
        for p in await Scoped(getattr(db, coll_name), target).find_list(
                {"migration_run_id": run_id}, limit=5000):
            people.append({**p, "_kind": kind})
    out = []
    for f in body.files:
        stored_path = None
        if f.content_base64:
            try:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                raw = base64.b64decode(f.content_base64)
                safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_",
                                   os.path.basename(f.filename or ""))
                stored_path = str(UPLOAD_DIR / f"{run_id}-{safe_name}")
                with open(stored_path, "wb") as fh:
                    fh.write(raw)
            except Exception:
                stored_path = None
        match = _match_attachment(f.filename, entries, people)
        doc = {"id": new_id(), "run_id": run_id, "target_tenant": target,
               "filename": f.filename, "stored_path": stored_path,
               "status": "attached" if match else "unmatched",
               "matched": match, "created_at": now_iso()}
        await atts(user).insert_one(doc)
        out.append({k: v for k, v in doc.items() if k != "_id"})
    summary = {"attached": sum(1 for d in out if d["status"] == "attached"),
               "unmatched": sum(1 for d in out if d["status"] == "unmatched"),
               "total": len(out)}
    return {"run_id": run_id, "summary": summary, "attachments": out}


@router.get("/qbo/runs/{run_id}/attachments")
async def list_attachments(run_id: str, user: dict = Depends(require("ledger:read"))):
    await _get_run(user, run_id)
    rows = await atts(user).find_list({"run_id": run_id}, sort=[("created_at", 1)], limit=5000)
    return {"attached": [r for r in rows if r["status"] == "attached"],
            "unmatched": [r for r in rows if r["status"] == "unmatched"]}


@router.post("/qbo/attachments/{att_id}/place")
async def place_attachment(att_id: str, body: PlaceBody,
                           user: dict = Depends(require("ops:manage"))):
    att = await atts(user).find_one({"id": att_id})
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    coll_name = {"journal_entry": "journal_entries", "customer": "customers",
                 "vendor": "vendors"}.get(body.target_type)
    if not coll_name:
        raise HTTPException(status_code=422, detail="target_type must be journal_entry, customer, or vendor")
    target = att.get("target_tenant") or user["tenant_id"]
    doc = await Scoped(getattr(db, coll_name), target).find_one({"id": body.target_id})
    if not doc:
        raise HTTPException(status_code=404, detail="That record was not found in this workspace")
    match = {"type": body.target_type, "id": body.target_id,
             "label": doc.get("name") or doc.get("number") or body.target_id,
             "via": "placed by hand"}
    await atts(user).update_one({"id": att_id},
                                {"$set": {"status": "attached", "matched": match}})
    return {"id": att_id, "status": "attached", "matched": match}


# ---------------- where did my reports go ----------------
@router.get("/reports-map")
async def reports_map(user: dict = Depends(require("ledger:read"))):
    return {"reports": Q.REPORTS_MAP,
            "note": "Your QuickBooks reports did not disappear — each one lives here now, reading the same numbers you imported."}
