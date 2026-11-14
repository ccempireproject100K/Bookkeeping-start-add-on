"""Audit-log writer used across routers: `from lib.audit import audit`.

RESTORED 2026-11-14 (InvoiceEase fold-in, FIX-IT-FIRST): 19 routers import
this module but the file was missing from the tree, so the app could not
import them at all. The shape mirrors the per-router `_audit` helpers
(e.g. routers/portal.py) that were written while this module was absent:
one document per event in `audit_log`, tenant-scoped, actor from the user
dict when present. `routers/audit.py` reads this same collection.
"""
from db import db
from lib.base import new_id, now_iso


async def audit(user, action, entity, entity_id, details=None,
                tenant_id=None, system=False):
    tid = tenant_id or (user or {}).get("tenant_id")
    doc = {
        "id": new_id(),
        "tenant_id": tid,
        "actor_id": (user or {}).get("id") if not system else "system",
        "actor_name": (user or {}).get("name") if not system else "system",
        "actor_role": (user or {}).get("role") if not system else "system",
        "action": action,
        "entity": entity,
        "entity_id": entity_id,
        "details": details or {},
        "created_at": now_iso(),
    }
    await db.audit_log.insert_one(doc)
    return doc
