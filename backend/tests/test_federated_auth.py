"""Federated (social) sign-in tests — FAKE providers only.

No real Google/Facebook/Microsoft/LinkedIn/Apple credentials exist or
are used. OIDC tokens are signed with a throwaway RSA key whose public
half is served through a stubbed JWKS client; provider HTTP calls are
stubbed at lib.federated._post_form / _get_json (the module's only
egress). The DB is an in-memory fake shaped like the existing auth
tests' mocks.

Covers, PER PROVIDER (google/facebook/microsoft/linkedin):
new signup, returning login, link-by-verified-email (no duplicate),
bad/unverified token refused, provider-off invisibility, and
registration-grant parity (a social signup is an ordinary registration:
role "owner" -> ledger:admin, exactly like email signup).
Plus: Apple ready-only behaviour, multi-provider linking to ONE
account, and the email/password login regression.
"""
import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Self-contained env (same setdefault pattern as the other suites):
# db.py reads MONGO_URL/DB_NAME at import; lib.auth reads JWT_SECRET
# when issuing a session. Dummy values only — no real DB is contacted
# (the DB is the in-memory fake below) and no real secret exists here.
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "federated-test-secret")

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException

from lib import federated
from lib.auth import PERMISSIONS
from routers import auth, federated_auth


# ------------------------------------------------------------ fake DB --
def _matches(doc, query):
    for key, want in query.items():
        if isinstance(want, dict) and "$elemMatch" in want:
            rows = doc.get(key) or []
            if not any(all(r.get(k) == v for k, v in want["$elemMatch"].items())
                       for r in rows):
                return False
        elif isinstance(want, dict):
            continue  # range ops etc. — not used for identity lookups
        elif doc.get(key) != want:
            return False
    return True


class FakeCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return d
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, query, update):
        for d in self.docs:
            if _matches(d, query):
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                for k, v in (update.get("$addToSet") or {}).items():
                    d.setdefault(k, [])
                    if v not in d[k]:
                        d[k].append(v)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _matches(d, query))

    async def delete_many(self, query):
        self.docs = [d for d in self.docs if not _matches(d, query)]
        return SimpleNamespace(deleted_count=0)


@pytest.fixture()
def store(monkeypatch):
    users, tenants = FakeCollection(), FakeCollection()
    for mod in (auth, federated_auth):
        monkeypatch.setattr(mod.db, "users", users)
        monkeypatch.setattr(mod.db, "tenants", tenants)
        monkeypatch.setattr(mod.db, "audit_log", FakeCollection())
        monkeypatch.setattr(mod.db, "login_attempts", FakeCollection())
        monkeypatch.setattr(mod.db, "accounts", FakeCollection())
    monkeypatch.setattr(auth, "seed_coa", AsyncMock())
    monkeypatch.setattr(auth, "check_device", AsyncMock())
    return SimpleNamespace(users=users, tenants=tenants)


# ------------------------------------------------------- fake signers --
def _rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    return key, priv


OTHER_KEY, OTHER_PEM = _rsa_pem()

ISSUERS = {
    "google": "https://accounts.google.com",
    "microsoft": "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0",
    "linkedin": "https://www.linkedin.com",
    "apple": "https://appleid.apple.com",
}
CLIENT_IDS = {"google": "google-client-id", "facebook": "facebook-client-id",
              "microsoft": "microsoft-client-id", "linkedin": "linkedin-client-id",
              "apple": "apple-client-id"}
OIDC_PROVIDERS = ["google", "microsoft", "linkedin"]
ALL_PROVIDERS = ["google", "facebook", "microsoft", "linkedin"]


@pytest.fixture()
def keys(monkeypatch):
    key, pem = _rsa_pem()
    pub = key.public_key()

    class FakeJWKClient:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=pub)

    monkeypatch.setattr(federated, "get_jwk_client",
                        lambda url: FakeJWKClient())
    return SimpleNamespace(priv=pem, pub=pub)


