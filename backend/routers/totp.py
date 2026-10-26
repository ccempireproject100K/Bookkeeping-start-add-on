"""Two-factor auth: TOTP authenticator-app codes.

Flow (any logged-in user):
  1. POST /totp/setup   -> {secret, otpauth_url}. Stored Fernet-encrypted as
     totp_pending_enc. NOT enabled yet.
  2. POST /totp/verify {code} -> verifies the code against the pending
     secret -> totp_enabled=True on the user.
  3. POST /totp/disable {password} -> clears secrets, totp_enabled=False.

At login (see routers/auth.py): if user.totp_enabled, the client must also
send totp_code; missing/invalid -> 401 {"detail": "TOTP_REQUIRED"}.

The secret is encrypted at rest with lib.pii (Fernet, TOKEN_ENCRYPTION_KEY).
TOTP verification is RFC 6238 (SHA-1, 30s step, 6 digits), implemented with
the stdlib (hmac/hashlib) so any authenticator app works and no new pip
dependency is needed. Manual key entry is used for setup (no QR library).
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import db
from lib.audit import append_audit
from lib.auth import get_current_user, verify_password
from lib.base import new_id, now_iso
from lib.pii import pii_decrypt, pii_encrypt

router = APIRouter(prefix="/totp", tags=["totp"])

ISSUER = "Mind at Ease Solutions"


async def _audit(user, action, entity, entity_id, details=None):
    await append_audit(db, {
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


def totp_verify(b32_secret: str, code: str, window: int = 1) -> bool:
    """RFC 6238 TOTP check (SHA-1, 30s step, 6 digits). window = steps each side."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return False
    try:
        key = base64.b32decode(b32_secret.upper())
    except Exception:
        return False
    now = int(time.time())
    for offset in range(-window, window + 1):
        counter = (now // 30) + offset
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        o = digest[-1] & 0x0F
        val = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
        if hmac.compare_digest(f"{val:06d}", code):
            return True
    return False


def _new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _otpauth_url(secret: str, email: str) -> str:
    issuer = quote(ISSUER)
    return f"otpauth://totp/{issuer}:{quote(email)}?secret={secret}&issuer={issuer}"


class VerifyIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class PasswordIn(BaseModel):
    password: str = Field(min_length=1)


@router.post("/setup")
async def setup(user: dict = Depends(get_current_user)):
    """Start enrollment: returns the secret + otpauth URI. Not enabled until /verify."""
    full = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not full:
        raise HTTPException(status_code=401, detail="Invalid session")
    if full.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="Two-factor auth is already enabled")
    secret = _new_secret()
    await db.users.update_one(
        {"id": user["id"]}, {"$set": {"totp_pending_enc": pii_encrypt(secret)}})
    await _audit(user, "auth.2fa.setup_started", "user", user["id"])
    return {"secret": secret, "otpauth_url": _otpauth_url(secret, user.get("email", ""))}


@router.post("/verify")
async def verify(body: VerifyIn, user: dict = Depends(get_current_user)):
    """Confirm the pending secret with an authenticator code -> enable 2FA."""
    full = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not full:
        raise HTTPException(status_code=401, detail="Invalid session")
    if full.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="Two-factor auth is already enabled")
    pending = full.get("totp_pending_enc")
    if not pending:
        raise HTTPException(status_code=400, detail="Call /totp/setup first")
    if not totp_verify(pii_decrypt(pending), body.code):
        raise HTTPException(status_code=401, detail="That code didn't work — check your authenticator app")
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"totp_secret_enc": pending, "totp_enabled": True,
                  "totp_enabled_at": now_iso()},
         "$unset": {"totp_pending_enc": ""}})
    await _audit(user, "auth.2fa.enabled", "user", user["id"])
    return {"ok": True}


@router.post("/disable")
async def disable(body: PasswordIn, user: dict = Depends(get_current_user)):
    """Disable 2FA; requires the account password."""
    full = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not full or not full.get("password_hash") or not verify_password(body.password, full["password_hash"]):
        raise HTTPException(status_code=401, detail="Password didn't match")
    await db.users.update_one(
        {"id": user["id"]},
        {"$unset": {"totp_secret_enc": "", "totp_pending_enc": ""},
         "$set": {"totp_enabled": False}})
    await _audit(user, "auth.2fa.disabled", "user", user["id"])
    return {"ok": True}
