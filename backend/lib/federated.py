"""Provider-agnostic federated (social) sign-in.

ONE plumbing layer for every identity provider — Google, Facebook,
Microsoft, LinkedIn, and an Apple-ready slot. Providers are data in
``PROVIDERS``; the auth router drives the same start/callback pipeline
for all of them:

    GET  /api/auth/{provider}            -> redirect to provider consent
    GET|POST /api/auth/{provider}/callback -> exchange + verify + sign in

Env gate (house pattern, per provider): a provider is ENABLED only when
its ``<PREFIX>_CLIENT_ID``, ``<PREFIX>_CLIENT_SECRET`` and
``<PREFIX>_REDIRECT_URI`` are all set in the environment. Absent keys =
provider invisible on the frontend, its endpoints answer 404, and
email/password behaviour is completely untouched. Nothing is ever faked:
with no keys there is no button and no flow.

Identity model (shared): a verified provider identity is
``{provider, provider_user_id, email, email_verified, name}``. It links
to a local account by (provider, provider_user_id) first, then by
VERIFIED email (never a duplicate account). Several providers can link
to ONE account; links live on the user record as ``identities: [...]``
plus a legacy ``google_sub`` mirror for Google.

Apple is READY-ONLY this wave: registry entry, OIDC verifier and the
form_post callback shape exist, but ``ready_only`` keeps it disabled
even if keys appear — activation is gated on the paid Apple Developer
Program ($99/yr), an owner money decision. Apple identities link by
provider user id ONLY (private-relay emails are never force-matched).

Token verification is always SERVER-SIDE:
- OIDC providers (google/microsoft/linkedin/apple): the ID token's
  RS256 signature is checked against the provider JWKS, plus issuer,
  audience (our client id) and expiry; ``email_verified`` must be true
  where the provider asserts it (required for google/linkedin).
- Facebook (no OIDC ID token in the Login flow): the access token is
  validated through Graph ``debug_token`` against our app id, then the
  profile is read from ``/me``; a missing email refuses sign-in.
  DEVIATION (as found by the AI Office crew): ``/me`` returns NO
  ``email_verified`` field — an email returned for an app-validated
  token is treated as provider-attested.
- Microsoft DEVIATION (as found by the AI Office crew): Entra ID
  tokens carry NO ``email_verified`` claim and Microsoft calls the
  ``email`` claim mutable. Verification is MAPPED from the optional
  claims that do assert it: ``email_verified=true``, or
  ``xms_edov=true`` (email-domain owner verified), or a
  ``verified_primary_email`` equal to the token's email. Without one,
  the identity is UNVERIFIED: it may sign in ONLY by an existing
  (provider, subject) link — it can never link by email and can
  never create an account (enforced in the auth router).

Tests use FAKE providers: they monkeypatch ``_post_form`` / ``_get_json``
(the only HTTP egress in this module) and ``get_jwk_client`` (the only
JWKS source), and sign tokens with a throwaway RSA key.
"""
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode

import jwt
import requests
from jwt import PyJWKClient

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


def _ms_tenant() -> str:
    return os.environ.get("MICROSOFT_TENANT", "common").strip() or "common"


@dataclass(frozen=True)
class Provider:
    name: str
    env_prefix: str
    label: str
    kind: str  # "oidc" | "facebook"
    auth_url: str
    token_url: str
    scopes: str
    jwks_url: str = ""
    issuers: tuple = ()          # exact OIDC issuers (microsoft handled specially)
    userinfo_url: str = ""
    require_email_verified: bool = True
    ready_only: bool = False     # apple: plumbing exists, activation gated
    extra_auth_params: dict = field(default_factory=dict)