def set_env(monkeypatch, provider_name):
    prefix = provider_name.upper()
    monkeypatch.setenv(f"{prefix}_CLIENT_ID", CLIENT_IDS[provider_name])
    monkeypatch.setenv(f"{prefix}_CLIENT_SECRET", f"{provider_name}-secret")
    monkeypatch.setenv(f"{prefix}_REDIRECT_URI",
                       f"https://bookkeeping-api.onrender.com/api/auth/{provider_name}/callback")


def clear_env(monkeypatch):
    for name in ALL_PROVIDERS + ["apple"]:
        for suffix in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
            monkeypatch.delenv(f"{name.upper()}_{suffix}", raising=False)


def make_token(provider_name, priv, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUERS[provider_name],
        "aud": CLIENT_IDS[provider_name],
        "sub": f"{provider_name}-user-1",
        "email": f"{provider_name}.user@example.com",
        "email_verified": True,
        "name": f"{provider_name.title()} User",
        "iat": now, "exp": now + 3600,
    }
    if provider_name == "microsoft":
        # Microsoft asserts no email_verified; xms_edov is one of the
        # claims the verification mapping accepts (see lib.federated).
        claims.pop("email_verified")
        claims["xms_edov"] = True
    claims.update(overrides)
    if claims.get("exp") is None:
        claims.pop("exp")
    return pyjwt.encode(claims, priv, algorithm="RS256",
                        headers={"kid": "fake-1"})


def stub_oidc(monkeypatch, provider_name, token):
    monkeypatch.setattr(federated, "_post_form",
                        lambda url, data: {"id_token": token,
                                           "access_token": "fake-access"})
    monkeypatch.setattr(federated, "_get_json",
                        lambda url, params=None, headers=None: {"name": "Stub Name"})


FB_STATE = {"debug": {"app_id": CLIENT_IDS["facebook"], "is_valid": True,
                      "expires_at": int(time.time()) + 3600},
            "me": {"id": "facebook-user-1", "name": "Facebook User",
                   "email": "facebook.user@example.com"}}


def stub_facebook(monkeypatch, debug=None, me=None):
    monkeypatch.setattr(federated, "_post_form",
                        lambda url, data: {"access_token": "fake-fb-token"})
    state = {"debug": dict(FB_STATE["debug"], **(debug or {})),
             "me": dict(FB_STATE["me"], **(me or {}))}

    def fake_get(url, params=None, headers=None):
        if "debug_token" in url:
            return {"data": state["debug"]}
        return state["me"]
    monkeypatch.setattr(federated, "_get_json", fake_get)


def fetch(provider_name):
    p = federated.get_provider(provider_name)
    return federated.fetch_identity(p, "fake-code")


def identity_for(provider_name, **kw):
    # The verified flag here models the identity AFTER lib.federated's
    # per-provider verification/mapping: Facebook = provider-attested
    # (/me has no email_verified field); Microsoft = verified via the
    # xms_edov / verified_primary_email mapping. Token-level UNVERIFIED
    # Microsoft identities are covered by dedicated tests below.
    base = {"google": ("google-user-1", "google.user@example.com", True),
            "facebook": ("facebook-user-1", "facebook.user@example.com", True),
            "microsoft": ("microsoft-user-1", "microsoft.user@example.com", True),
            "linkedin": ("linkedin-user-1", "linkedin.user@example.com", True),
            "apple": ("apple-user-1", "relay@privaterelay.appleid.com", False)}
    sub, email, verified = base[provider_name]
    return federated.Identity(provider=provider_name,
                              provider_user_id=kw.get("sub", sub),
                              email=kw.get("email", email),
                              email_verified=kw.get("verified", verified),
                              name=kw.get("name", "Social User"))


def resolve(identity):
    return asyncio.run(federated_auth.resolve_or_create_user(identity))


# ============================================================ per provider
@pytest.mark.parametrize("provider_name", ALL_PROVIDERS)
def test_new_signup(store, provider_name):
    user = resolve(identity_for(provider_name))
    assert user["role"] == "owner"
    assert "owner" in PERMISSIONS["ledger:admin"]  # operator grant
    assert user["tenant_id"]
    assert len(store.tenants.docs) == 1
    assert len(store.users.docs) == 1
    link = user["identities"][0]
    assert link["provider"] == provider_name
    assert link["provider_user_id"] == identity_for(provider_name).provider_user_id
    if provider_name == "google":
        assert user["google_sub"] == "google-user-1"


