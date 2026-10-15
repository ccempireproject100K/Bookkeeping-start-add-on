"""Passkeys (WebAuthn): phishing-proof sign-in with a hardware key or
platform authenticator (Touch ID, Windows Hello, …).

Flows:
  Registration (signed in):  POST /webauthn/register/options
                             POST /webauthn/register/verify {credential, name}
  Passwordless login:        POST /webauthn/login/options {email}
                             POST /webauthn/login/verify {email, credential}
  Management (signed in):    GET /webauthn/credentials, DELETE /webauthn/credentials/{id}

Configuration (environment):
  WEBAUTHN_RP_ID   relying-party id, i.e. the app's domain (default: localhost)
  WEBAUTHN_ORIGIN  full origin, e.g. https://books.example.com
                   (default: http://localhost:3000 when RP_ID is localhost)
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
)

from db import db
from lib.alerts import raise_alert
from lib.auth import get_current_user
from lib.base import new_id

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webauthn", tags=["webauthn"])

_CHALLENGE_TTL = timedelta(minutes=5)
RP_NAME = "Bookkeeping"


def _rp():
    rp_id = os.environ.get("WEBAUTHN_RP_ID", "localhost")
    origin = os.environ.get(
        "WEBAUTHN_ORIGIN",
        "http://localhost:3000" if rp_id == "localhost" else f"https://{rp_id}",
    )
    return rp_id, origin


async def _store_challenge(kind: str, user_id: str, challenge: str):
    await db.webauthn_challenges.delete_many({"user_id": user_id, "kind": kind})
    await db.webauthn_challenges.insert_one({
        "id": new_id(), "kind": kind, "user_id": user_id,
        "challenge": challenge,
        "created_at": datetime.now(timezone.utc),
    })


async def _take_challenge(kind: str, user_id: str) -> str:
    doc = await db.webauthn_challenges.find_one(
        {"user_id": user_id, "kind": kind}, sort=[("created_at", -1)])
    if not doc:
        raise HTTPException(400, "No pending WebAuthn ceremony — request options first")
    await db.webauthn_challenges.delete_many({"user_id": user_id, "kind": kind})
    age = datetime.now(timezone.utc) - doc["created_at"].replace(tzinfo=timezone.utc)
    if age > _CHALLENGE_TTL:
        raise HTTPException(400, "WebAuthn challenge expired — try again")
    return doc["challenge"]


async def _credentials(user_id: str) -> list:
    return await db.webauthn_credentials.find(
        {"user_id": user_id}, {"_id": 0}).to_list(25)


# ---------------- Registration (signed in) ----------------

class RegisterVerifyBody(BaseModel):
    credential: dict = Field(...)
    name: str = Field(default="Passkey", max_length=60)


@router.post("/register/options")
async def register_options(request: Request, user: dict = Depends(get_current_user)):
    rp_id, _origin = _rp()
    # Don't offer to register a passkey that's already registered.
    exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
               for c in await _credentials(user["id"])]
    options = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME,
        user_id=user["id"].encode(),
        user_name=user.get("email", user["id"]),
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification="preferred"),
    )
    await _store_challenge("reg", user["id"], bytes_to_base64url(options.challenge))
    return json.loads(options_to_json(options))


@router.post("/register/verify")
async def register_verify(body: RegisterVerifyBody,
                          user: dict = Depends(get_current_user)):
    rp_id, origin = _rp()
    challenge = await _take_challenge("reg", user["id"])
    try:
        verified = verify_registration_response(
            credential=body.credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id,
            expected_origin=origin,
        )
    except Exception as e:
        log.info("webauthn registration failed: %s", e)
        raise HTTPException(400, "Passkey registration failed — try again")
    await db.webauthn_credentials.insert_one({
        "id": new_id(), "user_id": user["id"], "tenant_id": user["tenant_id"],
        "credential_id": bytes_to_base64url(verified.credential_id),
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": verified.sign_count,
        "name": body.name.strip() or "Passkey",
        "created_at": datetime.now(timezone.utc),
    })
    await raise_alert(user["tenant_id"], user["id"], "passkey_added",
                      "Passkey added",
                      f"A new passkey ({body.name.strip() or 'Passkey'}) was registered "
                      f"on {user.get('email')}.")
    return {"ok": True}


@router.get("/credentials")
async def list_credentials(user: dict = Depends(get_current_user)):
    creds = await _credentials(user["id"])
    return {"credentials": [
        {"id": c["id"], "name": c.get("name", "Passkey"),
         "created_at": c.get("created_at")} for c in creds]}


@router.delete("/credentials/{cred_id}")
async def delete_credential(cred_id: str, user: dict = Depends(get_current_user)):
    res = await db.webauthn_credentials.delete_one(
        {"id": cred_id, "user_id": user["id"]})
    if not res.deleted_count:
        raise HTTPException(404, "Passkey not found")
    return {"ok": True}


# ---------------- Passwordless login ----------------

class LoginOptionsBody(BaseModel):
    email: str = Field(..., max_length=254)


class LoginVerifyBody(BaseModel):
    email: str = Field(..., max_length=254)
    credential: dict = Field(...)


@router.post("/login/options")
async def login_options(body: LoginOptionsBody):
    rp_id, _origin = _rp()
    user = await db.users.find_one({"email": body.email.lower().strip()})
    if not user:
        # Don't reveal whether the email exists; still return syntactically
        # valid options would leak nothing — instead fail generically.
        raise HTTPException(401, "Invalid email or password")
    creds = await _credentials(user["id"])
    if not creds:
        raise HTTPException(400, "No passkeys registered for this account")
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[PublicKeyCredentialDescriptor(
            id=base64url_to_bytes(c["credential_id"])) for c in creds],
    )
    await _store_challenge("auth", user["id"], bytes_to_base64url(options.challenge))
    return json.loads(options_to_json(options))


@router.post("/login/verify")
async def login_verify(body: LoginVerifyBody, request: Request, response: Response):
    from routers.auth import _audit, _issue, _public  # lazy: avoid import cycle
    from lib.devices import check_device

    rp_id, origin = _rp()
    email = body.email.lower().strip()
    ip = request.client.host if request.client else "unknown"
    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(401, "Invalid email or password")
    challenge = await _take_challenge("auth", user["id"])
    cred_id_b64 = body.credential.get("id", "")
    stored = await db.webauthn_credentials.find_one(
        {"user_id": user["id"], "credential_id": cred_id_b64})
    if not stored:
        raise HTTPException(401, "Unknown passkey")
    try:
        verified = verify_authentication_response(
            credential=body.credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(stored["public_key"]),
            credential_current_sign_count=stored.get("sign_count", 0),
        )
    except Exception as e:
        log.info("webauthn login failed for %s: %s", email, e)
        raise HTTPException(401, "Passkey verification failed")
    # Clone-detection: a real authenticator advances its signature counter on
    # every use. Only judge when both sides actually use counters — many
    # platform authenticators report 0 and must not be flagged.
    prev_count = stored.get("sign_count", 0) or 0
    new_count = verified.new_sign_count or 0
    if prev_count and new_count and new_count <= prev_count:
        await raise_alert(user["tenant_id"], user["id"], "passkey_clone",
                          "Possible passkey cloning detected",
                          f"A passkey login for {email} presented a stale signature "
                          f"counter — the credential may have been cloned.")
        raise HTTPException(401, "Passkey verification failed")
    await db.webauthn_credentials.update_one(
        {"id": stored["id"]},
        {"$set": {"sign_count": new_count}})
    await db.login_attempts.delete_many({"email": email})
    await _issue(response, user)
    await _audit(user, "auth.login.passkey", "user", user["id"], {"ip": ip})
    await check_device(user, request)
    return _public(user)
