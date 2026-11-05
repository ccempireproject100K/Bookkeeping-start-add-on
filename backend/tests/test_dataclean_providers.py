"""DataClean provider completion (2026-11-05 gap-audit builds):

  * Bouncer email adapter — the provider the owner actually signed
    up for: env-gated on BOUNCER_API_KEY behind the existing
    DATACLEAN_EMAIL_VERIFY_PROVIDER selector; single + batch paths;
    auth/team-verification-hold errors surface as `unavailable`,
    never as a verdict, and are never cached;
  * Google Address Validation adapter — env-gated on
    GOOGLE_ADDRESS_VALIDATION_API_KEY, selectable alongside Smarty
    via DATACLEAN_ADDRESS_VERIFY_PROVIDER, verdicts mapped to the
    lane's standard shape, provider postal code as a suggestion
    only (evidence, never an applied edit);
  * Markup mechanism — DATACLEAN_VERIFY_MARKUP_BPS (default 0 =
    pure pass-through, the mechanism never invents a size) applied
    on top of provider cost in the estimate and surfaced in the
    payload (cost / markup / price side by side).

No live calls anywhere — lib/dataclean_verify.http_json is faked
with recorded-response shapes throughout.
"""
import asyncio
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib import dataclean_api as dca
from lib import dataclean_jobs as jobs_lib
from lib import dataclean_lists as lists_lib
from lib import dataclean_verify as verify_lib
from lib import audit as audit_lib
from routers import dataclean_jobs as RJ

TENANT = "ws-providers"


def _match(doc, q):
    for k, v in q.items():
        dv = doc.get(k)
        if isinstance(v, dict):
            if "$in" in v and dv not in v["$in"]:
                return False
            if "$ne" in v and dv == v["$ne"]:
                return False
            if "$exists" in v and (k in doc) != bool(v["$exists"]):
                return False
        elif dv != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class FakeColl:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def insert_many(self, docs):
        self.docs.extend(dict(d) for d in docs)

    async def find_one(self, q, projection=None):
        for d in self.docs:
            if _match(d, q):
                return dict(d)
        return None

    def find(self, q, projection=None):
        return FakeCursor([dict(d) for d in self.docs if _match(d, q)])

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs:
            if _match(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                for k, v in (upd.get("$inc") or {}).items():
                    d[k] = (d.get(k) or 0) + v
                return

    async def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if _match(d, q):
                del self.docs[i]
                return

    async def delete_many(self, q):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _match(d, q)]

        class R:
            deleted_count = before - len(self.docs)
        return R()


class FakeDB:
    def __init__(self):
        self._colls = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeColl())


COLL_NAMES = (
    "dataclean_api_keys", "dataclean_usage", "dataclean_lists",
    "dataclean_list_records", "dataclean_list_suggestions",
    "dataclean_list_merges", "dataclean_jobs", "dataclean_job_artifacts",
    "dataclean_lookup_cache", "audit_chain", "audit_log",
)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    for name in COLL_NAMES:
        getattr(fake, name)
    for module in (dca, jobs_lib, lists_lib, verify_lib, RJ, audit_lib):
        monkeypatch.setattr(module, "db", fake, raising=False)
    key = {"id": "key-p", "tenant_id": TENANT, "key_hash": "hash-p",
           "key_stub": "mdck_providers", "label": "providers",
           "daily_call_limit": 100000, "per_minute_limit": 100000,
           "monthly_row_quota": 1000000, "revoked_at": None}
    fake.dataclean_api_keys.docs.append(key)
    actor = {"id": "key-p", "tenant_id": TENANT, "api_key_id": "key-p",
             "auth_kind": "api_key", "role": "dataclean_api_key",
             "_key_doc": key}
    return fake, actor


RECORDS = [
    {"name": "Good", "email": "good@x.example",
     "address": "1 Rose St, Austin, TX"},
    {"name": "Good Dup", "email": "GOOD@x.example",
     "address": "1 Rose St, Austin, TX"},
    {"name": "Bad", "email": "bad@x.example",
     "address": "9 Route Rd, Austin, TX"},
    {"name": "Risky", "email": "risky@x.example",
     "address": "999 Nowhere Blvd, Austin, TX"},
]


def _make_list(actor, records):
    lst = run(lists_lib.create_list(actor, "providers",
                                    kind="customers", source="api"))
    run(lists_lib.append_records(actor, lst, records))
    return lst["id"]


def _configure_bouncer(monkeypatch):
    monkeypatch.setenv("DATACLEAN_EMAIL_VERIFY_PROVIDER", "bouncer")
    monkeypatch.setenv("BOUNCER_API_KEY", "bouncer-key")


# ------------------------------------------------------------------
# Fakes (recorded provider shapes)
# ------------------------------------------------------------------