@pytest.mark.parametrize("provider_name", ALL_PROVIDERS)
def test_returning_login_no_duplicate(store, provider_name):
    first = resolve(identity_for(provider_name))
    second = resolve(identity_for(provider_name))
    assert second["id"] == first["id"]
    assert len(store.users.docs) == 1
    assert len(store.tenants.docs) == 1


@pytest.mark.parametrize("provider_name", ALL_PROVIDERS)
def test_link_by_verified_email_no_duplicate(store, provider_name):
    email_user = asyncio.run(auth.create_workspace(
        identity_for(provider_name).email, "a-strong-password-123",
        "Email User", "Email Shop"))
    linked = resolve(identity_for(provider_name, sub="brand-new-sub"))
    assert linked["id"] == email_user["id"]
    assert len(store.users.docs) == 1
    assert len(store.tenants.docs) == 1
    providers = [i["provider"] for i in store.users.docs[0]["identities"]]
    assert provider_name in providers


@pytest.mark.parametrize("provider_name", ALL_PROVIDERS)
def test_grant_parity_with_email_signup(store, provider_name):
    email_user = asyncio.run(auth.create_workspace(
        "parity.email@example.com", "a-strong-password-123",
        "Email User", "Parity Shop"))
    social_user = resolve(identity_for(provider_name,
                                     email="parity.social@example.com"))
    for field in ("role", "commission_rate"):
        assert social_user[field] == email_user[field]
    assert social_user["role"] == "owner"
    assert "ledger:admin" in [a for a, roles in PERMISSIONS.items()
                              if social_user["role"] in roles]
    email_tenant, social_tenant = store.tenants.docs
    assert social_tenant["settings"] == email_tenant["settings"]
    assert social_tenant["name"] == "My Shop"  # RegisterBody default shop
    # Seeded chart of accounts ran for BOTH signups (create_workspace).
    assert auth.seed_coa.await_count == 2


# ------------------------------------------------------- token refusals --
@pytest.mark.parametrize("provider_name", OIDC_PROVIDERS)
def test_oidc_signup_and_login_end_to_end(store, keys, monkeypatch, provider_name):
    set_env(monkeypatch, provider_name)
    stub_oidc(monkeypatch, provider_name,
              make_token(provider_name, keys.priv))
    ident = fetch(provider_name)
    assert ident.email == f"{provider_name}.user@example.com"
    user = resolve(ident)
    assert user["email"] == ident.email and user["role"] == "owner"
    again = resolve(fetch(provider_name))
    assert again["id"] == user["id"] and len(store.users.docs) == 1


@pytest.mark.parametrize("overrides,fragment", [
    ({"email_verified": False}, "not verified"),
    ({"aud": "someone-elses-client"}, "audience"),
    ({"exp": int(time.time()) - 60}, "expired"),
    ({"iss": "https://evil.example.com"}, "issuer"),
])
def test_google_bad_tokens_refused(store, keys, monkeypatch, overrides, fragment):
    set_env(monkeypatch, "google")
    stub_oidc(monkeypatch, "google",
              make_token("google", keys.priv, **overrides))
    with pytest.raises(federated.FederatedError) as e:
        fetch("google")
    assert fragment in str(e.value)


def test_linkedin_unverified_refused(store, keys, monkeypatch):
    set_env(monkeypatch, "linkedin")
    stub_oidc(monkeypatch, "linkedin",
              make_token("linkedin", keys.priv, email_verified=False))
    with pytest.raises(federated.FederatedError):
        fetch("linkedin")


@pytest.mark.parametrize("provider_name", OIDC_PROVIDERS)
def test_bad_signature_refused(store, keys, monkeypatch, provider_name):
    set_env(monkeypatch, provider_name)
    stub_oidc(monkeypatch, provider_name,
              make_token(provider_name, OTHER_PEM))  # signed by wrong key
    with pytest.raises(federated.FederatedError):
        fetch(provider_name)


