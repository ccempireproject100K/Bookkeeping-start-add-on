"""Inbound email -> receipt inbox.

Forward a receipt photo/PDF to your workspace's inbound address (or from your
login email) and it lands in the receipt inbox, filed exactly like a manual
upload: POST /receipts/upload -> scan -> review -> post.

Endpoint: POST /api/receipts/inbound
Accepts the common inbound-email webhook POST shapes — Mailgun routes,
SendGrid Inbound Parse, or anything that POSTs a multipart form with the
email fields and attachments (see docs/RECEIPT_EMAIL.md).

Auth: providers can't log in, so this route does NOT use session auth. It
verifies a shared secret instead: INBOUND_EMAIL_SECRET, passed as
?secret=... or the X-Inbound-Secret header. Requests without it are rejected.
If Mailgun signs the payload and MAILGUN_WEBHOOK_KEY is set, the signature
is verified too.

Workspace matching (first hit wins):
  1. Recipient token — mail to receipts+<token>@<INBOUND_EMAIL_DOMAIN>,
     where <token> is the workspace's inbound_token
     (GET /receipts/inbound-address, owner only).
  2. Sender email — the From address matched against user accounts.

Idempotency: the provider's Message-Id is recorded in `inbound_receipts`
(unique index); re-deliveries return 200 with duplicate:true.
"""
import hashlib
import hmac
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso
from routers.receipts import ALLOWED_TYPES, MAX_BYTES, ingest_receipt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/receipts", tags=["receipts"])

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TOKEN_RE = re.compile(r"\+([A-Za-z0-9_-]{8,})@")
EXT_TO_TYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp",
               ".pdf": "application/pdf"}

_index_ready = False


async def _ensure_index():
    global _index_ready
    if not _index_ready:
        await db.inbound_receipts.create_index("message_id", unique=True)
        _index_ready = True


def _secret() -> str:
    return os.environ.get("INBOUND_EMAIL_SECRET", "").strip()


def _check_secret(request: Request):
    """Fail closed: the endpoint refuses to run unless the shared secret is
    configured, and rejects any call that doesn't present it."""
    expected = _secret()
    if not expected:
        raise HTTPException(503, "Inbound email is not configured (INBOUND_EMAIL_SECRET).")
    given = request.query_params.get("secret", "") or \
        request.headers.get("x-inbound-secret", "")
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(401, "Bad or missing webhook secret.")


def _check_mailgun_signature(form) -> None:
    """Defense in depth: verify Mailgun's HMAC signature when a signing key
    is configured. Skipped for other providers."""
    key = os.environ.get("MAILGUN_WEBHOOK_KEY", "").strip()
    ts, token, sig = form.get("timestamp"), form.get("token"), form.get("signature")
    if not key or not (ts and token and sig):
        return
    mac = hmac.new(key.encode(), f"{ts}{token}".encode(),
                   hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, sig):
        raise HTTPException(401, "Bad Mailgun signature.")


def _first_present(*vals) -> str:
    for v in vals:
        if v:
            return str(v)
    return ""


def _message_id(form) -> str:
    mid = _first_present(form.get("Message-Id"), form.get("message-id"))
    if mid:
        return mid.strip()
    # SendGrid puts the raw headers in one field; fish out Message-ID.
    headers = _first_present(form.get("headers"))
    m = re.search(r"(?im)^message-id:\s*(.+)$", headers)
    return m.group(1).strip() if m else ""


async def _resolve_user(form) -> Optional[dict]:
    """Match the email to a workspace. Recipient token first, sender second."""
    to_field = _first_present(form.get("recipient"), form.get("to"),
                              form.get("envelope"))
    for addr in EMAIL_RE.findall(to_field):
        tm = TOKEN_RE.search(addr)
        if tm:
            tenant = await db.tenants.find_one({"inbound_token": tm.group(1)},
                                               {"_id": 0})
            if tenant:
                user = await db.users.find_one(
                    {"tenant_id": tenant["id"], "role": "owner"}, {"_id": 0}) or \
                    await db.users.find_one({"tenant_id": tenant["id"]}, {"_id": 0})
                if user:
                    return user
    from_field = _first_present(form.get("sender"), form.get("from"))
    m = EMAIL_RE.search(from_field)
    if m:
        return await db.users.find_one({"email": m.group(0).lower()}, {"_id": 0})
    return None


