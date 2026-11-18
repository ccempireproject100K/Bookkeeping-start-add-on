"""Backups & data-out (Top-20 #20): full "download everything" export,
validated self-serve restore, scheduled backups, and restore tests.

Sits alongside — never replaces — the CPA package (routers/cpa.py) and the
legacy encrypted backup (routers/backup.py); both keep working untouched.
The heavy lifting lives in lib/fullexport.py.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from db import db
from lib import fullexport as FX
from lib.audit import audit
from lib.auth import require
from lib.memberships import memberships

router = APIRouter(prefix="/data-out", tags=["data-out"])

MAX_UPLOAD = 512 * 1024 * 1024


def _member_tenant_ids(user):
    return {m.get("tenant_id") for m in memberships(user)}


# ---------------- Full export ----------------

@router.get("/summary")
async def summary(user: dict = Depends(require("ledger:read"))):
    """Honest pre-download summary: exactly what the full export holds."""
    return await FX.export_summary(db, user["tenant_id"])


@router.get("/export.zip")
async def export_zip(user: dict = Depends(require("ledger:admin"))):
    tenant_id = user["tenant_id"]
    zip_bytes, manifest = await FX.build_full_export(db, tenant_id)
    await audit(user, "dataout.export", "workspace", tenant_id,
                {"documents": manifest["documents"],
                 "attachments": manifest["attachments"]["count"],
                 "bytes": len(zip_bytes)})
    fname = f"bookkeeping-full-export-{date.today().isoformat()}.zip"
    return Response(content=zip_bytes, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "X-Export-Documents": str(manifest["documents"])})


# ---------------- Stored backups & schedule ----------------

@router.get("/backups")
async def list_backups(user: dict = Depends(require("ledger:read"))):
    tenant_id = user["tenant_id"]
    # Opportunistic scheduling: if a scheduled backup is due, it is made
    # when the owner opens this list (plus the explicit run-due endpoint).
    await FX.maybe_run_scheduled(db, tenant_id)
    docs = await db.full_backups.find({"tenant_id": tenant_id},
                                      {"_id": 0}).to_list(length=1000)
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    settings = await FX.get_settings(db, tenant_id)
    return {"backups": docs, "settings": settings}


@router.post("/backups/now")
async def backup_now(user: dict = Depends(require("ledger:admin"))):
    doc = await FX.create_stored_backup(db, user["tenant_id"], kind="manual")
    await audit(user, "dataout.backup_now", "backup", doc["id"],
                {"documents": doc["documents"], "pruned": doc.get("pruned", 0)})
    doc.pop("tenant_id", None)
    return doc


@router.post("/backups/run-due")
async def run_due(user: dict = Depends(require("ledger:admin"))):
    doc = await FX.maybe_run_scheduled(db, user["tenant_id"])
    return {"created": doc is not None, "backup": doc}


@router.get("/backups/{bid}/download")
async def download_backup(bid: str, user: dict = Depends(require("ledger:read"))):
    tenant_id = user["tenant_id"]
    meta = await db.full_backups.find_one({"id": bid, "tenant_id": tenant_id},
                                          {"_id": 0})
    if not meta:
        raise HTTPException(404, "Backup not found")
    path = FX.BACKUPS_ROOT / tenant_id / f"{bid}.zip"
    if not path.exists():
        raise HTTPException(404, "That backup's file is missing from storage.")
    fname = f"bookkeeping-full-export-{str(meta.get('created_at') or '')[:10]}.zip"
    return Response(content=path.read_bytes(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/backups/{bid}/test")
async def test_backup(bid: str, user: dict = Depends(require("ledger:admin"))):
    result = await FX.test_stored_backup(db, user["tenant_id"], bid)
    if result is None:
        raise HTTPException(404, "Backup not found")
    await audit(user, "dataout.backup_test", "backup", bid,
                {"passed": result["passed"]})
    return result


class ScheduleBody(BaseModel):
    enabled: bool = False
    cadence_days: int = FX.DEFAULT_CADENCE_DAYS
    keep: int = FX.DEFAULT_KEEP


@router.put("/backups/settings")
async def put_settings(body: ScheduleBody,
                       user: dict = Depends(require("ledger:admin"))):
    tenant_id = user["tenant_id"]
    if body.cadence_days < 1 or body.cadence_days > 90:
        raise HTTPException(422, "Cadence must be between 1 and 90 days.")
    if body.keep < 1 or body.keep > 60:
        raise HTTPException(422, "Keep must be between 1 and 60 backups.")
    current = await FX.get_settings(db, tenant_id)
    doc = {**current, "tenant_id": tenant_id, "enabled": body.enabled,
           "cadence_days": body.cadence_days, "keep": body.keep}
    await db.backup_settings.update_one({"tenant_id": tenant_id},
                                        {"$set": doc}, upsert=True)
    if body.enabled:
        await FX.maybe_run_scheduled(db, tenant_id)
    await audit(user, "dataout.schedule", "backup_settings", tenant_id,
                {"enabled": body.enabled, "cadence_days": body.cadence_days,
                 "keep": body.keep})
    return await FX.get_settings(db, tenant_id)


# ---------------- Restore ----------------

async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(422, "That file is empty.")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(422, "That file is too large to be a full export.")
    return data


@router.post("/restore/validate")
async def validate_restore(file: UploadFile = File(...),
                           user: dict = Depends(require("ledger:admin"))):
    """Dry-run ONLY: validates the uploaded export and reports what would
    be restored. Writes nothing."""
    data_bytes = await _read_upload(file)
    ok, errors, manifest, _data, _att = FX.parse_and_validate(data_bytes)
    if manifest is not None and manifest.get("tenant_id") not in _member_tenant_ids(user):
        ok = False
        errors = errors + ["This export belongs to a workspace you do not "
                           "have access to. Restoring it is refused."]
    return {"ok": ok, "errors": errors,
            "manifest": ({k: manifest.get(k) for k in
                          ("tenant_id", "tenant_name", "exported_at",
                           "documents", "journal", "version")}
                         if manifest else None),
            "collections": (manifest or {}).get("collections")}


@router.post("/restore")
async def restore(file: UploadFile = File(...),
                  target_tenant_id: Optional[str] = Form(None),
                  confirm_text: Optional[str] = Form(None),
                  user: dict = Depends(require("ledger:admin"))):
    """Restore a full export. Validation first — any failure restores
    NOTHING. Restoring over a non-empty workspace requires typing that
    workspace's name exactly. Restoring into a workspace the user does
    not belong to is refused outright."""
    data_bytes = await _read_upload(file)
    ok, errors, manifest, data, attachments = FX.parse_and_validate(data_bytes)
    if not ok:
        raise HTTPException(422, "Restore refused — nothing was restored. "
                            + " ".join(errors))
    target = target_tenant_id or user["tenant_id"]
    if target not in _member_tenant_ids(user):
        raise HTTPException(403, "Restore refused: that workspace is not "
                                 "one of yours. Nothing was restored.")
    if manifest.get("tenant_id") != target:
        # An export may only ever land back in its own workspace (or a
        # scratch copy under restore-test). Cross-workspace restores would
        # smuggle one business's books into another.
        if manifest.get("tenant_id") not in _member_tenant_ids(user):
            raise HTTPException(403, "Restore refused: this export belongs "
                                     "to a different workspace. Nothing "
                                     "was restored.")
        raise HTTPException(422, "Restore refused: this export belongs to "
                                 f"workspace '{manifest.get('tenant_name') or manifest.get('tenant_id')}', "
                                 "not the selected one. Nothing was restored.")
    existing = await FX._tenant_doc_count(db, target)
    if existing > 0:
        tenant = await db.tenants.find_one({"id": target}, {"_id": 0}) or {}
        want = tenant.get("name") or target
        if (confirm_text or "").strip() != want:
            raise HTTPException(
                422, "Restore refused: this workspace already has data. To "
                     f"replace it, type the workspace name exactly: {want}")
    inserted = await FX.restore_into(db, target, data, attachments)
    ver = await FX.verify_against_manifest(db, target, manifest)
    if not ver["ok"]:
        # Roll back: a restore that cannot verify itself must not stand.
        await FX.wipe_tenant(db, target)
        await audit(user, "dataout.restore_failed", "workspace", target,
                    {"problems": ver["problems"]})
        raise HTTPException(
            500, "Restore FAILED verification and was rolled back — the "
                 "workspace was left empty rather than half-restored. "
                 + "; ".join(ver["problems"]))
    await audit(user, "dataout.restore", "workspace", target,
                {"documents": inserted, "verified": True})
    return {"ok": True, "restored_documents": inserted,
            "verification": {"documents": ver["documents"],
                             "journal": ver["journal"], "ok": True}}
