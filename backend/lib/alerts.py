"""Security alerts: the in-app feed every anomaly detector writes to.

Alert types: new_device, brute_force, pii_burst, privilege_change,
off_hours_admin, backup_stale, sentinel_digest, passkey_added, ...
"""
from db import db
from lib.base import new_id, now_iso


async def raise_alert(tenant_id: str, user_id: str | None, atype: str,
                      title: str, detail: str = "") -> dict:
    alert = {
        "id": new_id(), "tenant_id": tenant_id, "user_id": user_id,
        "type": atype, "title": title, "detail": detail,
        "created_at": now_iso(), "acknowledged_at": None,
    }
    await db.security_alerts.insert_one(alert)
    return {k: v for k, v in alert.items() if k != "_id"}
