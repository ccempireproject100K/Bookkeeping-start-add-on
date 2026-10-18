"""Integration credentials & sync monitoring (Momo -> Books link).

Owner-facing management for the dedicated integration credentials that
replaced pasted user JWTs, plus the integration health endpoint Momo
uses to test the connection, plus the sync-record surface owners use
to see idempotency/sync outcomes and failures.

Contract: INTEGRATION_CONTRACT.md at the repo root. Credential secrets
are shown exactly once (issuance / rotation responses) and never again
— list views are metadata-only.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import now_iso
from lib.integration_auth import (
    DEFAULT_EXPIRES_DAYS,
    MAX_EXPIRES_DAYS,
    format_credential,
    get_integration_actor,
    hash_secret,
    mint_secret,
    new_credential_doc,
    public_view,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])


async def _find_own_credential(user, cred_id: str):
    cred = await db.integration_credentials.find_one(
        {"id": cred_id, "tenant_id": user["tenant_id"]})
    if not cred:
        raise HTTPException(status_code=404,
                            detail="Integration credential not found")
    return cred


class IssueBody(BaseModel):
    label: str = "Momo sync"
    expires_in_days: Optional[int] = DEFAULT_EXPIRES_DAYS


@router.post("/credentials")
async def issue_credential(body: IssueBody,
                           user: dict = Depends(require("ledger:admin"))):
    """Mint a new integration credential for this workspace.

    The full credential string is returned ONCE in this response; only
    its hash is stored. Paste it into Momo's bookkeeping-link settings.
    """
    days = body.expires_in_days or DEFAULT_EXPIRES_DAYS
    if days < 1 or days > MAX_EXPIRES_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"expires_in_days must be between 1 and {MAX_EXPIRES_DAYS}")
    doc, full = new_credential_doc(
        tenant_id=user["tenant_id"], created_by=user.get("email", ""),
        label=body.label, expires_in_days=days)
    await db.integration_credentials.insert_one(doc)
    await audit(user, "integration.credential_issued",
                "integration_credential", doc["id"],
                {"client_id": doc["client_id"], "label": doc["label"],
                 "expires_at": doc["expires_at"]})
    out = public_view(doc)
    out["credential"] = full
    return out


@router.get("/credentials")
async def list_credentials(user: dict = Depends(require("ledger:admin"))):
    rows = await db.integration_credentials.find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0}).sort(
            "created_at", -1).to_list(100)
    return [public_view(r) for r in rows]


@router.post("/credentials/{cred_id}/revoke")
async def revoke_credential(cred_id: str,
                            user: dict = Depends(require("ledger:admin"))):
    cred = await _find_own_credential(user, cred_id)
    if cred.get("revoked_at"):
        return {"ok": True, "id": cred_id, "revoked_at": cred["revoked_at"]}
    await db.integration_credentials.update_one(
        {"id": cred_id, "tenant_id": user["tenant_id"]},
        {"$set": {"revoked_at": now_iso()}})
    await audit(user, "integration.credential_revoked",
                "integration_credential", cred_id,
                {"client_id": cred.get("client_id")})
    return {"ok": True, "id": cred_id, "revoked_at": now_iso()}


@router.post("/credentials/{cred_id}/rotate")
async def rotate_credential(cred_id: str,
                            user: dict = Depends(require("ledger:admin"))):
    """Replace the credential's secret; the old secret dies immediately.

    Same client_id, new secret — returned ONCE in this response.
    """
    cred = await _find_own_credential(user, cred_id)
    if cred.get("revoked_at"):
        raise HTTPException(status_code=409,
                            detail="Cannot rotate a revoked credential")
    # Keep the existing client_id so Momo-side references stay stable.
    secret = mint_secret()
    full = format_credential(cred["client_id"], secret)
    await db.integration_credentials.update_one(
        {"id": cred_id, "tenant_id": user["tenant_id"]},
        {"$set": {"secret_hash": hash_secret(secret),
                  "rotated_at": now_iso()}})
    await audit(user, "integration.credential_rotated",
                "integration_credential", cred_id,
                {"client_id": cred.get("client_id")})
    out = public_view(cred)
    out["rotated_at"] = now_iso()
    out["credential"] = full
    return out


@router.get("/me")
async def integration_me(actor: dict = Depends(get_integration_actor)):
    """Connection-health check for the integration credential itself."""
    tenant = await db.tenants.find_one(
        {"id": actor["tenant_id"]}, {"_id": 0, "id": 1, "name": 1})
    cred = await db.integration_credentials.find_one(
        {"client_id": actor["integration_client_id"]})
    return {
        "ok": True,
        "client_id": actor["integration_client_id"],
        "label": (cred or {}).get("label"),
        "scopes": actor.get("scopes", []),
        "tenant_id": actor["tenant_id"],
        "tenant_name": (tenant or {}).get("name"),
        "expires_at": (cred or {}).get("expires_at"),
    }


@router.get("/sync-records")
async def sync_records(limit: int = Query(100, ge=1, le=500),
                       user: dict = Depends(require("ledger:admin"))):
    """Recent idempotency/sync outcomes for this workspace."""
    rows = await db.idempotency_records.find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0}).sort(
            "updated_at", -1).to_list(limit)
    return rows