def _providers() -> dict:
    tenant = _ms_tenant()
    return {
        "google": Provider(
            name="google", env_prefix="GOOGLE", label="Google", kind="oidc",
            auth_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            jwks_url="https://www.googleapis.com/oauth2/v3/certs",
            issuers=GOOGLE_ISSUERS,
            scopes="openid email profile",
            extra_auth_params={"access_type": "online", "prompt": "select_account"},
        ),
        "facebook": Provider(
            name="facebook", env_prefix="FACEBOOK", label="Facebook", kind="facebook",
            auth_url="https://www.facebook.com/dialog/oauth",
            token_url="https://graph.facebook.com/oauth/access_token",
            scopes="email,public_profile",
            require_email_verified=False,  # no verified flag exists; debug_token + email presence gate instead
        ),
        "microsoft": Provider(
            name="microsoft", env_prefix="MICROSOFT", label="Microsoft", kind="oidc",
            auth_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            jwks_url=f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
            issuers=(),  # validated by pattern: https://login.microsoftonline.com/<tid>/v2.0
            scopes="openid email profile",
            # Microsoft asserts no email_verified claim: verification is
            # mapped from email_verified / xms_edov / verified_primary_email
            # in verify_oidc_id_token, and an unmapped identity is
            # subject-link-only (see module docstring). The token itself
            # is NOT refused here — a returning subject link must work.
            require_email_verified=False,
        ),
        "linkedin": Provider(
            name="linkedin", env_prefix="LINKEDIN", label="LinkedIn", kind="oidc",
            auth_url="https://www.linkedin.com/oauth/v2/authorization",
            token_url="https://www.linkedin.com/oauth/v2/accessToken",
            jwks_url="https://www.linkedin.com/oauth/openid/jwks",
            issuers=("https://www.linkedin.com",),
            userinfo_url="https://api.linkedin.com/v2/userinfo",
            scopes="openid profile email",
        ),
        "apple": Provider(
            name="apple", env_prefix="APPLE", label="Apple", kind="oidc",
            auth_url="https://appleid.apple.com/auth/authorize",
            token_url="https://appleid.apple.com/auth/token",
            jwks_url="https://appleid.apple.com/auth/keys",
            issuers=("https://appleid.apple.com",),
            scopes="name email",
            require_email_verified=False,
            ready_only=True,  # $99/yr Apple Developer gate — owner decision
            extra_auth_params={"response_mode": "form_post"},
        ),
    }


PROVIDERS = _providers()


def get_provider(name: str) -> Optional[Provider]:
    # Microsoft endpoints embed the tenant, which is env-driven; rebuild
    # so a tenant change (or test monkeypatch) is honoured.
    return _providers().get((name or "").lower())


def client_id(provider: Provider) -> str:
    return os.environ.get(f"{provider.env_prefix}_CLIENT_ID", "").strip()


def client_secret(provider: Provider) -> str:
    return os.environ.get(f"{provider.env_prefix}_CLIENT_SECRET", "").strip()


def redirect_uri(provider: Provider) -> str:
    return os.environ.get(f"{provider.env_prefix}_REDIRECT_URI", "").strip()


def is_enabled(provider: Provider) -> bool:
    """Env gate: all three keys present — and ready-only providers (Apple)
    stay OFF until the owner clears their external gate."""
    if provider.ready_only:
        return False
    return bool(client_id(provider) and client_secret(provider)
                and redirect_uri(provider))


def public_config() -> dict:
    """What the frontend may know: which providers are on. No secrets,
    no client ids beyond what the browser needs to start the flow (it
    only needs the backend start URL)."""
    out = {}
    for name, p in _providers().items():
        out[name] = {"enabled": is_enabled(p), "label": p.label,
                     "ready_only": p.ready_only}
    return {"providers": out,
            "google_enabled": out["google"]["enabled"]}


# ---------------------------------------------------------------- HTTP --
# The ONLY network egress in this module. Tests monkeypatch these two
# helpers to run a fake provider — no real provider is ever contacted
# in tests, and no real credentials exist in this repo.
def _post_form(url: str, data: dict) -> dict:
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()


def _get_json(url: str, params: dict = None, headers: dict = None) -> dict:
    r = requests.get(url, params=params or {}, headers=headers or {}, timeout=15)
    r.raise_for_status()
    return r.json()


_jwk_clients: dict = {}


def get_jwk_client(jwks_url: str) -> PyJWKClient:
    if jwks_url not in _jwk_clients:
        _jwk_clients[jwks_url] = PyJWKClient(jwks_url)
    return _jwk_clients[jwks_url]


# ------------------------------------------------------------- identity --
@dataclass
class Identity:
    provider: str
    provider_user_id: str
    email: str = ""
    email_verified: bool = False
    name: str = ""


class FederatedError(Exception):
    """Any verification failure. The callback turns this into a refused
    sign-in — never a half-created account."""


def authorization_url(provider: Provider, state: str) -> str:
    params = {
        "client_id": client_id(provider),
        "redirect_uri": redirect_uri(provider),
        "response_type": "code",
        "scope": provider.scopes,
        "state": state,
        **provider.extra_auth_params,
    }
    return f"{provider.auth_url}?{urlencode(params)}"


def exchange_code(provider: Provider, code: str) -> dict:
    data = {
        "code": code,
        "client_id": client_id(provider),
        "client_secret": client_secret(provider),
        "redirect_uri": redirect_uri(provider),
        "grant_type": "authorization_code",
    }
    try:
        return _post_form(provider.token_url, data)
    except Exception as e:
        raise FederatedError(f"{provider.name}: code exchange failed: {e}")


def _issuer_ok(provider: Provider, iss: str) -> bool:
    if provider.name == "microsoft":
        if _ms_tenant() not in ("common", "organizations", "consumers"):
            return iss == f"https://login.microsoftonline.com/{_ms_tenant()}/v2.0"
        return bool(re.fullmatch(
            r"https://login\.microsoftonline\.com/[0-9a-fA-F-]{36}/v2\.0", iss or ""))
    return iss in provider.issuers


