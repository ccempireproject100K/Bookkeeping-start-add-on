"""JWT cookie auth, password hashing, tenant resolution, role permissions."""
import os
import jwt
import bcrypt
from datetime import datetime, timezone, timedelta
from fastapi import Request, HTTPException, Depends

from db import db

JWT_ALG = "HS256"


def _secret() -> str:
    return os.environ["JWT_SECRET"]


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(user_id: str, email: str, version: int = 0) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "type": "access",
        "v": version,  # bumped by "sign out all devices"; old tokens die
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALG)


def set_session_cookie(response, token: str):
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=604800,
        path="/",
    )


def clear_session_cookie(response):
    response.delete_cookie("access_token", path="/", secure=True, samesite="none")


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    # Token versioning: "sign out all devices" bumps token_version, killing
    # every previously issued JWT without any server-side session store.
    if payload.get("v", 0) != user.get("token_version", 0):
        raise HTTPException(status_code=401, detail="Session revoked")
    return user


# ---- Roles & permission map ----
ROLES = {"owner", "accountant", "bookkeeper", "barber"}

# action -> roles allowed
PERMISSIONS = {
    "ledger:read": {"owner", "accountant", "bookkeeper"},
    "ledger:post_draft": {"owner", "bookkeeper"},
    "ledger:approve": {"owner"},
    "ledger:admin": {"owner"},            # accounts, periods, migration, cutover
    "ops:sell": {"owner", "barber"},
    "ops:manage": {"owner"},              # products, invoices, cash close
    "dashboard:read": {"owner", "accountant", "bookkeeper", "barber"},
    # ---- Ported feature permissions (adapted from public repo) ----
    "time:manage": {"owner", "accountant", "bookkeeper", "barber"},  # clock in/out, own entries
    "time:approve": {"owner"},            # view anyone's entries, approve, close
    "payroll:read": {"owner", "accountant"},
    "payroll:write": {"owner"},
    "budget:read": {"owner", "accountant", "bookkeeper"},
    "budget:write": {"owner"},
    "tax:read": {"owner", "accountant", "bookkeeper"},
    "tax:write": {"owner"},
    "pii:read": {"owner", "accountant"},  # full PII values (tax IDs, payout accounts)
    "audit:read": {"owner", "accountant"},  # read-only audit trail viewer
    "settings:write": {"owner"},  # workspace settings: fiscal close, equity, inventory add-on
}


def require(action: str):
    async def dep(user: dict = Depends(get_current_user)) -> dict:
        allowed = PERMISSIONS.get(action, set())
        if user.get("role") not in allowed:
            raise HTTPException(status_code=403, detail=f"Role '{user.get('role')}' cannot perform {action}")
        return user
    return dep
