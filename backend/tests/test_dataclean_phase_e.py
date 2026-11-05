"""Phase E (P4 connectors): HubSpot round-trip + Salesforce decline.

The §7 acceptance fixture, against a fully mocked provider seam
(lib/dataclean_connectors.http_json — no live calls anywhere):

  HubSpot: OAuth connect -> pull 200 contacts (two pages) -> scan ->
  approve exactly 3 merges -> write-back PATCHes only the approved
  blank fills and calls HubSpot's native merge exactly 3 times; a
  fourth, UNAPPROVED pair is never touched. Tokens never appear in
  any response view.

  Salesforce: OAuth connects, but the org's edition has no API
  access — the probe declines with the edition reason printed, the
  pull refuses the same way, and write-back is the merge-decision
  export only (zero provider calls).
"""
import asyncio
import os
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib import dataclean_api as dca
from lib import dataclean_connectors as conn
from lib import dataclean_jobs as jobs_lib
from lib import dataclean_lists as lists_lib
from lib import audit as audit_lib
from routers import dataclean_connectors as RC

TENANT = "ws-phase-e"


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
    "dataclean_list_merges", "dataclean_connector_tokens",
    "dataclean_jobs", "dataclean_job_artifacts",
    "audit_chain", "audit_log",
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    for name in COLL_NAMES:
        getattr(fake, name)
    for module in (dca, conn, lists_lib, jobs_lib, RC, audit_lib):
        monkeypatch.setattr(module, "db", fake, raising=False)
    key = {"id": "key-e", "tenant_id": TENANT, "key_hash": "hash-e",
           "key_stub": "mdck_phase_e", "label": "phase-e",
           "daily_call_limit": 100000, "per_minute_limit": 100000,
           "monthly_row_quota": 1000000, "revoked_at": None}
    fake.dataclean_api_keys.docs.append(key)
    actor = {"id": "key-e", "tenant_id": TENANT, "api_key_id": "key-e",
             "auth_kind": "api_key", "role": "dataclean_api_key",
             "_key_doc": key}
    for var, val in (
            ("DATACLEAN_HUBSPOT_CLIENT_ID", "hs-client"),
            ("DATACLEAN_HUBSPOT_CLIENT_SECRET", "hs-secret"),
            ("DATACLEAN_HUBSPOT_REDIRECT_URI",
             "https://books.example.com/cb/hs"),
            ("DATACLEAN_SALESFORCE_CLIENT_ID", "sf-client"),
            ("DATACLEAN_SALESFORCE_CLIENT_SECRET", "sf-secret"),
            ("DATACLEAN_SALESFORCE_REDIRECT_URI",
             "https://books.example.com/cb/sf")):
        monkeypatch.setenv(var, val)
    return fake, actor


def _state_from(url):
    return parse_qs(urlparse(url).query)["state"][0]


def _connect(run_, actor, provider, http):
    url = run_(RC.authorize(provider, actor=actor))["authorize_url"]
    view = run_(RC.callback(provider,
                            RC.CallbackIn(code="auth-code",
                                          state=_state_from(url)),
                            actor=actor))
    return view


# ------------------------------------------------------------------
# HubSpot fixture: 200 contacts, 3 approvable pairs + 1 left pending
# ------------------------------------------------------------------

def _hs_contacts():
    def c(i, **props):
        return {"id": f"hs-{i}", "properties": props}
    special = {
        1: c(1, firstname="Acme", lastname="Supply",
             email="ops@acme.example",
             address="9 Oak Ave", city="Dallas", state="TX",
             zip="75001"),
        2: c(2, firstname="ACME", lastname="SUPPLY",
             email="ops@acme.example", phone="5125550134",
             address="9 Oak Ave", city="Dallas", state="TX",
             zip="75001"),
        3: c(3, firstname="Bluebonnet", lastname="Bakery",
             email="hi@bb.example", phone="5125550177"),
        4: c(4, firstname="BLUEBONNET", lastname="BAKERY",
             email="hi@bb.example", phone="5125550177",
             address="1 Rose St", city="Austin", state="TX",
             zip="78701"),
        5: c(5, firstname="Lone Star", lastname="Plumbing",
             phone="5125550199",
             address="77 Ranch Rd", city="Austin", state="TX",
             zip="78701"),
        6: c(6, firstname="LONE STAR", lastname="PLUMBING",
             email="crew@lsp.example", phone="5125550199",
             address="77 Ranch Rd", city="Austin", state="TX",
             zip="78701"),
        7: c(7, firstname="Unapproved", lastname="Pair",
             email="pair@unapproved.example"),
        8: c(8, firstname="UNAPPROVED", lastname="PAIR",
             email="pair@unapproved.example"),
    }
    out = []
    for i in range(1, 201):
        out.append(special.get(i) or c(
            i, firstname=f"Business {i}", lastname="Shop",
            email=f"biz{i}@unique{i}.example",
            address=f"{100 + i} Solo Rd", city="Fort Worth",
            state="TX", zip="76102"))
    return out