def _iter_attachments(form):
    """Yield (filename, content_type, UploadFile) for every attached file,
    across Mailgun (attachment-N fields) and SendGrid (named files) shapes."""
    for _key, value in form.multi_items():
        if isinstance(value, UploadFile) and value.filename:
            yield value.filename, (value.content_type or ""), value


def _content_type(filename: str, declared: str) -> str:
    if declared in ALLOWED_TYPES:
        return declared
    ext = "." + (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    return EXT_TO_TYPE.get(ext, declared)


@router.post("/inbound")
async def inbound_receipt_email(request: Request):
    _check_secret(request)
    form = await request.form()
    _check_mailgun_signature(form)

    mid = _message_id(form)
    await _ensure_index()
    if mid:
        try:
            await db.inbound_receipts.insert_one(
                {"message_id": mid, "received_at": now_iso()})
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "E11000" in str(exc):
                return {"ok": True, "duplicate": True, "filed": 0,
                        "receipt_ids": []}
            raise

    user = await _resolve_user(form)
    if not user:
        # Authenticated but unroutable: 200 so the provider doesn't retry
        # mail we could never file.
        logger.warning("inbound receipt email: unknown workspace (from=%s)",
                       _first_present(form.get("sender"), form.get("from"))[:80])
        return {"ok": False, "filed": 0, "receipt_ids": [],
                "reason": "no workspace matched the sender or recipient"}

    filed, skipped = [], []
    for filename, declared_type, upload in _iter_attachments(form):
        ctype = _content_type(filename, declared_type)
        try:
            data = await upload.read()
            doc = await ingest_receipt(user, filename, ctype, data,
                                       source="email")
            filed.append(doc["id"])
        except HTTPException as exc:
            skipped.append({"filename": filename, "reason": exc.detail})
        except Exception as exc:  # transient (disk/db): let it raise -> retry
            logger.exception("inbound receipt ingest failed")
            raise HTTPException(502, f"Could not file {filename}: {exc}")
        finally:
            await upload.close()

    await audit(user, "receipt.inbound", "receipt", "",
                {"filed": len(filed), "skipped": len(skipped),
                 "from": _first_present(form.get("sender"), form.get("from"))[:120],
                 "subject": _first_present(form.get("subject"))[:120]})
    return {"ok": True, "duplicate": False, "filed": len(filed),
            "receipt_ids": filed, "skipped": skipped}


# ---------------- Inbound address management (owner only) ----------------
def _inbound_domain() -> str:
    return os.environ.get("INBOUND_EMAIL_DOMAIN", "inbound.example.com")


@router.get("/inbound-address")
async def get_inbound_address(user: dict = Depends(require("settings:write"))):
    """Show this workspace's receipt-by-email address."""
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    token = tenant.get("inbound_token")
    if not token:
        token = new_id().replace("-", "")[:24]
        await db.tenants.update_one({"id": user["tenant_id"]},
                                    {"$set": {"inbound_token": token}})
    return {"address": f"receipts+{token}@{_inbound_domain()}",
            "domain": _inbound_domain(),
            "webhook": "/api/receipts/inbound",
            "configured": bool(_secret())}


@router.post("/inbound-address/rotate")
async def rotate_inbound_address(user: dict = Depends(require("settings:write"))):
    """Issue a fresh inbound token (invalidates the old address)."""
    token = new_id().replace("-", "")[:24]
    await db.tenants.update_one({"id": user["tenant_id"]},
                                {"$set": {"inbound_token": token}})
    await audit(user, "receipt.inbound_rotate", "tenant", user["tenant_id"], {})
    return {"address": f"receipts+{token}@{_inbound_domain()}"}