class BouncerFake:
    EMAILS = {"good@x.example": "deliverable",
              "bad@x.example": "undeliverable",
              "risky@x.example": "risky"}

    def __init__(self, auth_error=None, batch_fail=False):
        self.calls = []
        self.auth_error = auth_error
        self.batch_fail = batch_fail

    def __call__(self, method, url, params=None, headers=None,
                 auth=None, json_body=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "json_body": json_body})
        if self.auth_error is not None:
            return self.auth_error, {"error": "unauthorized"}
        if "/verify/batch" in url and method == "POST":
            if self.batch_fail:
                return 500, {}
            return 202, {"id": "batch-1", "status": "in-progress"}
        if "/verify/batch/" in url:
            results = [{"email": e, "status": s}
                       for e, s in self.EMAILS.items()]
            return 200, {"id": "batch-1", "status": "completed",
                         "results": results}
        if "/verify/" in url:  # single
            from urllib.parse import unquote
            email = unquote(url.rsplit("/", 1)[-1])
            raw = self.EMAILS.get(email, "unknown")
            return 200, {"email": email, "status": raw,
                         "reason": f"recorded-{raw}"}
        return 404, {}

    def batch_posts(self):
        return [c for c in self.calls
                if c["method"] == "POST" and "/verify/batch" in c["url"]]

    def single_calls(self):
        return [c for c in self.calls
                if "/verify/" in c["url"] and "/verify/batch" not in c["url"]]


class GoogleFake:
    def __init__(self, http_error=None):
        self.calls = []
        self.http_error = http_error

    def __call__(self, method, url, params=None, headers=None,
                 auth=None, json_body=None):
        self.calls.append({"method": method, "url": url,
                           "params": params, "json_body": json_body})
        if self.http_error is not None:
            return self.http_error, {"error": {"status": "DENIED"}}
        line = ((json_body or {}).get("address") or {}) \
            .get("addressLines", [""])[0]
        if "Rose" in line:
            return 200, {"result": {
                "verdict": {"validationGranularity": "PREMISE",
                            "addressComplete": True,
                            "hasUnconfirmedComponents": False},
                "address": {
                    "formattedAddress": "1 Rose St, Austin, TX 78701",
                    "postalAddress": {"postalCode": "78701-1234"}}}}
        if "Route" in line:
            return 200, {"result": {
                "verdict": {"validationGranularity": "ROUTE",
                            "addressComplete": False,
                            "hasUnconfirmedComponents": True},
                "address": {
                    "formattedAddress": "Route Rd, Austin, TX",
                    "postalAddress": {"postalCode": "78704"}}}}
        return 200, {"result": {"verdict": {
            "validationGranularity": "OTHER",
            "addressComplete": False}, "address": {}}}


# ------------------------------------------------------------------
# Bouncer: gating + single-lookup mapping
# ------------------------------------------------------------------

def test_bouncer_honest_off_without_key(env, monkeypatch):
    fake, actor = env
    monkeypatch.setenv("DATACLEAN_EMAIL_VERIFY_PROVIDER", "bouncer")
    monkeypatch.delenv("BOUNCER_API_KEY", raising=False)
    assert verify_lib.email_provider() is None
    assert verify_lib.any_configured(["email"]) is False
    _configure_bouncer(monkeypatch)
    assert verify_lib.email_provider() == "bouncer"


def test_bouncer_single_mapping(env, monkeypatch):
    fake, actor = env
    _configure_bouncer(monkeypatch)
    monkeypatch.setattr(verify_lib, "http_json", BouncerFake())
    res, origin = run(verify_lib.lookup_value(actor, "email",
                                              "good@x.example"))
    assert origin == "provider"
    assert res["status"] == "deliverable"
    assert res["source"] == "bouncer"
    assert res["provider_status"] == "deliverable"
    assert res["reason"] == "recorded-deliverable"
    res, _ = run(verify_lib.lookup_value(actor, "email",
                                         "bad@x.example"))
    assert res["status"] == "undeliverable"
    res, _ = run(verify_lib.lookup_value(actor, "email",
                                         "risky@x.example"))
    assert res["status"] == "risky"
    res, _ = run(verify_lib.lookup_value(actor, "email",
                                         "nobody@x.example"))
    assert res["status"] == "unknown"


def test_bouncer_hold_error_is_unavailable_never_clean(env, monkeypatch):
    fake, actor = env
    _configure_bouncer(monkeypatch)
    # 403 = the shape of an auth failure / team-verification hold.
    monkeypatch.setattr(verify_lib, "http_json",
                        BouncerFake(auth_error=403))
    res, origin = run(verify_lib.lookup_value(actor, "email",
                                              "good@x.example"))
    assert origin == "unavailable"
    assert res["status"] == "unavailable"
    # Nothing about the held account's refusal may be cached as a
    # verdict — the next run must be free to succeed.
    assert fake.dataclean_lookup_cache.docs == []


