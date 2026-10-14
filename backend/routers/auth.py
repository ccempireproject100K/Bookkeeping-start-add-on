"""Auth: register (new workspace), login, logout, me, demo role switch."""
import os
from fastapi import APIRouter, Response, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr, field_validator

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import (
    hash_password, verify_password, create_access_token,
    set_session_cookie, clear_session_cookie, get_current_user,
)
from lib.coa import seed_coa

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterBody(BaseModel):
    email: EmailStr
    password: str
    name: str
    shop_name: str = "My Barbershop"

    @field_validator("password")
    @classmethod
    def validate_password(cls, password: str) -> str:
        # bcrypt hashes only the first 72 bytes; reject longer values rather than
        # silently treating distinct supplied passwords as equivalent.
        if not 12 <= len(password.encode("utf-8")) <= 72:
            raise ValueError("Password must be between 12 and 72 UTF-8 bytes")
        return password

    @field_validator("name", "shop_name")
    @classmethod
    def validate_names(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 120:
            raise ValueError("Name must be between 1 and 120 characters")
        return value


class LoginBody(BaseModel):
    email: EmailStr
    password: str


async def _issue(response: Response, user: dict):
    token = create_access_token(user["id"], user["email"])
    set_session_cookie(response, token)


def _public(user: dict) -> dict:
    return {k: v for k, v in user.items() if k not in ("password_hash", "_id")}


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
async def login(body: LoginBody, response: Response):
    user = await db.users.find_one({"email": body.email.lower().strip()})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    await _issue(response, user)
    return _public(user)


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
    rows = await db.users.find(
        {"tenant_id": user["tenant_id"]},
        {"_id": 0, "password_hash": 0},
    ).to_list(200)
    return rows


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
