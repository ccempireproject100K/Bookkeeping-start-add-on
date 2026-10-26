"""Password reset: request a link by email, set a new password with it.

There was no reset flow at all before this module — a user who forgot
their password had no way back in. The security shape follows the
patterns already established in this codebase:

  * Uniform responses: the request endpoint answers identically whether
    or not the email belongs to an account (no account enumeration),
    and confirm/peek give one generic failure for unknown, used, or
    expired tokens.
  * The emailed token is a random URL-safe secret; only its SHA-256 is
    stored, so a database read never yields a usable link.
  * Tokens expire after one hour and are single-use; requesting a new
    link invalidates every earlier unused token for the account.
  * Brute-force: per-email and per-IP request counts in a 15-minute
    window, the same shape as the login_attempts protection in
    routers/auth.py; the global rate-limit middleware is a second layer.
  * A successful reset bumps users.token_version, so every previously
    issued JWT stops validating — the same revocation mechanism as
    "sign out all devices" (POST /auth/sessions/revoke-all) — and
    clears the account's failed-login history.
  * The new password goes through the exact validation registration
    uses: 12–72 UTF-8 bytes (RegisterBody) plus the breached-password
    screen in lib/pwned.py.

Email goes through lib.mailer.send_email — the app's one transactional
sender (invites, portal links, device alerts). It is key-gated on
SMTP_HOST (+ SMTP_PORT/SMTP_USER/SMTP_PASSWORD/SMTP_FROM/SMTP_USE_TLS);
unconfigured, send_email returns ("skipped", ...) and the request still
answers uniformly — the token is issued and stored, only delivery waits
on SMTP configuration. The link base is FRONTEND_URL, the same
environment variable invite links are built from.
"""
import hashlib
import os
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi import HTTPException
from pydantic import BaseModel, EmailStr, field_validator

from db import db
from lib.auth import hash_password
from lib.base import new_id
from lib.mailer import send_email
from lib.pwned import is_pwned

router = APIRouter(prefix="/auth", tags=["auth"])

TOKEN_TTL = timedelta(hours=1)
# Same thresholds as the login brute-force window in routers/auth.py.
MAX_PER_EMAIL = 5
MAX_PER_IP = 20
WINDOW = timedelta(minutes=15)

UNIFORM_MESSAGE = (
    "If an account exists for that email, a password reset link is on its way."
)
INVALID_LINK = (
    "This reset link is invalid or has expired. Request a new one and try again."
)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reset_url(raw_token: str) -> str:
    base = os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/reset-password?token={raw_token}"


class RequestBody(BaseModel):
    email: EmailStr


class ConfirmBody(BaseModel):
    token: str
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, password: str) -> str:
        # Identical to RegisterBody.validate_password (routers/auth.py):
        # bcrypt hashes only the first 72 bytes; reject longer values
        # rather than silently treating distinct passwords as equivalent.
        if not 12 <= len(password.encode("utf-8")) <= 72:
            raise ValueError("Password must be between 12 and 72 UTF-8 bytes")
        return password


async def _record_request(email: str, ip: str):
    await db.password_reset_requests.insert_one({
        "id": new_id(), "email": email, "ip": ip,
        "at": datetime.now(timezone.utc),
    })
    # Opportunistic cleanup so the collection stays small (login pattern).
    await db.password_reset_requests.delete_many(
        {"at": {"$lt": datetime.now(timezone.utc) - timedelta(hours=1)}})


async def _check_request_rate_limit(email: str, ip: str):
    window = {"at": {"$gte": datetime.now(timezone.utc) - WINDOW}}
    n_email = await db.password_reset_requests.count_documents(
        {**window, "email": email})
    n_ip = await db.password_reset_requests.count_documents(
        {**window, "ip": ip})
    if n_email >= MAX_PER_EMAIL or n_ip >= MAX_PER_IP:
        raise HTTPException(
            status_code=429,
            detail="Too many reset requests. Try again in 15 minutes.")


