"""Empire-automation glue: books-side endpoints' shared machinery.

Two halves of the books <-> Activepieces contract (empire-automation
bundle, flows/internal-glue — the flows were built first and named
these books-side dependencies as missing; this module + routers/
integration_events.py are the books side):

* INBOUND (GLUE-1/2/3): the flows POST Stripe events (new payment,
  payment failed, subscription canceled) to a books integration-events
  URL with `Authorization: Bearer <books_bearer>` and a JSON body of
  {"data": <the Stripe trigger output>}. routers/integration_events.py
  is that receiver; the auth/extraction helpers live here.

* OUTBOUND (GLUE-4): the bookkeeping surface raises alerts by POSTing
  a signed webhook to the flow's catch-webhook URL. The flow verifies
  header `x-books-signature` as the hex HMAC-SHA256 of the RAW body
  string keyed with the shared `alerts_webhook_secret` (an optional
  "sha256=" prefix is stripped flow-side), then reads body fields
  {id, type, summary} — dedupe is on body.id. `send_books_alert`
  below produces exactly that shape and signature.

Environment (read at call time, never at import):
  BOOKS_INTEGRATION_EVENTS_TOKEN  the bearer the flows present
                                  (Activepieces `books_bearer`
                                  connection). Unset = the receiver
                                  refuses (503) unless a signed post
                                  verifies instead.
  BOOKS_ALERTS_WEBHOOK_URL        the GLUE-4 catch-webhook URL.
  BOOKS_ALERTS_WEBHOOK_SECRET     the shared signing secret
                                  (Activepieces `alerts_webhook_secret`
                                  connection).

Nothing here ever raises into a caller: the sender reports
{"sent": False, "reason": ...} and the fan-out wired into
lib/alerts.raise_alert is fire-and-forget, so a down automation
instance can never break the books surface it observes.
"""
import hashlib
import hmac
import json
import os

from lib.base import new_id, now_iso

EVENTS_TOKEN_ENV = "BOOKS_INTEGRATION_EVENTS_TOKEN"
ALERTS_URL_ENV = "BOOKS_ALERTS_WEBHOOK_URL"
ALERTS_SECRET_ENV = "BOOKS_ALERTS_WEBHOOK_SECRET"

SIGNATURE_HEADER = "x-books-signature"

# Stripe event types the flows treat as "a payment failed" (GLUE-2's
# trigger is invoice_payment_failed; the piece's event vocabulary and
# Stripe's raw type strings are both accepted here).
PAYMENT_FAILED_TYPES = (
    "invoice.payment_failed",
    "invoice_payment_failed",
    "payment_intent.payment_failed",
    "charge.failed",
)


def events_token() -> str:
    return (os.environ.get(EVENTS_TOKEN_ENV) or "").strip()


def alerts_webhook_url() -> str:
    return (os.environ.get(ALERTS_URL_ENV) or "").strip()


def alerts_webhook_secret() -> str:
    return (os.environ.get(ALERTS_SECRET_ENV) or "").strip()


def alerts_webhook_configured() -> bool:
    return bool(alerts_webhook_url() and alerts_webhook_secret())


# ------------------------------------------------------------- signatures

def sign_body(raw: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of the exact raw body bytes — the GLUE-4
    flow's verification computation, byte for byte."""
    return hmac.new(secret.encode("utf-8"), raw,
                    hashlib.sha256).hexdigest()


def verify_signature(raw: bytes, header_value: str, secret: str) -> bool:
    """Constant-time check of an `x-books-signature` header value
    against the raw body. Accepts the bare hex digest or the
    "sha256="-prefixed form (the flow strips the same prefix)."""
    if not secret or not header_value:
        return False
    given = header_value.strip()
    if given.startswith("sha256="):
        given = given[len("sha256="):]
    return hmac.compare_digest(given, sign_body(raw, secret))


def verify_bearer(header_value: str, token: str) -> bool:
    """Constant-time check of an Authorization header against the
    configured integration-events bearer token."""
    if not token or not header_value:
        return False
    given = header_value.strip()
    if given.startswith("Bearer "):
        given = given[len("Bearer "):]
    return hmac.compare_digest(given, token)


# ------------------------------------------------------- inbound extraction

def unwrap_payload(body):
    """The GLUE-1/2/3 flows POST {"data": <trigger output>}; depending
    on how the piece interpolates the trigger, that value arrives as
    an object or as a JSON string. Accept both, and accept a bare
    event object with no wrapper at all."""
    if isinstance(body, dict) and "data" in body:
        data = body["data"]
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except (TypeError, ValueError):
                return data
            return parsed
        return data
    return body


def classify_event(payload):
    """Return (source, event_id, event_type) for an unwrapped payload.

    Identity extraction mirrors the flows' own code step exactly:
    the event id is `evt.id`, falling back to `evt.data.object.id`.
    A payload carrying a `summary` (and no Stripe `data` envelope) is
    a books-alert-shaped post (the GLUE-4 body shape, accepted here
    so alert fan-out can also land on this surface); anything else
    with an id is recorded as a generic integration event.
    """
    if not isinstance(payload, dict):
        return "generic", "", "unknown"
    event_id = payload.get("id") or ""
    if not event_id:
        obj = (payload.get("data") or {}).get("object") or {}
        if isinstance(obj, dict):
            event_id = obj.get("id") or ""
    event_type = payload.get("type") or "unknown"
    if "data" in payload or payload.get("object") == "event":
        source = "stripe"
    elif "summary" in payload:
        source = "books_alert"
    else:
        source = "generic"
    return source, str(event_id), str(event_type)


# ------------------------------------------------------------ alert sender

def build_alert_body(*, alert_type: str, summary: str, detail: str = "",
                     event_id: str = None) -> dict:
    """The GLUE-4 body shape: {id, type, summary} are the fields the
    flow reads (dedupe is on id — every alert gets a fresh one unless
    the caller supplies a stable id for a re-send)."""
    return {
        "id": event_id or new_id(),
        "type": alert_type or "unknown",
        "summary": summary or "",
        "detail": detail or "",
        "source": "bookkeeping",
        "created_at": now_iso(),
    }


async def send_books_alert(*, alert_type: str, summary: str,
                           detail: str = "", event_id: str = None,
                           _client=None) -> dict:
    """POST one signed alert webhook to the GLUE-4 catch-webhook URL.

    Returns {"sent": bool, ...} and NEVER raises: not configured,
    transport errors, and non-2xx responses are all reported, not
    thrown, so alert fan-out can never break the operation that
    raised the alert. `_client` is a test seam (an httpx.AsyncClient
    instance to use instead of constructing one).
    """
    url = alerts_webhook_url()
    secret = alerts_webhook_secret()
    if not url or not secret:
        return {"sent": False, "reason": "not_configured"}
    body = build_alert_body(alert_type=alert_type, summary=summary,
                            detail=detail, event_id=event_id)
    raw = json.dumps(body, separators=(",", ":"),
                     sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: f"sha256={sign_body(raw, secret)}",
    }
    try:
        if _client is not None:
            resp = await _client.post(url, content=raw, headers=headers)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, content=raw,
                                         headers=headers)
    except Exception as exc:  # transport must never break the caller
        return {"sent": False, "reason": "transport_error",
                "error": str(exc), "alert_id": body["id"]}
    if 200 <= resp.status_code < 300:
        return {"sent": True, "alert_id": body["id"],
                "status_code": resp.status_code}
    return {"sent": False, "reason": f"http_{resp.status_code}",
            "alert_id": body["id"]}