class HubSpotFake:
    def __init__(self):
        self.contacts = _hs_contacts()
        self.calls = []

    def __call__(self, method, url, token=None, payload=None, data=None,
                 params=None):
        self.calls.append({"method": method, "url": url,
                           "token": token, "payload": payload,
                           "data": data, "params": params})
        if url.endswith("/oauth/v1/token"):
            return 200, {"access_token": "hs-token",
                         "refresh_token": "hs-refresh",
                         "expires_in": 1800, "hub_id": 4242}
        if method == "GET" and "/crm/v3/objects/contacts" in url:
            after = (params or {}).get("after")
            if after is None:
                return 200, {"results": self.contacts[:100],
                             "paging": {"next": {"after": "p2"}}}
            return 200, {"results": self.contacts[100:]}
        if method == "PATCH":
            return 200, {}
        if method == "POST" and url.endswith("/merge"):
            return 200, {}
        return 404, {}


def test_hubspot_unconfigured_is_honest(env, monkeypatch):
    fake, actor = env
    monkeypatch.delenv("DATACLEAN_HUBSPOT_CLIENT_ID")
    with pytest.raises(HTTPException) as exc:
        run(RC.authorize("hubspot", actor=actor))
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "not_configured"


def test_hubspot_callback_rejects_tampered_state(env, monkeypatch):
    fake, actor = env
    monkeypatch.setattr(conn, "http_json", HubSpotFake())
    with pytest.raises(HTTPException) as exc:
        run(RC.callback("hubspot",
                        RC.CallbackIn(code="x", state="bogus.state"),
                        actor=actor))
    assert exc.value.status_code == 400


def test_hubspot_round_trip_pull_scan_approve_writeback(env, monkeypatch):
    fake, actor = env
    hs = HubSpotFake()
    monkeypatch.setattr(conn, "http_json", hs)

    view = _connect(run, actor, "hubspot", hs)
    assert view["status"] == "connected"
    assert "hs-token" not in str(view)
    status = run(RC.list_connectors(actor=actor))
    hs_view = next(c for c in status["connectors"]
                   if c["provider"] == "hubspot")
    assert hs_view["connected"] is True
    assert "hs-token" not in str(status)

    pulled = run(RC.pull("hubspot", RC.PullIn(limit=200), actor=actor))
    assert pulled["pulled"] == 200
    list_id = pulled["list_id"]
    assert len(fake.dataclean_list_records.docs) == 200

    # Scan through the Phase B job pipeline — the deterministic
    # batched engine production uses for real lists (the small-list
    # sync path routes 200 records through Splink, whose untrained
    # conservatism on sparse fixtures is a product behavior, not
    # something this fixture should fight).
    job, err = run(jobs_lib.create_job(actor, "dedupe_scan", list_id))
    assert err is None
    job = run(jobs_lib.process_job(actor, job["id"]))
    assert job["status"] == "complete"
    assert job["result_counts"]["suggested"] >= 4
    sugg, _total = run(lists_lib.list_suggestions(actor, list_id))
    by_ext = {}
    rec_by_id = {d["id"]: d for d in fake.dataclean_list_records.docs}
    for s in sugg:
        pair = tuple(sorted((rec_by_id[s["a_id"]]["external_id"],
                             rec_by_id[s["b_id"]]["external_id"])))
        by_ext[pair] = s
    approved = [("hs-1", "hs-2"), ("hs-3", "hs-4"), ("hs-5", "hs-6")]
    for pair in approved:
        assert pair in by_ext, pair
        s = by_ext[pair]
        survivor_rec = rec_by_id[s["a_id"]]
        if survivor_rec["external_id"] != pair[0]:
            survivor_rec = rec_by_id[s["b_id"]]
        out, err = run(lists_lib.merge_suggestion(
            actor, list_id, s["id"], survivor_id=survivor_rec["id"]))
        assert err is None

    hs.calls.clear()
    out = run(RC.writeback("hubspot",
                           RC.WritebackIn(list_id=list_id), actor=actor))
    assert out["written"] == 3
    patches = [c for c in hs.calls if c["method"] == "PATCH"]
    merges = [c for c in hs.calls
              if c["method"] == "POST" and c["url"].endswith("/merge")]
    assert len(patches) == 3 and len(merges) == 3
    patch_props = {c["url"].rsplit("/", 1)[1]:
                   c["payload"]["properties"] for c in patches}
    # Only the approved blank fills, nothing else, per survivor.
    assert patch_props["hs-1"] == {"phone": "5125550134"}
    assert set(patch_props["hs-3"]) == {"address"}
    assert "1 Rose St" in patch_props["hs-3"]["address"]
    assert patch_props["hs-5"] == {"email": "crew@lsp.example"}
    merge_pairs = {(c["payload"]["primaryObjectId"],
                    c["payload"]["objectIdToMerge"]) for c in merges}
    assert merge_pairs == {("hs-1", "hs-2"), ("hs-3", "hs-4"),
                           ("hs-5", "hs-6")}
    # The unapproved pair was never touched.
    assert not any("hs-7" in str(c) or "hs-8" in str(c)
                   for c in hs.calls)
    # A second write-back is a no-op (already recorded).
    hs.calls.clear()
    out2 = run(RC.writeback("hubspot",
                            RC.WritebackIn(list_id=list_id), actor=actor))
    assert out2["written"] == 0 and hs.calls == []


