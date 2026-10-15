"""Auth: register (new workspace), login, logout, me, demo role switch."""
import os
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Response, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr, Field

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import (
    hash_password, verify_password, create_access_token,
    set_session_cookie, clear_session_cookie, get_current_user, require,
)
from lib.coa import seed_coa
from lib.pii import last4, pii_decrypt, pii_encrypt

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterBody(BaseModel):
    email: EmailStr
    password: str
    name: str
    shop_name: str = "My Barbershop"


class LoginBody(BaseModel):
    email: EmailStr
    password: str
    totp_code: str = ""


async def _issue(response: Response, user: dict):
    token = create_access_token(user["id"], user["email"],
                                user.get("token_version", 0))
    set_session_cookie(response, token)


# Never sent to clients: credentials, encrypted PII, pending 2FA secrets.
_EXCLUDED_FIELDS = ("password_hash", "_id", "totp_secret_enc", "totp_pending_enc",
                    "payout_account_enc", "payout_routing_enc")


def _public(user: dict) -> dict:
    return {k: v for k, v in user.items() if k not in _EXCLUDED_FIELDS}


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


async def create_workspace(email, password, name, shop_name, role="owner", tenant_id=None, commission_rate=0):
    email = email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    if tenant_id is None:
        tenant_id = new_id()
        await db.tenants.insert_one({
            "id": tenant_id, "name": shop_name,
            "settings": {"inventory_enabled": True, "sales_tax_rate": 0.0725,
                         "double_entry_mode": "primary"},
            "created_at": now_iso(),
        })
        await seed_coa(Scoped(db.accounts, tenant_id))
    user = {
        "id": new_id(), "tenant_id": tenant_id, "email": email,
        "password_hash": hash_password(password), "name": name,
        "role": role, "commission_rate": commission_rate, "created_at": now_iso(),
    }
    await db.users.insert_one(user)
    return user


@router.post("/register")
async def register(body: RegisterBody, response: Response):
    user = await create_workspace(body.email, body.password, body.name, body.shop_name)
    await _issue(response, user)
    return _public(user)


