"""Dedicated integration credentials for app-to-app sync (Momo -> Books).

Replaces the pasted 7-day owner JWT Momo previously used to push
customers/invoices/payments into a tenant's books. A credential is a
long-lived, scoped, revocable machine credential — see
INTEGRATION_CONTRACT.md at the repo root for the full contract.

Design rules (owner directive, 2026-10-17):

* Additional machine path only — all user auth (JWT/cookie/roles) is
  untouched, and nothing here weakens it.
* Least privilege: the credential authenticates an *integration actor*
  bound to exactly one tenant, with role "integration" — a role that
  appears in NO entry of lib.auth.PERMISSIONS. It is accepted only by
  `get_sync_actor` on the three sync write endpoints (and by
  `get_integration_actor` on the health endpoint). Presented anywhere
  else it is simply not a valid JWT and the existing decoder rejects
  it with 401.
* Tenant isolation by construction: the tenant always comes from the
  credential record, never from the request, so a credential cannot
  be aimed at another tenant's data.
* Secrets: 256-bit random, shown exactly once at issuance/rotation.
  Only SHA-256(secret) is stored; verification is a constant-time
  digest compare. Secrets are never logged and never returned by any
  list/read endpoint.
* Lifecycle: issuance, revocation, and rotation are owner-only
  operations (routers/integrations.py). Rotation replaces the secret
  in place — the old secret dies immediately. Credentials carry an
  expiry (default 365 days) and are rejected once expired/revoked.
"""
import hashlib
import hmac
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from db import db
from lib.auth import PERMISSIONS, get_current_user
from lib.base import new_id, now_iso

TOKEN_PREFIX = "mae1."
CREDENTIAL_SCOPES = ["books:sync"]
DEFAULT_EXPIRES_DAYS = 365
MAX_EXPIRES_DAYS = 730


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint_secret() -> str:
    return _secrets.token_urlsafe(32)


def format_credential(client_id: str, secret: str) -> str:
    return f"{TOKEN_PREFIX}{client_id}.{secret}"


def mint_credential_material():
    """Return (client_id, secret, full credential string)."""
    client_id = "ci_" + _secrets.token_hex(12)
    secret = mint_secret()
    return client_id, secret, format_credential(client_id, secret)


def is_integration_token(token: str) -> bool:
    return bool(token) and token.startswith(TOKEN_PREFIX)


def _parse(token: str):
    """Split a presented credential into (client_id, secret) or None."""
    if not is_integration_token(token):
        return None
    rest = token[len(TOKEN_PREFIX):]
    client_id, sep, secret = rest.partition(".")
    if not sep or not client_id or not secret:
        return None
    return client_id, secret


def _is_past(iso_ts) -> bool:
    if not iso_ts:
        return False
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts <= datetime.now(timezone.utc)


def new_credential_doc(*, tenant_id: str, created_by: str, label: str,
                       expires_in_days: int):
    """Build the stored document for a freshly minted credential.

    Returns (doc, full_credential_string). The full string is the only
    place the secret ever exists outside the caller's hands.
    """
    client_id, secret, full = mint_credential_material()
    now = datetime.now(timezone.utc)
    doc = {
        "id": new_id(),
        "tenant_id": tenant_id,
        "client_id": client_id,
        "secret_hash": hash_secret(secret),
        "label": label or "Momo sync",
        "scopes": list(CREDENTIAL_SCOPES),
        "created_at": now_iso(),
        "created_by": created_by,
        "expires_at": (now + timedelta(days=expires_in_days)).isoformat(),
        "revoked_at": None,
        "rotated_at": None,
        "last_used_at": None,
    }
    return doc, full


def public_view(doc: dict) -> dict:
    """Metadata-only view of a credential — never secret material."""
    return {
        "id": doc.get("id"),
        "client_id": doc.get("client_id"),
        "label": doc.get("label"),
        "scopes": doc.get("scopes", []),
        "created_at": doc.get("created_at"),
        "created_by": doc.get("created_by"),
        "expires_at": doc.get("expires_at"),
        "revoked_at": doc.get("revoked_at"),
        "rotated_at": doc.get("rotated_at"),
        "last_used_at": doc.get("last_used_at"),
    }


async def authenticate_integration_token(token: str) -> dict:
    """Validate a presented credential; return the integration actor.

    Raises HTTPException(401) on any failure. Bad-secret and
    unknown-client failures deliberately share one generic message so
    the response does not reveal which part was wrong.
    """
    parsed = _parse(token)
    if not parsed:
        raise HTTPException(status_code=401,
                            detail="Invalid integration credential")
    client_id, secret = parsed
    cred = await db.integration_credentials.find_one(
        {"client_id": client_id})
    if not cred or not hmac.compare_digest(
            hash_secret(secret), cred.get("secret_hash", "")):
        raise HTTPException(status_code=401,
                            detail="Invalid integration credential")
    if cred.get("revoked_at"):
        raise HTTPException(status_code=401,
                            detail="Integration credential revoked")
    if _is_past(cred.get("expires_at")):
        raise HTTPException(status_code=401,
                            detail="Integration credential expired")
    try:
        await db.integration_credentials.update_one(
            {"id": cred["id"]}, {"$set": {"last_used_at": now_iso()}})
    except Exception:
        pass  # bookkeeping about the credential must not break sync
    return {
        "id": f"integration:{client_id}",
        "tenant_id": cred["tenant_id"],
        "email": f"integration+{client_id}@system.local",
        "name": f"Integration ({cred.get('label') or 'sync'})",
        "role": "integration",
        "auth_kind": "integration",
        "integration_client_id": client_id,
        "scopes": cred.get("scopes", []),
    }


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    return ""


async def get_integration_actor(request: Request) -> dict:
    """Dependency for integration-only endpoints (health check).

    A user JWT is NOT accepted here: this surface exists so Momo can
    verify the machine credential itself.
    """
    token = _bearer(request)
    if not is_integration_token(token):
        raise HTTPException(status_code=401,
                            detail="Integration credential required")
    return await authenticate_integration_token(token)


async def get_sync_actor(request: Request) -> dict:
    """Dependency for the sync write endpoints (customers/invoices/pay).

    Two accepted auth paths, nothing else changes:

    * `Authorization: Bearer mae1.<client_id>.<secret>` — a dedicated
      integration credential (scope `books:sync`), bound to its tenant.
    * The existing user session (cookie or JWT Bearer) with the same
      `ops:manage` role check `require("ops:manage")` performed before.
    """
    token = _bearer(request)
    if is_integration_token(token):
        actor = await authenticate_integration_token(token)
        if "books:sync" not in actor.get("scopes", []):
            raise HTTPException(status_code=403,
                                detail="Integration credential lacks the books:sync scope")
        return actor
    user = await get_current_user(request)
    allowed = PERMISSIONS.get("ops:manage", set())
    if user.get("role") not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user.get('role')}' cannot perform ops:manage")
    return user


def actor_label(user: dict) -> str:
    """Short actor string recorded on idempotency/sync records."""
    if (user or {}).get("auth_kind") == "integration":
        return f"integration:{user.get('integration_client_id')}"
    return (user or {}).get("email", "")