# ------------------------------------------------------------------
# Salesforce: the no-API edition declines honestly
# ------------------------------------------------------------------

class SalesforceFake:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, token=None, payload=None, data=None,
                 params=None):
        self.calls.append({"method": method, "url": url})
        if url.endswith("/services/oauth2/token"):
            return 200, {"access_token": "sf-token",
                         "refresh_token": "sf-refresh",
                         "instance_url":
                             "https://acme.my.salesforce.com"}
        if "/query" in url:
            return 403, {"errorCode": "API_DISABLED_FOR_ORG",
                         "message": "API is not enabled for this org"}
        return 404, {}


def test_salesforce_no_api_edition_declines(env, monkeypatch):
    fake, actor = env
    sf = SalesforceFake()
    monkeypatch.setattr(conn, "http_json", sf)
    view = _connect(run, actor, "salesforce", sf)
    assert view["status"] == "connected"
    assert "sf-token" not in str(view)

    probe = run(RC.salesforce_probe(actor=actor))
    assert probe["available"] is False
    assert probe["connected"] is True
    assert "edition-gated" in probe["reason"]
    assert probe["provider_error"] == "API_DISABLED_FOR_ORG"

    with pytest.raises(HTTPException) as exc:
        run(RC.pull("salesforce", RC.PullIn(), actor=actor))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "salesforce_api_unavailable"
    assert "edition-gated" in exc.value.detail["message"]


def test_salesforce_writeback_is_decision_export_only(env, monkeypatch):
    fake, actor = env
    sf = SalesforceFake()
    monkeypatch.setattr(conn, "http_json", sf)
    lst = run(lists_lib.create_list(actor, "sf decisions",
                                    kind="customers", source="api"))
    run(lists_lib.append_records(actor, lst, [
        {"name": "Acme", "email": "a@x.example",
         "external_id": "001xx1"},
        {"name": "ACME", "email": "a@x.example", "phone": "5125550134",
         "external_id": "001xx2"},
    ]))
    run(lists_lib.scan_list(actor, lst))
    sugg, _total = run(lists_lib.list_suggestions(actor, lst["id"]))
    assert len(sugg) == 1
    out, err = run(lists_lib.merge_suggestion(actor, lst["id"],
                                              sugg[0]["id"]))
    assert err is None
    sf.calls.clear()
    res = run(RC.writeback("salesforce",
                           RC.WritebackIn(list_id=lst["id"]),
                           actor=actor))
    assert res["mode"] == "decision_export"
    assert len(res["export"]["merges"]) == 1
    assert res["export"]["merges"][0]["fills"] == {
        "phone": "5125550134"}
    assert sf.calls == []  # zero provider calls — export only
