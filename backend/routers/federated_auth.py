"""Federated sign-in routes — ONE pipeline for every provider.

    GET  /api/auth/config                  public provider on/off config
    GET  /api/auth/{provider}              start: redirect to provider
    GET  /api/auth/{provider}/callback     OIDC/OAuth callback (redirect flow)
    POST /api/auth/{provider}/callback     form_post callback (Apple shape)

Providers: google, facebook, microsoft, linkedin (+ apple ready-only,
always 404 until its owner gate clears). A provider whose env keys are
absent is OFF: start/callback answer 404 and the frontend hides its
button — email/password behaviour is untouched either way.

Account resolution (shared, in ``resolve_or_create_user``):
1. an existing (provider, provider_user_id) link wins — returning login
   (this step is verification-independent: a Microsoft identity whose
   token carried no verification claim still signs in by subject);
2. else a VERIFIED-email match links the identity to that account —
   never a duplicate (Apple is the exception: private-relay emails are
   never force-matched, Apple links by provider id only);
3. else a brand-new signup through ``create_workspace`` — in
   Bookkeeping ordinary registration IS the operator grant
   (role "owner" -> ledger:admin), and a social signup gets exactly
   that, no more, no less.

An UNVERIFIED identity (non-Apple) can do step 1 ONLY: it never links
by email and never creates an account. Microsoft is the live case —
its verified flag comes from the xms_edov / verified_primary_email
mapping in ``lib.federated``; Facebook's /me email is provider-attested
(no email_verified field exists there).
"""
import os
import secrets
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import RedirectResponse

from db import db
from lib import federated
from lib.base import now_iso
from routers.auth import create_workspace, _issue, _audit

router = APIRouter(prefix="/auth", tags=["auth"])

_STATE_COOKIE = "oauth_state"


def _frontend(path: str = "/") -> str:
    base = os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}{path}"


def _provider_or_404(name: str) -> federated.Provider:
    provider = federated.get_provider(name)
    if provider is None or not federated.is_enabled(provider):
        raise HTTPException(status_code=404, detail="Provider not enabled")
    return provider


@router.get("/config")
async def auth_config():
    """Public: which sign-in providers are enabled (frontend buttons)."""
    return federated.public_config()


@router.get("/{provider}")
async def start(provider: str):
    p = _provider_or_404(provider)
    state = secrets.token_urlsafe(32)
    resp = RedirectResponse(federated.authorization_url(p, state), status_code=302)
    resp.set_cookie(_STATE_COOKIE, f"{p.name}:{state}", httponly=True,
                    secure=True, samesite="lax", max_age=600, path="/")
    return resp


async def resolve_or_create_user(identity: federated.Identity) -> dict:
    """Link-or-create for a VERIFIED provider identity. Returns the user."""
    # 1) Returning login: this provider identity is already linked.
    user = await db.users.find_one(
        {"identities": {"$elemMatch": {
            "provider": identity.provider,
            "provider_user_id": identity.provider_user_id}}},
        {"_id": 0})
    if user is None and identity.provider == "google":
        user = await db.users.find_one(
            {"google_sub": identity.provider_user_id}, {"_id": 0})
    if user is not None:
        return user
    # 2) Link by VERIFIED email — same account, never a duplicate.
    #    Apple never reaches this branch: relay emails are not identity.
    #    An unverified identity (any non-Apple provider) stops here:
    #    no email link AND no signup below — subject-link only.
    if identity.provider != "apple" and not identity.email_verified:
        raise HTTPException(status_code=401,
                            detail="Provider email is not verified")
    if identity.provider != "apple" and identity.email:
        user = await db.users.find_one({"email": identity.email}, {"_id": 0})
        if user is not None:
            link = {"provider": identity.provider,
                    "provider_user_id": identity.provider_user_id,
                    "email": identity.email, "linked_at": now_iso()}
            patch = {"$addToSet": {"identities": link}}
            if identity.provider == "google" and not user.get("google_sub"):
                patch["$set"] = {"google_sub": identity.provider_user_id}
            await db.users.update_one({"id": user["id"]}, patch)
            user.setdefault("identities", []).append(link)
            if identity.provider == "google":
                user.setdefault("google_sub", identity.provider_user_id)
            return user
    # 3) New signup — ordinary registration, operator grant included,
    #    via the exact same create_workspace path as email signup.
    if not identity.email or "@" not in identity.email:
        raise HTTPException(status_code=401,
                            detail="Provider returned no usable email")
    name = identity.name or identity.email.split("@")[0]
    user = await create_workspace(identity.email, None, name, "My Shop",
                                  identity=identity)
    return user


async def _finish(provider: federated.Provider, identity: federated.Identity,
                  request: Request) -> RedirectResponse:
    user = await resolve_or_create_user(identity)
    resp = RedirectResponse(_frontend("/"), status_code=302)
    await _issue(resp, user)
    try:
        await _audit(user, "auth.login", "user", user["id"],
                     {"provider": provider.name})
    except Exception:
        pass  # audit must never break a successful sign-in
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp


def _state_ok(request: Request, provider: federated.Provider, state: str) -> bool:
    cookie = request.cookies.get(_STATE_COOKIE, "")
    return bool(state) and cookie == f"{provider.name}:{state}"


@router.get("/{provider}/callback")
async def callback_get(provider: str, request: Request,
                       code: str = "", state: str = "", error: str = ""):
    p = _provider_or_404(provider)
    if error or not code:
        return RedirectResponse(_frontend("/login?social_error=provider"),
                                status_code=302)
    if not _state_ok(request, p, state):
        return RedirectResponse(_frontend("/login?social_error=state"),
                                status_code=302)
    try:
        identity = federated.fetch_identity(p, code)
        return await _finish(p, identity, request)
    except federated.FederatedError:
        return RedirectResponse(_frontend("/login?social_error=verify"),
                                status_code=302)
    except HTTPException:
        return RedirectResponse(_frontend("/login?social_error=verify"),
                                status_code=302)


@router.post("/{provider}/callback")
async def callback_post(provider: str, request: Request):
    """form_post callback shape (Apple's response_mode=form_post, kept
    provider-generic). Apple itself stays 404 while ready-only."""
    p = _provider_or_404(provider)
    form = await request.form()
    code = str(form.get("code") or "")
    state = str(form.get("state") or "")
    if not code or not _state_ok(request, p, state):
        return RedirectResponse(_frontend("/login?social_error=state"),
                                status_code=302)
    try:
        identity = federated.fetch_identity(p, code)
    except federated.FederatedError:
        return RedirectResponse(_frontend("/login?social_error=verify"),
                                status_code=302)
    # Apple may send the user's name exactly once, inside `user`.
    if p.name == "apple" and form.get("user") and not identity.name:
        try:
            import json
            u = json.loads(str(form["user"]))
            nm = (u.get("name") or {})
            identity.name = " ".join(
                x for x in [nm.get("firstName"), nm.get("lastName")] if x)
        except Exception:
            pass
    try:
        return await _finish(p, identity, request)
    except HTTPException:
        return RedirectResponse(_frontend("/login?social_error=verify"),
                                status_code=302)