@pytest.mark.parametrize("provider_name", ["google", "linkedin", "microsoft"])
def test_unverified_email_never_links(store, provider_name):
    asyncio.run(auth.create_workspace(
        "victim@example.com", "a-strong-password-123", "Victim", "Shop"))
    with pytest.raises(HTTPException) as e:
        resolve(identity_for(provider_name, email="victim@example.com",
                             verified=False, sub="attacker-sub"))
    assert e.value.status_code == 401
    assert "identities" not in store.users.docs[0]


# ------------------------------------------- Microsoft deviation (mapped) --
def test_microsoft_no_verification_claims_unverified_and_no_signup(
        store, keys, monkeypatch):
    """Entra token with NO email_verified / xms_edov /
    verified_primary_email: identity resolves UNVERIFIED — no signup,
    no email-link; the account store stays untouched."""
    set_env(monkeypatch, "microsoft")
    stub_oidc(monkeypatch, "microsoft",
              make_token("microsoft", keys.priv, xms_edov=None))
    ident = fetch("microsoft")
    assert ident.email == "microsoft.user@example.com"
    assert ident.email_verified is False
    with pytest.raises(HTTPException) as e:
        resolve(ident)
    assert e.value.status_code == 401
    assert len(store.users.docs) == 0
    # And it must not link onto an existing account by that email.
    asyncio.run(auth.create_workspace(
        "microsoft.user@example.com", "a-strong-password-123",
        "Email User", "Shop"))
    with pytest.raises(HTTPException):
        resolve(ident)
    assert "identities" not in store.users.docs[0]


def test_microsoft_unverified_still_signs_in_by_subject(store):
    """Subject-link only: once a Microsoft identity is linked, a later
    UNVERIFIED identity with the same sub signs that account in."""
    user = resolve(identity_for("microsoft", email="linked@example.com"))
    again = resolve(identity_for("microsoft", email="changed@example.com",
                                 verified=False))
    assert again["id"] == user["id"]
    assert len(store.users.docs) == 1


def test_microsoft_verified_primary_email_maps_verified(
        store, keys, monkeypatch):
    set_env(monkeypatch, "microsoft")
    stub_oidc(monkeypatch, "microsoft", make_token(
        "microsoft", keys.priv, xms_edov=None,
        verified_primary_email="microsoft.user@example.com"))
    ident = fetch("microsoft")
    assert ident.email_verified is True
    user = resolve(ident)
    assert user["role"] == "owner" and len(store.users.docs) == 1


def test_microsoft_verified_primary_email_mismatch_stays_unverified(
        store, keys, monkeypatch):
    set_env(monkeypatch, "microsoft")
    stub_oidc(monkeypatch, "microsoft", make_token(
        "microsoft", keys.priv, xms_edov=False,
        verified_primary_email="someone-else@example.com"))
    assert fetch("microsoft").email_verified is False


def test_facebook_signup_and_login(store, monkeypatch):
    set_env(monkeypatch, "facebook")
    stub_facebook(monkeypatch)
    ident = fetch("facebook")
    user = resolve(ident)
    assert user["role"] == "owner" and len(store.users.docs) == 1
    assert resolve(fetch("facebook"))["id"] == user["id"]


def test_facebook_invalid_token_refused(store, monkeypatch):
    set_env(monkeypatch, "facebook")
    stub_facebook(monkeypatch, debug={"is_valid": False})
    with pytest.raises(federated.FederatedError):
        fetch("facebook")


def test_facebook_wrong_app_refused(store, monkeypatch):
    set_env(monkeypatch, "facebook")
    stub_facebook(monkeypatch, debug={"app_id": "another-app"})
    with pytest.raises(federated.FederatedError):
        fetch("facebook")


def test_facebook_no_email_refused(store, monkeypatch):
    set_env(monkeypatch, "facebook")
    stub_facebook(monkeypatch, me={"email": ""})
    with pytest.raises(federated.FederatedError):
        fetch("facebook")