def _truthy(value) -> bool:
    return value is True or (isinstance(value, str)
                             and value.strip().lower() == "true")


def verify_oidc_id_token(provider: Provider, id_token: str) -> Identity:
    """Full server-side OIDC verification: RS256 signature via provider
    JWKS, issuer, audience == our client id, expiry, subject present,
    and email_verified == true where the provider must assert it."""
    if not id_token:
        raise FederatedError(f"{provider.name}: missing id_token")
    try:
        signing_key = get_jwk_client(provider.jwks_url).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key,
            algorithms=["RS256"],
            audience=client_id(provider),
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise FederatedError(f"{provider.name}: id_token expired")
    except jwt.InvalidAudienceError:
        raise FederatedError(f"{provider.name}: id_token audience mismatch")
    except jwt.InvalidTokenError as e:
        raise FederatedError(f"{provider.name}: id_token invalid: {e}")
    if not _issuer_ok(provider, claims.get("iss", "")):
        raise FederatedError(f"{provider.name}: id_token issuer mismatch")
    sub = str(claims.get("sub") or "")
    if not sub:
        raise FederatedError(f"{provider.name}: id_token has no subject")
    email = str(claims.get("email") or "").lower().strip()
    if provider.name == "microsoft" and not email:
        email = str(claims.get("preferred_username") or "").lower().strip()
    verified = _truthy(claims.get("email_verified"))
    if provider.name == "microsoft":
        # Deviation mapping (see module docstring): Entra asserts no
        # email_verified; xms_edov or a matching verified_primary_email
        # are the claims that DO assert verification.
        verified = bool(
            verified
            or _truthy(claims.get("xms_edov"))
            or (email and str(claims.get("verified_primary_email") or "")
                .lower().strip() == email))
    if provider.require_email_verified and not verified:
        raise FederatedError(f"{provider.name}: email is not verified")
    if provider.name != "apple" and "@" not in email:
        raise FederatedError(f"{provider.name}: id_token has no email")
    name = str(claims.get("name") or "").strip()
    if not name and email:
        name = email.split("@")[0]
    return Identity(provider=provider.name, provider_user_id=sub,
                    email=email, email_verified=verified, name=name)


def fetch_facebook_identity(provider: Provider, tokens: dict) -> Identity:
    """Facebook Login has no OIDC ID token: validate the access token
    server-side with debug_token (must be valid, unexpired, and issued
    for OUR app), then read the profile from /me. No email = refused."""
    access_token = tokens.get("access_token")
    if not access_token:
        raise FederatedError("facebook: missing access_token")
    app_token = f"{client_id(provider)}|{client_secret(provider)}"
    try:
        dbg = _get_json("https://graph.facebook.com/debug_token",
                        params={"input_token": access_token,
                                "access_token": app_token}).get("data", {})
    except Exception as e:
        raise FederatedError(f"facebook: debug_token failed: {e}")
    if not dbg.get("is_valid"):
        raise FederatedError("facebook: access token is not valid")
    if str(dbg.get("app_id")) != client_id(provider):
        raise FederatedError("facebook: access token was issued for another app")
    expires_at = dbg.get("expires_at") or 0
    if expires_at and int(expires_at) < int(time.time()):
        raise FederatedError("facebook: access token expired")
    try:
        me = _get_json("https://graph.facebook.com/me",
                       params={"fields": "id,name,email",
                               "access_token": access_token})
    except Exception as e:
        raise FederatedError(f"facebook: profile fetch failed: {e}")
    email = str(me.get("email") or "").lower().strip()
    if "@" not in email:
        raise FederatedError("facebook: no email returned by provider")
    # DEVIATION: /me carries no email_verified field; an email returned
    # for a debug_token-validated, app-scoped token is provider-attested.
    return Identity(provider="facebook",
                    provider_user_id=str(me.get("id") or ""),
                    email=email, email_verified=True,
                    name=str(me.get("name") or email.split("@")[0]))


def fetch_identity(provider: Provider, code: str) -> Identity:
    """Code -> verified Identity. The single pipeline every provider runs."""
    tokens = exchange_code(provider, code)
    if provider.kind == "facebook":
        return fetch_facebook_identity(provider, tokens)
    identity = verify_oidc_id_token(provider, tokens.get("id_token", ""))
    # LinkedIn sometimes carries the profile only in userinfo; if the ID
    # token lacked a name, top it up — identity fields (sub/email) still
    # come from the verified token alone.
    if provider.userinfo_url and tokens.get("access_token") and not identity.name:
        try:
            info = _get_json(provider.userinfo_url, headers={
                "Authorization": f"Bearer {tokens['access_token']}"})
            identity.name = str(info.get("name") or identity.name)
        except Exception:
            pass
    return identity