@router.post("/login")
async def login(body: LoginBody, response: Response, request: Request):
    email = body.email.lower().strip()
    ip = request.client.host if request.client else "unknown"
    # Brute-force protection: 5 bad tries per email or 20 per IP in 15 min.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    window = {"at": {"$gte": cutoff}}
    n_email = await db.login_attempts.count_documents({**window, "email": email})
    n_ip = await db.login_attempts.count_documents({**window, "ip": ip})
    if n_email >= 5 or n_ip >= 20:
        raise HTTPException(status_code=429,
                            detail="Too many failed login attempts. Try again in 15 minutes.")
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        await _record_failed_login(email, ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.get("totp_enabled"):
        # Password is fine, but 2FA is on: require the authenticator code.
        # Lazy import avoids a routers.totp <-> routers.auth import cycle.
        from routers.totp import totp_verify
        enc = user.get("totp_secret_enc")
        if not enc or not totp_verify(pii_decrypt(enc), body.totp_code):
            await _record_failed_login(email, ip)
            raise HTTPException(status_code=401, detail="TOTP_REQUIRED")
    # Success: clear this email's failure history.
    await db.login_attempts.delete_many({"email": email})
    await _issue(response, user)
    await _audit(user, "auth.login", "user", user["id"], {"ip": ip})
    return _public(user)


async def _record_failed_login(email: str, ip: str):
    await db.login_attempts.insert_one({
        "id": new_id(), "email": email, "ip": ip,
        "at": datetime.now(timezone.utc),
    })
    # Opportunistic cleanup so the collection stays small.
    await db.login_attempts.delete_many(
        {"at": {"$lt": datetime.now(timezone.utc) - timedelta(hours=1)}})


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(response: Response, body: dict = None,
                              user: dict = Depends(get_current_user)):
    """Sign out other devices: bumps the token version so every previously issued
    JWT stops validating. The current device is re-issued a fresh token and stays
    signed in — unless ``everywhere`` is true, in which case this device is signed
    out too and must log back in."""
    everywhere = bool((body or {}).get("everywhere"))
    new_v = user.get("token_version", 0) + 1
    await db.users.update_one({"id": user["id"]},
                              {"$set": {"token_version": new_v}})
    user = {**user, "token_version": new_v}
    await _audit(user, "auth.sessions.revoke_all", "user", user["id"],
                 {"everywhere": everywhere})
    if everywhere:
        clear_session_cookie(response)
    else:
        await _issue(response, user)
    return {"ok": True, "everywhere": everywhere}


class TenantSettingsBody(BaseModel):
    inventory_enabled: bool = Field(...)


@router.patch("/tenant/settings")
async def update_tenant_settings(body: TenantSettingsBody,
                                 user: dict = Depends(require("settings:write"))):
    """Owner toggle for the inventory add-on. Data is kept when the add-on is
    turned off and comes back when it's turned back on."""
    await db.tenants.update_one(
        {"id": user["tenant_id"]},
        {"$set": {"settings.inventory_enabled": body.inventory_enabled}})
    await _audit(user, "settings.inventory_toggled", "tenant", user["tenant_id"],
                 {"inventory_enabled": body.inventory_enabled})
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    return {"settings": (tenant or {}).get("settings", {})}


@router.post("/logout")
async def logout(response: Response, user: dict = Depends(get_current_user)):
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    out = _public(user)
    out["tenant"] = tenant
    return out


@router.get("/team")
async def team(user: dict = Depends(get_current_user)):
    # Encrypted payout fields are never listed; only their last-4 digits.
    rows = await db.users.find(
        {"tenant_id": user["tenant_id"]},
        {"_id": 0, "password_hash": 0, "totp_secret_enc": 0, "totp_pending_enc": 0,
         "payout_account_enc": 0, "payout_routing_enc": 0},
    ).to_list(200)
    return rows


class PayoutUpdateBody(BaseModel):
    payout_account: str = Field(default="", max_length=34)
    payout_routing: str = Field(default="", max_length=12)


@router.put("/team/{id}/payout")
async def update_team_payout(id: str, body: PayoutUpdateBody,
                             user: dict = Depends(require("ops:manage"))):
    """Store a team member's payout account/routing Fernet-encrypted.
    Empty fields are ignored; last-4 digits are kept for display."""
    patch: dict = {}
    acct = (body.payout_account or "").strip()
    if acct:
        patch["payout_account_enc"] = pii_encrypt(acct)
        patch["payout_account_last4"] = last4(acct)
    route = (body.payout_routing or "").strip()
    if route:
        patch["payout_routing_enc"] = pii_encrypt(route)
        patch["payout_routing_last4"] = last4(route)
    if not patch:
        raise HTTPException(status_code=400, detail="Nothing to update")
    res = await db.users.update_one(
        {"id": id, "tenant_id": user["tenant_id"]}, {"$set": patch})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Member not found")
    await _audit(user, "team.payout.updated", "user", id,
                 {"account_updated": "payout_account_enc" in patch,
                  "routing_updated": "payout_routing_enc" in patch})
    row = await db.users.find_one(
        {"id": id, "tenant_id": user["tenant_id"]},
        {"_id": 0, "payout_account_last4": 1, "payout_routing_last4": 1})
    return {"ok": True, **(row or {})}


@router.get("/team/{id}/payout")
async def team_payout(id: str, user: dict = Depends(require("pii:read"))):
    """Full payout account details — pii:read only. Never logged."""
    u = await db.users.find_one({"id": id, "tenant_id": user["tenant_id"]}, {"_id": 0})
    if not u:
        raise HTTPException(status_code=404, detail="Member not found")
    await _audit(user, "team.payout.viewed", "user", id)
    return {
        "account": pii_decrypt(u["payout_account_enc"]) if u.get("payout_account_enc") else "",
        "routing": pii_decrypt(u["payout_routing_enc"]) if u.get("payout_routing_enc") else "",
    }


@router.post("/migrate/encrypt-pii")
async def migrate_encrypt_pii(user: dict = Depends(require("ledger:admin"))):
    """Owner-only, idempotent: encrypt any plaintext PII still in the DB.

    - contractors.tax_id -> tax_id_enc + tax_id_last4 (plaintext then unset)
    - contractors.tax_id_enc holding PLAINTEXT (pre-encryption router wrote it
      directly) -> encrypted in place; Fernet tokens start with "gAAAAA"
    - users.payout_account / payout_routing -> payout_*_enc + *_last4 (plaintext then unset)
    Run after any agent stored plaintext; safe to run repeatedly.
    """
    try:
        pii_encrypt("probe")
    except HTTPException as e:
        raise e
    contractors_n = 0
    async for c in db.contractors.find(
            {"tax_id": {"$exists": True, "$ne": ""}, "tax_id_enc": {"$exists": False}}):
        tax_id = (c.get("tax_id") or "").strip()
        if not tax_id:
            continue
        await db.contractors.update_one(
            {"_id": c["_id"]},
            {"$set": {"tax_id_enc": pii_encrypt(tax_id), "tax_id_last4": last4(tax_id)},
             "$unset": {"tax_id": ""}})
        contractors_n += 1
    # Plaintext that landed directly in tax_id_enc (pre-encryption router).
    # Fernet tokens start with "gAAAAA"; anything else is plaintext to migrate.
    async for c in db.contractors.find({"tax_id_enc": {"$exists": True, "$ne": ""}}):
        enc = (c.get("tax_id_enc") or "").strip()
        if not enc or enc.startswith("gAAAAA"):
            continue
        await db.contractors.update_one(
            {"_id": c["_id"]},
            {"$set": {"tax_id_enc": pii_encrypt(enc), "tax_id_last4": last4(enc)}})
        contractors_n += 1
    users_n = 0
    async for u in db.users.find({"$or": [{"payout_account": {"$exists": True, "$ne": ""}},
                                          {"payout_routing": {"$exists": True, "$ne": ""}}]}):
        patch: dict = {}
        unset: dict = {}
        acct = (u.get("payout_account") or "").strip()
        if acct and not u.get("payout_account_enc"):
            patch["payout_account_enc"] = pii_encrypt(acct)
            patch["payout_account_last4"] = last4(acct)
            unset["payout_account"] = ""
        route = (u.get("payout_routing") or "").strip()
        if route and not u.get("payout_routing_enc"):
            patch["payout_routing_enc"] = pii_encrypt(route)
            patch["payout_routing_last4"] = last4(route)
            unset["payout_routing"] = ""
        if patch:
            op: dict = {"$set": patch}
            if unset:
                op["$unset"] = unset
            await db.users.update_one({"_id": u["_id"]}, op)
            users_n += 1
    await _audit(user, "pii.migrate", "system", "pii",
                 {"contractors": contractors_n, "users": users_n})
    return {"ok": True, "contractors_encrypted": contractors_n, "users_encrypted": users_n}


class SwitchBody(BaseModel):
    role: str


@router.post("/switch-role")
async def switch_role(body: SwitchBody, response: Response, user: dict = Depends(get_current_user)):
    """Demo convenience: log in as the seeded user with the requested role in the same workspace."""
    target = await db.users.find_one({"tenant_id": user["tenant_id"], "role": body.role})
    if not target:
        raise HTTPException(status_code=404, detail=f"No {body.role} user in this workspace")
    await _issue(response, target)
    return _public(target)