# ------------------------------------------------------ provider gating --
@pytest.mark.parametrize("provider_name", ALL_PROVIDERS + ["apple"])
def test_provider_off_invisible(store, monkeypatch, provider_name):
    clear_env(monkeypatch)
    p = federated.get_provider(provider_name)
    assert federated.is_enabled(p) is False
    cfg = asyncio.run(federated_auth.auth_config())
    assert cfg["providers"][provider_name]["enabled"] is False
    with pytest.raises(HTTPException) as e:
        asyncio.run(federated_auth.start(provider_name))
    assert e.value.status_code == 404


def test_apple_ready_only_even_with_keys(store, monkeypatch):
    set_env(monkeypatch, "apple")
    p = federated.get_provider("apple")
    assert p.ready_only is True
    assert federated.is_enabled(p) is False  # $99/yr developer gate
    cfg = asyncio.run(federated_auth.auth_config())
    assert cfg["providers"]["apple"]["enabled"] is False
    with pytest.raises(HTTPException) as e:
        asyncio.run(federated_auth.start("apple"))
    assert e.value.status_code == 404


def test_enabled_provider_start_redirects(store, monkeypatch):
    clear_env(monkeypatch)
    set_env(monkeypatch, "google")
    cfg = asyncio.run(federated_auth.auth_config())
    assert cfg["google_enabled"] is True
    assert cfg["providers"]["facebook"]["enabled"] is False
    resp = asyncio.run(federated_auth.start("google"))
    assert resp.status_code == 302
    assert "accounts.google.com" in resp.headers["location"]
    assert "state=" in resp.headers["location"]


def test_callback_bad_state_redirects_to_error(store, monkeypatch):
    set_env(monkeypatch, "google")
    req = SimpleNamespace(cookies={}, headers={}, client=None)
    resp = asyncio.run(federated_auth.callback_get(
        "google", req, code="x", state="y"))
    assert resp.status_code == 302
    assert "social_error=state" in resp.headers["location"]


# --------------------------------------------------- linking semantics --
def test_multiple_providers_link_to_one_account(store):
    user = resolve(identity_for("google", email="multi@example.com"))
    same = resolve(identity_for("facebook", email="multi@example.com"))
    same = resolve(identity_for("linkedin", email="multi@example.com"))
    assert same["id"] == user["id"]
    assert len(store.users.docs) == 1
    providers = {i["provider"] for i in store.users.docs[0]["identities"]}
    assert providers == {"google", "facebook", "linkedin"}


def test_apple_links_by_provider_id_never_email(store):
    email_user = asyncio.run(auth.create_workspace(
        "relay@privaterelay.appleid.com", "a-strong-password-123",
        "Email User", "Shop"))
    # Unknown Apple sub + an email that happens to match: NO link, NO
    # duplicate — the signup is refused by the email-uniqueness rule.
    with pytest.raises(HTTPException):
        resolve(identity_for("apple", sub="apple-unknown-sub"))
    assert "identities" not in store.users.docs[0]
    # Once linked by sub, the same sub logs that account in.
    store.users.docs[0]["identities"] = [{
        "provider": "apple", "provider_user_id": "apple-user-1",
        "email": "relay@privaterelay.appleid.com", "linked_at": "t"}]
    got = resolve(identity_for("apple", email="other@example.com"))
    assert got["id"] == email_user["id"]


# ------------------------------------------------------- email regression --
def test_email_password_login_regression(store):
    asyncio.run(auth.create_workspace(
        "owner@example.com", "correct-horse- battery", "Owner", "Shop"))
    from starlette.responses import Response
    body = auth.LoginBody(email="owner@example.com",
                          password="correct-horse- battery")
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                          headers={}, cookies={})
    out = asyncio.run(auth.login(body, Response(), req))
    assert out["email"] == "owner@example.com" and out["role"] == "owner"
    bad = auth.LoginBody(email="owner@example.com", password="wrong-password")
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth.login(bad, Response(), req))
    assert e.value.status_code == 401


def test_social_account_cannot_password_login(store):
    user = resolve(identity_for("google", email="socialonly@example.com"))
    assert user["password_hash"]  # shape preserved, value unusable
    from lib.auth import verify_password
    assert verify_password("anything-at-all", user["password_hash"]) is False
