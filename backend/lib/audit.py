"""Audit trail writer.

Every mutating endpoint records who did what: ``await audit(user, action,
entity, entity_id, details)``. Rows land in the ``audit_log`` collection and
are readable through the ``/api/audit`` viewer (owner/accountant only).
"""
from db import db
from lib.base import new_id, now_iso


async def audit(user: dict, action: str, entity: str = "",
                entity_id: str = "", details: dict = None) -> dict:
    """Write one audit row for the user's tenant. Never raises."""
    try:
        doc = {
            "id": new_id(),
            "tenant_id": (user or {}).get("tenant_id", ""),
            "actor": (user or {}).get("id", ""),
            "actor_name": (user or {}).get("name", "") or (user or {}).get("email", ""),
            "actor_email": (user or {}).get("email", ""),
            "action": action,
            "entity": entity or "",
            "entity_id": entity_id or "",
            "details": details or {},
            "created_at": now_iso(),
        }
        await db.audit_log.insert_one(doc)
        return doc
    except Exception:
        # Auditing must never break the operation it records.
        return {}
