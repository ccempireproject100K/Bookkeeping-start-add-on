"""Integration-events receiver: the books side of GLUE-1/2/3 (+4 intake).

The empire-automation flows POST empire events to the bookkeeping
surface (REGISTER.md chain #1: "Stripe events -> bookkeeping — if a
payment event is lost, the books are wrong and nobody knows"). This
endpoint is that receiving surface. It did not exist; the flows were
PARTIAL until it landed.

Contract (from flows/internal-glue/*.flow.json, not invented here):
  POST /api/integrations/events
  Authorization: Bearer <books_bearer>          (GLUE-1/2/3)
  Content-Type: application/json
  Body: {"data": <Stripe trigger output>}      (object or JSON string)

A post signed with `x-books-signature` (HMAC-SHA256 of the raw body,
the GLUE-4 discipline) is also accepted in place of the bearer — the
two credentials are the two the bundle defines for books-bound
traffic. When NEITHER credential is configured in the environment,
the endpoint refuses 503: an integration door with no lock is worse
than a closed one.

Semantics (the bundle's 9.2 discipline, mirrored):
* Auth is verified before anything is written; a failed check is a
  401 with NO side effects — no event row, no audit row.
* Dedupe on the source's event id: a redelivery returns
  {"status": "duplicate"} and changes nothing a second time (the one
  additional write is the audit row recording the duplicate outcome).
* Every accepted post lands one row in `integration_events` and one
  audit row per outcome (received / duplicate).
* A Stripe payment-failed event additionally fans out a books alert
  through lib.integration_events.send_books_alert (the GLUE-4 signed
  sender); the fan-out result is recorded on the event row and in the
  response, and a failed fan-out never fails the event post — the
  event itself is the money truth and it has landed.

Dedupe is check-then-insert, the same claim pattern the flows use;
a unique index on (source, event_id) is the deploy-side hardening if
concurrent double-delivery ever becomes real.
"""
import json

from fastapi import APIRouter, HTTPException, Request

from db import db
from lib.audit import audit
from lib.base import new_id, now_iso
from lib import integration_events as IE

router = APIRouter(prefix="/integrations", tags=["integration-events"])

# The machine actor recorded on this surface's audit rows.
_RECEIVER_USER = {
    "id": "system:integration-events",
    "tenant_id": "",
    "name": "Integration events receiver",
    "email": "integration-events@system.local",
}


def _authorize(request: Request, raw: bytes) -> str:
    """Return the auth method that verified ("bearer" | "hmac").

    503 when no credential is configured at all; 401 when a presented
    credential does not verify. Nothing is written on either failure.
    """
    token = IE.events_token()
    secret = IE.alerts_webhook_secret()
    if not token and not secret:
        raise HTTPException(
            status_code=503,
            detail="Integration events receiver is not configured")
    if token and IE.verify_bearer(
            request.headers.get("Authorization", ""), token):
        return "bearer"
    if secret and IE.verify_signature(
            raw, request.headers.get(IE.SIGNATURE_HEADER, ""), secret):
        return "hmac"
    raise HTTPException(status_code=401,
                        detail="Invalid integration event credential")


@router.post("/events")
async def receive_event(request: Request):
    raw = await request.body()
    auth_method = _authorize(request, raw)
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON")
    payload = IE.unwrap_payload(body)
    source, event_id, event_type = IE.classify_event(payload)
    if not event_id:
        raise HTTPException(
            status_code=422,
            detail="Event carries no id (expected evt.id or data.object.id)")

    existing = await db.integration_events.find_one(
        {"source": source, "event_id": event_id})
    if existing:
        await audit(_RECEIVER_USER, "integration.event.duplicate",
                    "integration_event", existing.get("id", ""),
                    {"source": source, "event_id": event_id,
                     "type": event_type, "auth": auth_method})
        return {"status": "duplicate", "id": existing.get("id"),
                "source": source, "event_id": event_id,
                "type": event_type}

    doc = {
        "id": new_id(),
        "source": source,
        "event_id": event_id,
        "type": event_type,
        "payload": payload,
        "auth": auth_method,
        "received_at": now_iso(),
        "alert_fanout": None,
    }
    await db.integration_events.insert_one(doc)

    fanout = None
    if source == "stripe" and event_type in IE.PAYMENT_FAILED_TYPES:
        summary = (f"Stripe payment failed ({event_type}) — "
                   f"event {event_id}")
        fanout = await IE.send_books_alert(
            alert_type="payment_failed", summary=summary,
            detail=f"integration event {doc['id']}")
        doc["alert_fanout"] = fanout
        await db.integration_events.update_one(
            {"id": doc["id"]}, {"$set": {"alert_fanout": fanout}})

    await audit(_RECEIVER_USER, "integration.event.received",
                "integration_event", doc["id"],
                {"source": source, "event_id": event_id,
                 "type": event_type, "auth": auth_method,
                 "alert_fanout": (fanout or {}).get("sent")})
    return {"status": "received", "id": doc["id"], "source": source,
            "event_id": event_id, "type": event_type,
            "alert_fanout": fanout}