# ------------------------------------------------------------------
# Bouncer: batch path through a confirmed verify job
# ------------------------------------------------------------------

def test_bouncer_batch_seeds_cache_in_job_flow(env, monkeypatch):
    fake, actor = env
    _configure_bouncer(monkeypatch)
    prov = BouncerFake()
    monkeypatch.setattr(verify_lib, "http_json", prov)
    lid = _make_list(actor, RECORDS)
    out = run(RJ.verify(RJ.VerifyIn(list_id=lid, kinds=["email"],
                                    confirm_estimate=True),
                        actor=actor))
    assert out["job"]["status"] == "complete"
    # Exactly one batch submission for 3 distinct values; the batch
    # carried every distinct email; no single lookups were needed.
    posts = prov.batch_posts()
    assert len(posts) == 1
    assert sorted(posts[0]["json_body"]["emails"]) == [
        "bad@x.example", "good@x.example", "risky@x.example"]
    assert posts[0]["headers"]["x-api-key"] == "bouncer-key"
    assert prov.single_calls() == []
    assert out["counts"]["lookups_made"] == 0
    assert out["counts"]["cache_hits"] == 4  # 3 batch-seeded + 1 dup
    assert len(fake.dataclean_lookup_cache.docs) == 3
    recs = {d["name"]: d for d in fake.dataclean_list_records.docs}
    assert recs["Good"]["verifications"]["email"]["status"] == \
        "deliverable"
    assert recs["Good"]["verifications"]["email"]["source"] == "bouncer"
    assert recs["Bad"]["verifications"]["email"]["status"] == \
        "undeliverable"
    assert recs["Risky"]["verifications"]["email"]["status"] == "risky"
    # Evidence only: the record field itself is untouched.
    assert recs["Bad"]["email"] == "bad@x.example"


def test_bouncer_batch_failure_falls_back_to_singles(env, monkeypatch):
    fake, actor = env
    _configure_bouncer(monkeypatch)
    prov = BouncerFake(batch_fail=True)
    monkeypatch.setattr(verify_lib, "http_json", prov)
    lid = _make_list(actor, RECORDS)
    out = run(RJ.verify(RJ.VerifyIn(list_id=lid, kinds=["email"],
                                    confirm_estimate=True),
                        actor=actor))
    assert out["job"]["status"] == "complete"
    assert len(prov.batch_posts()) == 1       # batch was attempted
    assert len(prov.single_calls()) == 3       # then singles took over
    assert out["counts"]["lookups_made"] == 3
    recs = {d["name"]: d for d in fake.dataclean_list_records.docs}
    assert recs["Good"]["verifications"]["email"]["status"] == \
        "deliverable"


# ------------------------------------------------------------------
# Google Address Validation: selection + mapping
# ------------------------------------------------------------------

def test_google_address_selection(env, monkeypatch):
    fake, actor = env
    monkeypatch.delenv("DATACLEAN_ADDRESS_VERIFY_PROVIDER",
                       raising=False)
    monkeypatch.delenv("DATACLEAN_SMARTY_AUTH_ID", raising=False)
    monkeypatch.delenv("DATACLEAN_SMARTY_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GOOGLE_ADDRESS_VALIDATION_API_KEY", "g-key")
    # Key alone (the owner's actual provisioning) selects Google.
    assert verify_lib.provider_for("address") == \
        "google_address_validation"
    # Explicit selector picks Google too.
    monkeypatch.setenv("DATACLEAN_ADDRESS_VERIFY_PROVIDER", "google")
    assert verify_lib.provider_for("address") == \
        "google_address_validation"
    # Selector without the key is honest-off, not a silent fallback.
    monkeypatch.delenv("GOOGLE_ADDRESS_VALIDATION_API_KEY")
    monkeypatch.delenv("DATACLEAN_GOOGLE_ADDRESS_VALIDATION_API_KEY",
                       raising=False)
    assert verify_lib.provider_for("address") is None
    # With both provisioned and no selector, Smarty keeps priority
    # (pre-existing behavior is unchanged); the selector overrides.
    monkeypatch.setenv("GOOGLE_ADDRESS_VALIDATION_API_KEY", "g-key")
    monkeypatch.setenv("DATACLEAN_SMARTY_AUTH_ID", "smarty-id")
    monkeypatch.setenv("DATACLEAN_SMARTY_AUTH_TOKEN", "smarty-token")
    monkeypatch.delenv("DATACLEAN_ADDRESS_VERIFY_PROVIDER")
    assert verify_lib.provider_for("address") == "smarty"
    monkeypatch.setenv("DATACLEAN_ADDRESS_VERIFY_PROVIDER", "google")
    assert verify_lib.provider_for("address") == \
        "google_address_validation"


