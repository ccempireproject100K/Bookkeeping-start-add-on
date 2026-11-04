"""Phase D (P9-P11 paid-data verification): CODE-ONLY lanes.

No live calls anywhere — the provider seam
(lib/dataclean_verify.http_json) is faked with recorded-response
shapes. Pins the design's acceptance set:

  * honest-off: nothing configured -> 503 everywhere, no job created;
  * pre-flight estimate math: distinct values x published rate,
    cached values excluded, and an estimate makes ZERO provider
    calls;
  * the job flow: create -> process pauses at awaiting_confirmation
    with the estimate on the job -> confirm runs the lookups;
  * evidence only: results land on records as `verifications` with
    source + timestamp; the record's own fields are never edited,
    even when the verdict is `undeliverable`;
  * cache: a second run over the same values makes ZERO new provider
    calls;
  * outage: provider raises -> lookups marked unavailable, the job
    still completes, nothing is cached or faked;
  * provider mappings: NeverBounce + ZeroBounce statuses, Twilio
    line type, Smarty DPV + ZIP+4 as a suggestion only.
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

TENANT = "ws-phase-d"


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
    key = {"id": "key-d", "tenant_id": TENANT, "key_hash": "hash-d",
           "key_stub": "mdck_phase_d", "label": "phase-d",
           "daily_call_limit": 100000, "per_minute_limit": 100000,
           "monthly_row_quota": 1000000, "revoked_at": None}
    fake.dataclean_api_keys.docs.append(key)
    actor = {"id": "key-d", "tenant_id": TENANT, "api_key_id": "key-d",
             "auth_kind": "api_key", "role": "dataclean_api_key",
             "_key_doc": key}
    return fake, actor


class ProviderFake:
    """Recorded provider shapes. `outage=True` raises on every call."""

    EMAILS = {"good@x.example": "valid", "bad@x.example": "invalid",
              "risky@x.example": "catchall"}

    def __init__(self, outage=False):
        self.calls = []
        self.outage = outage

    def __call__(self, method, url, params=None, headers=None, auth=None):
        self.calls.append({"method": method, "url": url,
                           "params": params, "auth": auth})
        if self.outage:
            raise ConnectionError("provider down")
        if "neverbounce" in url:
            result = self.EMAILS.get((params or {}).get("email"),
                                     "unknown")
            return 200, {"result": result}
        if "zerobounce" in url:
            raw = self.EMAILS.get((params or {}).get("email"),
                                  "unknown")
            return 200, {"status": {"catchall": "catch-all"}.get(
                raw, raw)}
        if "lookups.twilio" in url:
            if "5125550100" in url:
                return 200, {"valid": True,
                             "line_type_intelligence": {
                                 "type": "mobile",
                                 "carrier_name": "T-Mobile"}}
            return 200, {"valid": False,
                         "line_type_intelligence": {
                             "type": "landline", "carrier_name": None}}
        if "smarty" in url:
            street = (params or {}).get("street") or ""
            if "Rose" in street:
                return 200, {"results": [{
                    "analysis": {"dpv_match_code": "Y",
                                 "dpv_footnotes": "AABB"},
                    "components": {"zipcode": "78701",
                                   "plus4_code": "1234"}}]}
            return 200, {"results": []}
        return 404, {}


def _configure_all(monkeypatch):
    monkeypatch.setenv("DATACLEAN_EMAIL_VERIFY_PROVIDER", "neverbounce")
    monkeypatch.setenv("DATACLEAN_NEVERBOUNCE_API_KEY", "nb-key")
    monkeypatch.setenv("DATACLEAN_TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("DATACLEAN_TWILIO_AUTH_TOKEN", "tw-token")
    monkeypatch.setenv("DATACLEAN_SMARTY_AUTH_ID", "smarty-id")
    monkeypatch.setenv("DATACLEAN_SMARTY_AUTH_TOKEN", "smarty-token")


def _make_list(run_, actor, records):
    lst = run_(lists_lib.create_list(actor, "verify me",
                                     kind="customers", source="api"))
    run_(lists_lib.append_records(actor, lst, records))
    return lst["id"]


RECORDS = [
    {"name": "Good", "email": "good@x.example",
     "phone": "(512) 555-0100", "address": "1 Rose St, Austin, TX"},
    {"name": "Good Dup", "email": "GOOD@x.example",
     "phone": "5125550100", "address": "1 Rose St, Austin, TX"},
    {"name": "Bad", "email": "bad@x.example"},
]


# ------------------------------------------------------------------
# Honest-off
# ------------------------------------------------------------------

def test_unconfigured_is_honest_off(env, monkeypatch):
    fake, actor = env
    lid = _make_list(run, actor, RECORDS)
    assert verify_lib.any_configured() is False
    with pytest.raises(HTTPException) as exc:
        run(RJ.verify(RJ.VerifyIn(list_id=lid), actor=actor))
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc2:
        run(RJ.create_job(RJ.CreateJobIn(kind="verify", list_id=lid),
                          actor=actor))
    assert exc2.value.status_code == 503
    assert fake.dataclean_jobs.docs == []  # refused job left nothing


# ------------------------------------------------------------------
# Estimate math (zero provider calls)
# ------------------------------------------------------------------

def test_estimate_math_distinct_x_rate(env, monkeypatch):
    fake, actor = env
    _configure_all(monkeypatch)
    prov = ProviderFake()
    monkeypatch.setattr(verify_lib, "http_json", prov)
    lid = _make_list(run, actor, RECORDS)
    out = run(RJ.verify(RJ.VerifyIn(list_id=lid), actor=actor))
    est = out["estimate"]
    # distinct: email {good, bad}=2, phone {5125550100}=1,
    # address {1 rose st...}=1
    assert est["kinds"]["email"]["distinct_values"] == 2
    assert est["kinds"]["email"]["lookups"] == 2
    assert est["kinds"]["email"]["cost_usd"] == pytest.approx(0.016)
    assert est["kinds"]["phone"]["lookups"] == 1
    assert est["kinds"]["phone"]["cost_usd"] == pytest.approx(0.008)
    assert est["kinds"]["address"]["lookups"] == 1
    assert est["kinds"]["address"]["cost_usd"] == pytest.approx(0.017)
    assert est["total_lookups"] == 4
    assert est["total_cost_usd"] == pytest.approx(0.041)
    assert out["confirm_required"] is True
    assert prov.calls == []  # an estimate never calls a provider


# ------------------------------------------------------------------
# Job flow: pause -> confirm -> evidence + cache
# ------------------------------------------------------------------

def test_verify_job_pause_confirm_evidence_and_cache(env, monkeypatch):
    fake, actor = env
    _configure_all(monkeypatch)
    prov = ProviderFake()
    monkeypatch.setattr(verify_lib, "http_json", prov)
    lid = _make_list(run, actor, RECORDS)

    out = run(RJ.verify(RJ.VerifyIn(list_id=lid, kinds=["email"],
                                    confirm_estimate=True),
                        actor=actor))
    job = out["job"]
    assert job["status"] == "complete"
    assert job["estimate"]["total_lookups"] == 2
    counts = out["counts"]
    assert counts["lookups_made"] == 2      # good + bad, once each
    assert counts["cache_hits"] == 1        # the GOOD@ dup hit cache
    assert counts["records_verified"] == 3

    recs = {d["name"]: d for d in fake.dataclean_list_records.docs}
    good = recs["Good"]["verifications"]["email"]
    assert good["status"] == "deliverable"
    assert good["source"] == "neverbounce"
    assert good["at"]
    bad = recs["Bad"]["verifications"]["email"]
    assert bad["status"] == "undeliverable"
    # Evidence only: the record's own fields are untouched.
    assert recs["Bad"]["email"] == "bad@x.example"
    assert "verifications" not in recs["Bad"].get("email", "")
    assert len(fake.dataclean_lookup_cache.docs) == 2
    arts = [d for d in fake.dataclean_job_artifacts.docs
            if d["kind"] == "verification"]
    assert len(arts) == 3

    # Second run over the same values: zero new provider calls.
    calls_before = len(prov.calls)
    out2 = run(RJ.verify(RJ.VerifyIn(list_id=lid, kinds=["email"],
                                     confirm_estimate=True),
                         actor=actor))
    assert out2["counts"]["lookups_made"] == 0
    assert out2["counts"]["cache_hits"] == 3
    assert len(prov.calls) == calls_before
    assert out2["job"]["estimate"]["total_lookups"] == 0


def test_pause_state_and_wrong_job_confirm(env, monkeypatch):
    fake, actor = env
    _configure_all(monkeypatch)
    monkeypatch.setattr(verify_lib, "http_json", ProviderFake())
    lid = _make_list(run, actor, RECORDS)
    job, err = run(jobs_lib.create_job(actor, "verify", lid,
                                       options={"kinds": ["email"]}))
    assert err is None
    job = run(jobs_lib.process_job(actor, job["id"]))
    assert job["status"] == "awaiting_confirmation"
    assert job["estimate"]["total_cost_usd"] == pytest.approx(0.016)
    assert job["result_counts"]["lookups_needed"] == 2
    # A dedupe job cannot be confirmed.
    other, _ = run(jobs_lib.create_job(actor, "dedupe_scan", lid))
    with pytest.raises(HTTPException) as exc:
        run(RJ.confirm_job(other["id"], actor=actor))
    assert exc.value.status_code == 409


# ------------------------------------------------------------------
# Outage: unavailable, still completes, never cached
# ------------------------------------------------------------------

def test_provider_outage_marks_unavailable(env, monkeypatch):
    fake, actor = env
    _configure_all(monkeypatch)
    monkeypatch.setattr(verify_lib, "http_json",
                        ProviderFake(outage=True))
    lid = _make_list(run, actor, RECORDS)
    out = run(RJ.verify(RJ.VerifyIn(list_id=lid, kinds=["email"],
                                    confirm_estimate=True),
                        actor=actor))
    assert out["job"]["status"] == "complete"
    assert out["counts"]["unavailable"] == 3
    recs = {d["name"]: d for d in fake.dataclean_list_records.docs}
    assert recs["Good"]["verifications"]["email"]["status"] == \
        "unavailable"
    assert fake.dataclean_lookup_cache.docs == []


# ------------------------------------------------------------------
# Provider mappings (ZeroBounce / Twilio / Smarty)
# ------------------------------------------------------------------

def test_zerobounce_twilio_smarty_mappings(env, monkeypatch):
    fake, actor = env
    monkeypatch.setenv("DATACLEAN_EMAIL_VERIFY_PROVIDER", "zerobounce")
    monkeypatch.setenv("DATACLEAN_ZEROBOUNCE_API_KEY", "zb-key")
    monkeypatch.setenv("DATACLEAN_TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("DATACLEAN_TWILIO_AUTH_TOKEN", "tw-token")
    monkeypatch.setenv("DATACLEAN_SMARTY_AUTH_ID", "smarty-id")
    monkeypatch.setenv("DATACLEAN_SMARTY_AUTH_TOKEN", "smarty-token")
    monkeypatch.setattr(verify_lib, "http_json", ProviderFake())

    res, origin = run(verify_lib.lookup_value(actor, "email",
                                              "risky@x.example"))
    assert origin == "provider"
    assert res["status"] == "risky" and res["source"] == "zerobounce"
    assert res["provider_status"] == "catch-all"

    res, _ = run(verify_lib.lookup_value(actor, "phone",
                                         "(512) 555-0100"))
    assert res["status"] == "valid" and res["line_type"] == "mobile"
    assert res["source"] == "twilio_lookup"

    res, _ = run(verify_lib.lookup_value(
        actor, "address", "1 Rose St, Austin, TX"))
    assert res["status"] == "deliverable"
    assert res["dpv_match_code"] == "Y"
    # ZIP+4 arrives as a labeled suggestion, never an applied edit.
    assert res["suggested_postal_code"] == "78701-1234"

    res, _ = run(verify_lib.lookup_value(
        actor, "address", "999 Nowhere Blvd, Austin, TX"))
    assert res["status"] == "not_found"
