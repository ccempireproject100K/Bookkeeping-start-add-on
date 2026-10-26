"""Team invitations: bring an accountant, bookkeeper, or tax pro into a workspace.

Flow (adapted from the momoempire/Momoempire invitations pattern):
  1. Owner invites by email -> magic-link invite (/invite?token=...)
  2. Invitee opens the link, sets name + password (or, if they already have
     an account, the workspace is added to it)
  3. They join with the invited role; owners can revoke or remove access

A single login can belong to many workspaces (memberships); the workspace
switcher (GET /auth/workspaces, POST /auth/switch) hops between them.
"""
import hashlib
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field

from db import db
from lib.auth import get_current_user, hash_password, require
from lib.base import new_id, now_iso
from lib.mailer import send_email
from lib.memberships import memberships
from lib.pwned import is_pwned

router = APIRouter(prefix="/invites", tags=["invites"])


def _hash_token(raw: str) -> str:
    """Invite tokens are stored only as SHA-256 hashes — the same rule
    portal links and password resets follow — so a database read never
    yields a usable invite link."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _find_invite(token: str):
    """Resolve a raw invite token to its document. New invites are
    found by hash; the plaintext lookup remains only so invites issued
    before hash-at-rest landed keep working until they are accepted
    or revoked (no new document ever stores a raw token)."""
    inv = await db.invitations.find_one({"token_hash": _hash_token(token)})
    if inv is None:
        inv = await db.invitations.find_one({"token": token})
    return inv

# Owner can't be granted by invite (ownership transfer is a separate,
# deliberate action). Accountant is the tax-pro role.
INVITABLE_ROLES = {"accountant", "bookkeeper", "barber"}


def _accept_url(token: str) -> str:
    base = os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/invite?token={token}"


class InviteIn(BaseModel):
    email: EmailStr
    role: str = Field(..., max_length=20)


class AcceptIn(BaseModel):
    token: str = Field(..., max_length=128)
    name: str = Field(..., max_length=80)
    password: str = Field(default="", max_length=128)  # required for new accounts only


@router.post("")
async def create_invite(body: InviteIn, user: dict = Depends(require("ledger:admin"))):
    role = body.role.strip().lower()
    if role not in INVITABLE_ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(sorted(INVITABLE_ROLES))}")
    email = body.email.lower().strip()
    if email == user.get("email", "").lower():
        raise HTTPException(400, "You can't invite yourself")
    # No duplicate pending invite for the same email+workspace.
    existing = await db.invitations.find_one(
        {"tenant_id": user["tenant_id"], "email": email, "status": "pending"})
    if existing:
        raise HTTPException(409, "A pending invite already exists for this email")
    token = secrets.token_urlsafe(32)
    inv = {
        "id": new_id(), "token_hash": _hash_token(token),
        "tenant_id": user["tenant_id"],
        "email": email, "role": role, "invited_by": user["id"],
        "status": "pending", "created_at": now_iso(), "accepted_at": None,
    }
    await db.invitations.insert_one(inv)
    accept_url = _accept_url(token)
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"name": 1})
    shop = (tenant or {}).get("name", "your workspace")
    # Best-effort email; the owner always gets the link to share manually.
    await send_email(
        email, f"You're invited to {shop}",
        f"{user.get('name') or user.get('email')} invited you to {shop} "
        f"as {role}.\n\nAccept here:\n{accept_url}\n\n"
        f"The link is single-use. If you already have an account, accepting "
        f"adds this workspace to it.")
    out = {k: v for k, v in inv.items()
           if k not in ("_id", "token", "token_hash")}
    out["accept_url"] = accept_url
    return out


@router.get("")
async def list_invites(user: dict = Depends(require("ledger:admin"))):
    rows = await db.invitations.find(
        {"tenant_id": user["tenant_id"]}, {"_id": 0, "token": 0, "token_hash": 0}
    ).sort("created_at", -1).to_list(200)
    return rows


@router.post("/{inv_id}/revoke")
async def revoke_invite(inv_id: str, user: dict = Depends(require("ledger:admin"))):
    res = await db.invitations.update_one(
        {"id": inv_id, "tenant_id": user["tenant_id"], "status": "pending"},
        {"$set": {"status": "revoked"}})
    if not res.modified_count:
        raise HTTPException(404, "Pending invite not found")
    return {"ok": True}


@router.delete("/members/{email}")
async def remove_member(email: str, user: dict = Depends(require("ledger:admin"))):
    """Remove a team member's access to this workspace."""
    email = email.lower().strip()
    if email == user.get("email", "").lower():
        raise HTTPException(400, "You can't remove yourself")
    target = await db.users.find_one({"email": email})
    if not target:
        raise HTTPException(404, "User not found")
    ms = [m for m in memberships(target) if m["tenant_id"] != user["tenant_id"]]
    if len(ms) == len(memberships(target)):
        raise HTTPException(404, "That user is not in this workspace")
    # Never orphan a workspace: keep at least one owner membership.
    removed_owner = any(m["tenant_id"] == user["tenant_id"] and m["role"] == "owner"
                        for m in memberships(target))
    if removed_owner:
        owners_left = await db.users.count_documents({
            "email": {"$ne": email},
            "memberships": {"$elemMatch": {"tenant_id": user["tenant_id"], "role": "owner"}},
        })
        # Also count legacy single-tenant owners.
        owners_left += await db.users.count_documents({
            "email": {"$ne": email}, "tenant_id": user["tenant_id"], "role": "owner",
            "memberships": {"$exists": False},
        })
        if owners_left < 1:
            raise HTTPException(400, "Can't remove the last owner of a workspace")
    update = {"$set": {"memberships": ms}}
    if target.get("tenant_id") == user["tenant_id"]:
        if ms:
            update["$set"]["tenant_id"] = ms[0]["tenant_id"]
            update["$set"]["role"] = ms[0]["role"]
        else:
            # No workspaces left: deactivate the login entirely.
            await db.users.delete_one({"id": target["id"]})
            await db.invitations.update_many(
                {"email": email, "status": "pending"},
                {"$set": {"status": "revoked"}})
            return {"ok": True, "deactivated": True}
    else:
        update["$set"]["memberships"] = ms
    await db.users.update_one({"id": target["id"]}, update)
    return {"ok": True}