def test_google_address_mapping(env, monkeypatch):
    fake, actor = env
    monkeypatch.setenv("GOOGLE_ADDRESS_VALIDATION_API_KEY", "g-key")
    monkeypatch.setattr(verify_lib, "http_json", GoogleFake())

    res, origin = run(verify_lib.lookup_value(
        actor, "address", "1 Rose St, Austin, TX"))
    assert origin == "provider"
    assert res["status"] == "deliverable"
    assert res["source"] == "google_address_validation"
    assert res["validation_granularity"] == "PREMISE"
    # Postal code arrives as a labeled suggestion only.
    assert res["suggested_postal_code"] == "78701-1234"

    res, _ = run(verify_lib.lookup_value(
        actor, "address", "9 Route Rd, Austin, TX"))
    assert res["status"] == "partial"

    res, _ = run(verify_lib.lookup_value(
        actor, "address", "999 Nowhere Blvd, Austin, TX"))
    assert res["status"] == "not_found"


def test_google_address_error_is_unavailable(env, monkeypatch):
    fake, actor = env
    monkeypatch.setenv("GOOGLE_ADDRESS_VALIDATION_API_KEY", "g-key")
    monkeypatch.setattr(verify_lib, "http_json",
                        GoogleFake(http_error=403))
    res, origin = run(verify_lib.lookup_value(
        actor, "address", "1 Rose St, Austin, TX"))
    assert origin == "unavailable"
    assert res["status"] == "unavailable"
    assert fake.dataclean_lookup_cache.docs == []


# ------------------------------------------------------------------
# Markup mechanism: default 0 = pass-through; a set value flows
# ------------------------------------------------------------------

def _estimate(env, monkeypatch, markup=None):
    fake, actor = env
    monkeypatch.setenv("DATACLEAN_EMAIL_VERIFY_PROVIDER",
                       "neverbounce")
    monkeypatch.setenv("DATACLEAN_NEVERBOUNCE_API_KEY", "nb-key")
    if markup is None:
        monkeypatch.delenv("DATACLEAN_VERIFY_MARKUP_BPS",
                           raising=False)
    else:
        monkeypatch.setenv("DATACLEAN_VERIFY_MARKUP_BPS", markup)
    lid = _make_list(actor, RECORDS[:3])  # good, good-dup, bad
    out = run(RJ.verify(RJ.VerifyIn(list_id=lid, kinds=["email"]),
                        actor=actor))
    return out["estimate"]


def test_markup_default_zero_is_pure_pass_through(env, monkeypatch):
    est = _estimate(env, monkeypatch)
    assert est["markup_bps"] == 0
    assert est["total_markup_usd"] == 0
    assert est["total_price_usd"] == est["total_cost_usd"]
    kind = est["kinds"]["email"]
    assert kind["lookups"] == 2
    assert kind["markup_bps"] == 0
    assert kind["markup_usd"] == 0
    assert kind["price_usd"] == kind["cost_usd"] == pytest.approx(0.016)
    assert kind["price_rate_usd"] == pytest.approx(kind["rate_usd"])


def test_markup_set_value_flows_through_exactly(env, monkeypatch):
    est = _estimate(env, monkeypatch, markup="250")  # 2.5%
    kind = est["kinds"]["email"]
    assert est["markup_bps"] == 250
    assert kind["price_rate_usd"] == pytest.approx(0.0082)
    assert kind["cost_usd"] == pytest.approx(0.016)      # provider cost
    assert kind["markup_usd"] == pytest.approx(0.0004)   # 2.5% of cost
    assert kind["price_usd"] == pytest.approx(0.0164)    # what is quoted
    assert est["total_cost_usd"] == pytest.approx(0.016)
    assert est["total_markup_usd"] == pytest.approx(0.0004)
    assert est["total_price_usd"] == pytest.approx(0.0164)


def test_markup_malformed_and_negative_are_zero(env, monkeypatch):
    fake, actor = env
    monkeypatch.setenv("DATACLEAN_VERIFY_MARKUP_BPS", "abc")
    assert verify_lib.markup_bps() == 0
    monkeypatch.setenv("DATACLEAN_VERIFY_MARKUP_BPS", "-125")
    assert verify_lib.markup_bps() == 0
    monkeypatch.setenv("DATACLEAN_VERIFY_MARKUP_BPS", "125")
    assert verify_lib.markup_bps() == 125
    assert verify_lib.marked_up_rate("email") == pytest.approx(0.0081)
