"""Security alerts: the in-app feed every anomaly detector writes to.

Alert types: new_device, brute_force, pii_burst, privilege_change,
off_hours_admin, backup_stale, sentinel_digest, passkey_added, ...

Fan-out: every raised alert is ALSO posted to the empire-automation
GLUE-4 flow as a signed books alert webhook (lib/integration_events.
send_books_alert) — fire-and-forget, and only when the webhook is
configured (BOOKS_ALERTS_WEBHOOK_URL + BOOKS_ALERTS_WEBHOOK_SECRET);
unconfigured is the normal state until the automation instance
exists, and a failed fan-out never affects the in-app alert.
"""
import asyncio

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
    try:
        from lib.integration_events import (alerts_webhook_configured,
                                            send_books_alert)
        if alerts_webhook_configured():
            asyncio.ensure_future(send_books_alert(
                alert_type=atype, summary=title, detail=detail,
                event_id=alert["id"]))
    except Exception:
        pass  # fan-out must never break the alert it mirrors
    return {k: v for k, v in alert.items() if k != "_id"}