# ---------- Public invite acceptance (token link) ----------

@router.get("/peek/{token}")
async def peek_invite(token: str):
    inv = await _find_invite(token)
    if not inv or inv.get("status") != "pending":
        raise HTTPException(404, "This invite is no longer available")
    tenant = await db.tenants.find_one({"id": inv["tenant_id"]}, {"name": 1})
    return {"email": inv["email"], "role": inv["role"],
            "tenant_name": (tenant or {}).get("name", "a workspace")}


@router.post("/accept")
async def accept_invite(body: AcceptIn, response: Response):
    # Local import: routers.auth owns session issuance; invites is imported
    # by server after auth, so this never cycles at module load.
    from routers.auth import _audit, _issue, _public

    inv = await _find_invite(body.token)
    if not inv or inv.get("status") != "pending":
        raise HTTPException(400, "This invite is invalid or already used")
    email = inv["email"].lower()
    name = body.name.strip() or email.split("@")[0]
    user = await db.users.find_one({"email": email})
    if user:
        ms = memberships(user)
        if not any(m["tenant_id"] == inv["tenant_id"] for m in ms):
            ms.append({"tenant_id": inv["tenant_id"], "role": inv["role"]})
        await db.users.update_one(
            {"id": user["id"]},
            {"$set": {"memberships": ms, "tenant_id": inv["tenant_id"],
                      "role": inv["role"]}})
        user = await db.users.find_one({"id": user["id"]})
        await _audit(user, "invite.accepted", "user", user["id"],
                     {"tenant_id": inv["tenant_id"], "role": inv["role"],
                      "existing_account": True})
    else:
        if not body.password:
            raise HTTPException(400, "A password is required to create your account")
        if is_pwned(body.password):
            raise HTTPException(
                400, "This password has appeared in a data breach. "
                     "Please choose a different one.")
        user = {
            "id": new_id(), "tenant_id": inv["tenant_id"], "email": email,
            "password_hash": hash_password(body.password), "name": name,
            "role": inv["role"], "created_at": now_iso(),
            "memberships": [{"tenant_id": inv["tenant_id"], "role": inv["role"]}],
        }
        await db.users.insert_one(user)
        await _audit(user, "invite.accepted", "user", user["id"],
                     {"tenant_id": inv["tenant_id"], "role": inv["role"],
                      "existing_account": False})
    await db.invitations.update_one(
        {"id": inv["id"]},
        {"$set": {"status": "accepted", "accepted_at": now_iso()}})
    await _issue(response, user)
    return _public(user)