async def _issue_token(user: dict) -> str:
    """Invalidate the account's outstanding tokens and mint a fresh one.
    Returns the raw token (only ever sent in the email, never stored)."""
    now = datetime.now(timezone.utc)
    await db.password_resets.update_many(
        {"user_id": user["id"], "used_at": None, "invalidated": False},
        {"$set": {"invalidated": True}})
    raw = secrets.token_urlsafe(32)
    await db.password_resets.insert_one({
        "id": new_id(), "user_id": user["id"], "tenant_id": user["tenant_id"],
        "email": user["email"], "token_hash": _hash_token(raw),
        "created_at": now, "expires_at": now + TOKEN_TTL,
        "used_at": None, "invalidated": False,
    })
    return raw


def _build_email(raw_token: str) -> tuple[str, str]:
    url = _reset_url(raw_token)
    subject = "Reset your Mind at Ease Solutions password"
    body = (
        "We received a request to reset the password for your Mind at Ease "
        "Solutions account.\n\n"
        f"Set a new password here:\n{url}\n\n"
        "This link expires in 1 hour and can be used once. If you didn't "
        "request a reset, you can ignore this email — your password stays "
        "as it is.\n\n"
        "Sent via Mind at Ease Solutions"
    )
    return subject, body


@router.post("/password-reset/request")
async def request_reset(body: RequestBody, request: Request):
    email = body.email.lower().strip()
    ip = request.client.host if request.client else "unknown"
    await _check_request_rate_limit(email, ip)
    await _record_request(email, ip)
    user = await db.users.find_one({"email": email})
    if user:
        raw = await _issue_token(user)
        subject, text = _build_email(raw)
        # Best-effort delivery (lib.mailer is key-gated on SMTP_HOST);
        # the response is uniform either way — see module docstring.
        await send_email(email, subject, text)
    return {"ok": True, "message": UNIFORM_MESSAGE}


async def _live_token(raw: str) -> dict:
    """The token document for a raw token, or raise the one generic 400."""
    doc = await db.password_resets.find_one({"token_hash": _hash_token(raw)})
    if (not doc or doc.get("used_at") is not None or doc.get("invalidated")
            or doc["expires_at"] < datetime.now(timezone.utc)):
        raise HTTPException(status_code=400, detail=INVALID_LINK)
    return doc


@router.get("/password-reset/peek/{token}")
async def peek_reset(token: str):
    """Lets the reset page reject a dead link before showing the form.
    Returns validity only — never account details."""
    await _live_token(token)
    return {"valid": True}


@router.post("/password-reset/confirm")
async def confirm_reset(body: ConfirmBody):
    doc = await _live_token(body.token)
    if is_pwned(body.password):
        raise HTTPException(
            400, "This password has appeared in a data breach. "
                 "Please choose a different one.")
    user = await db.users.find_one({"id": doc["user_id"]})
    if not user:
        raise HTTPException(status_code=400, detail=INVALID_LINK)
    now = datetime.now(timezone.utc)
    # Bumping token_version revokes every outstanding JWT (lib.auth
    # rejects tokens whose version no longer matches the user row).
    new_version = user.get("token_version", 0) + 1
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"password_hash": hash_password(body.password),
                  "token_version": new_version}})
    await db.password_resets.update_one(
        {"id": doc["id"]}, {"$set": {"used_at": now}})
    # Nothing else outstanding for this account stays usable.
    await db.password_resets.update_many(
        {"user_id": user["id"], "used_at": None, "invalidated": False},
        {"$set": {"invalidated": True}})
    # Fresh start on the brute-force counter, as a successful login does.
    await db.login_attempts.delete_many({"email": user["email"]})
    # Local import: routers.auth is imported before this module in
    # server.py; this mirrors how routers/invites.py reuses _audit.
    from routers.auth import _audit
    user = {**user, "token_version": new_version}
    await _audit(user, "auth.password_reset", "user", user["id"], {})
    return {"ok": True}
