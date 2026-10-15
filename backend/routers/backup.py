"""Backup & restore: tenant-scoped JSON dump of the main business collections as a
downloadable file; restore replaces the tenant's data in those collections.
Users, auth sessions, counters and the audit log are deliberately excluded —
restoring must never lock the workspace out or rewrite history."""
import json
from datetime import date
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from db import db
from lib.base import new_id, now_iso
from lib.auth import require, get_current_user

router = APIRouter(prefix="/backup", tags=["backup"])

# Main business collections covered by export/import (ledger lines ride with entries).
MAIN_COLLECTIONS = [
    "invoices", "customer_credits", "bills", "expenses", "vendor_credits", "vendors",
    "customers", "products", "purchase_orders", "estimates", "jobs", "job_costs",
    "sales", "giftcards", "commission_payouts", "booth_rent_payments",
    "recurring_invoices", "payment_reminders",
    "journal_entries", "journal_lines",
    "bank_accounts", "bank_transactions",
]


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


@router.post("/export")
async def export_backup(user: dict = Depends(require("ledger:admin"))):
    """Return a JSON dump of the tenant's main collections as a downloadable file."""
    tenant_id = user["tenant_id"]
    dump = {"format": "bookkeeping-backup", "version": 1,
            "tenant_id": tenant_id, "exported_at": now_iso(),
            "collections": {}}
    total = 0
    for name in MAIN_COLLECTIONS:
        docs = await db[name].find({"tenant_id": tenant_id}, {"_id": 0}).to_list(length=200000)
        dump["collections"][name] = docs
        total += len(docs)
    await _audit(user, "backup.export", "backup", tenant_id, {"documents": total})
    body = json.dumps(dump, default=str)
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="bookkeeping-backup-{date.today().isoformat()}.json"'},
    )


class ImportBody(BaseModel):
    confirm: bool = False
    dump: Optional[Dict[str, List[dict]]] = None


@router.post("/import")
async def import_backup(body: ImportBody, user: dict = Depends(require("ledger:admin"))):
    """Restore the tenant's main collections from an exported dump. The safeguard:
    the body MUST contain {"confirm": true} — without it the import is refused and
    nothing is touched."""
    if body.confirm is not True:
        raise HTTPException(
            status_code=422,
            detail='Import refused: body must contain {"confirm": true} to proceed.',
        )
    if not body.dump:
        raise HTTPException(status_code=422, detail="Import refused: 'dump' is missing or empty.")
    tenant_id = user["tenant_id"]
    restored = 0
    collections = 0
    for name, docs in body.dump.items():
        if name not in MAIN_COLLECTIONS:
            continue  # never touch anything outside the known set (users, audit log, ...)
        if not isinstance(docs, list):
            raise HTTPException(status_code=422, detail=f"Invalid docs for collection '{name}'")
        await db[name].delete_many({"tenant_id": tenant_id})
        clean = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            d = dict(d)
            d.pop("_id", None)
            d["tenant_id"] = tenant_id  # force-own everything we insert
            clean.append(d)
        if clean:
            await db[name].insert_many(clean)
            restored += len(clean)
        collections += 1
    await _audit(user, "backup.import", "backup", tenant_id,
                 {"documents": restored, "collections": collections})
    return {"ok": True, "restored_documents": restored, "collections": collections}
