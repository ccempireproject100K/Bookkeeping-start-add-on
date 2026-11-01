"""DataClean Phase 3 tests (2026-10-30) — the external API product shell.

Pins:
1. Keys: shown once at issue, stored hashed only, masked in lists,
   revocable, tenant-scoped; a key authenticates ONLY its own tenant
   and ONLY on this surface (a books `maek_` key is refused).
2. The versioned surface reuses the engine verbatim: preview payload
   matches the internal /import/cleanup-preview shape, normalize
   returns dataclean.result/1 dicts untouched, dedupe pairs carry the
   internal evidence shape.
3. Metering: every compute call writes a dataclean_api_usage row
   (tenant, key, endpoint, rows, status) and updates key counters;
   rate-limit and quota refusals are metered too, with honest 429s.
4. Playground: runs the real preview, persists NOTHING (every fake
   collection stays empty), enforces its caps and its IP rate limit.

Same fake-db idiom as test_cleanup_phase2.py (plus $inc support, which
record_usage's counter upkeep uses).
"""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ------------------------------------------------------------------ fakes
class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec or []):
            self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n=None, length=None):
        return self._docs[:(n if n is not None else length)]


class FakeColl:
    def __init__(self):
        self.docs = {}
        self._seq = 0

    def _match(self, doc, q):
        for k, v in (q or {}).items():
            dv = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]:
                    return False
                if "$ne" in v and dv == v["$ne"]:
                    return False
                if "$exists" in v and ((k in doc) != bool(v["$exists"])):
                    return False
            elif dv != v:
                return False
        return True

    async def insert_one(self, doc):
        self._seq += 1
        doc.setdefault("_id", f"oid{self._seq}")
        self.docs[doc["_id"]] = dict(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    def find(self, q=None, proj=None):
        return FakeCursor([dict(d) for d in self.docs.values()
                           if self._match(d, q)])

    async def find_one(self, q=None, proj=None):
        for d in self.docs.values():
            if self._match(d, q or {}):
                return dict(d)
        return None

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs.values():
            if self._match(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                for k, v in (upd.get("$inc") or {}).items():
                    d[k] = (d.get(k) or 0) + v
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))


COLL_NAMES = ("dataclean_api_keys", "dataclean_api_usage", "audit_log",
              "customers", "vendors", "products", "import_profiles")


class FakeDB:
    def __init__(self):
        for name in COLL_NAMES:
            setattr(self, name, FakeColl())


class FakeRequest:
    def __init__(self, headers=None, host="203.0.113.7"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()


USER1 = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.example",
         "role": "owner", "permissions": ["*"]}
USER2 = {"id": "u2", "tenant_id": "t2", "email": "other@shop.example",
         "role": "owner", "permissions": ["*"]}

CSV_TWO = ("Client,E-mail,Phone,Address\n"
           "Acme Supply Inc,ops@acme.example,(512) 555-0134,"
           "\"9 Oak Ave, Dallas, TX 75001\"\n"
           "Lone Star Plumbing,,512.555.0142,"
           "\"77 Ranch Rd, Austin, TX 78701\"\n")

DUPE_RECORDS = [
    {"id": "c1", "name": "Acme Supply Inc", "phone": "(512) 555-0111",
     "email": "a@acme.example", "address": ""},
    {"id": "c2", "name": "ACME SUPPLY, INC.", "phone": "512.555.0111",
     "email": "a@acme.example", "address": ""},
    {"id": "c3", "name": "Unrelated Co", "phone": "(512) 555-0999",
     "email": "u@u.example", "address": ""},
]


def _patch_all(fake):
    import lib.audit as AUD
    import lib.dataclean_api as DCA
    import routers.dataclean_admin as ADM
    import routers.dataclean_api as API
    DCA.db = fake
    API.db = fake
    ADM.db = fake
    AUD.db = fake
    DCA.key_limiter._hits.clear()
    DCA.playground_limiter._hits.clear()
    return DCA, API, ADM


def _issue(API_ADM, user, **kw):
    ADM = API_ADM
    return run(ADM.create_key(ADM.CreateKeyBody(**kw), user))


def _actor(DCA, full_key, header="X-API-Key"):
    return run(DCA.get_dataclean_actor(FakeRequest({header: full_key})))


def _expect_http(status, fn, *args, **kwargs):
    try:
        run(fn(*args, **kwargs))
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTP {status}")


# ------------------------------------------------------------------ keys
def test_key_shown_once_stored_hashed_and_masked_in_lists():
    fake = FakeDB()
    _DCA, _API, ADM = _patch_all(fake)
    out = _issue(ADM, USER1, label="Zapier bridge")
    full = out["key"]
    assert full.startswith("mdck_")
    assert out["key_stub"] in full or full.startswith(out["key_stub"][:13])
    stored = list(fake.dataclean_api_keys.docs.values())
    assert len(stored) == 1
    doc = stored[0]
    assert doc["key_hash"] and doc["key_hash"] != full
    assert full not in str(doc), "plaintext key must never be stored"
    assert doc["scopes"] == ["dataclean:use"]
    assert doc["tenant_id"] == "t1" and doc["calls_total"] == 0
    listed = run(ADM.list_keys(USER1))
    assert len(listed) == 1
    assert "key" not in listed[0] and "key_hash" not in listed[0]
    assert listed[0]["key_stub"] == doc["key_stub"]


def test_key_auth_both_headers_and_generic_401s():
    fake = FakeDB()
    DCA, _API, ADM = _patch_all(fake)
    full = _issue(ADM, USER1)["key"]
    a1 = _actor(DCA, full)
    assert a1["tenant_id"] == "t1" and a1["auth_kind"] == "api_key"
    a2 = _actor(DCA, f"Bearer {full}", header="Authorization")
    assert a2["api_key_id"] == a1["api_key_id"]
    e = _expect_http(401, DCA.get_dataclean_actor,
                     FakeRequest({"X-API-Key": "mdck_wrong"}))
    assert e.detail["code"] == "invalid_api_key"
    # A books key (lib/developer.py prefix) is NOT a DataClean key.
    _expect_http(401, DCA.get_dataclean_actor,
                 FakeRequest({"X-API-Key": "maek_whatever"}))
    _expect_http(401, DCA.get_dataclean_actor, FakeRequest({}))


def test_revoke_kills_the_key_and_is_tenant_scoped():
    fake = FakeDB()
    DCA, _API, ADM = _patch_all(fake)
    out = _issue(ADM, USER1)
    kid, full = out["id"], out["key"]
    # Another tenant cannot revoke (or even see) this key.
    _expect_http(404, ADM.revoke_key, kid, USER2)
    assert run(ADM.list_keys(USER2)) == []
    res = run(ADM.revoke_key(kid, USER1))
    assert res["ok"] and res["revoked_at"]
    _expect_http(401, DCA.get_dataclean_actor,
                 FakeRequest({"X-API-Key": full}))
    # Revoking twice is a calm no-op, not an error.
    again = run(ADM.revoke_key(kid, USER1))
    assert again["revoked_at"] == res["revoked_at"]


# ------------------------------------------------------- surface behaviour
def test_preview_matches_internal_console_shape():
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    import routers.importer as IMP
    IMP.db = fake
    actor = _actor(DCA, _issue(ADM, USER1)["key"])
    ext = run(API.cleanup_preview(
        API.CleanupIn(kind="customers", csv_text=CSV_TWO), actor))
    internal = run(IMP.cleanup_preview(
        IMP.CleanupPreviewIn(kind="customers", csv_text=CSV_TWO), USER1))
    for key in internal:
        assert key in ext, f"external preview missing internal key {key}"
        assert ext[key] == internal[key], f"preview key {key} diverged"
    assert ext["shape"] == "dataclean.result/1"
    assert ext["rows"] == 2 and ext["can_import"] is True


def test_clean_rows_payload_and_strict_fields():
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    actor = _actor(DCA, _issue(ADM, USER1)["key"])
    out = run(API.cleanup_clean(API.CleanupIn(kind="customers", rows=[
        {"name": "Acme  Supply", "phone": "(512) 555-0134",
         "email": " OPS@ACME.EXAMPLE "},
    ]), actor))
    assert out["shape"] == "dataclean.result/1" and out["rows"] == 1
    rec = out["records"][0]
    assert rec["phone"] == "+15125550134"
    assert rec["email"] == "ops@acme.example"
    rules = {c["rule"] for c in out["changes"]}
    assert "phone:libphonenumber-e164" in rules
    assert out["changes_by_rule"]["phone:libphonenumber-e164"] == 1
    e = _expect_http(422, API.cleanup_clean, API.CleanupIn(
        kind="customers", rows=[{"name": "X", "ssn": "123"}]), actor)
    assert e.detail["code"] == "unknown_field"
    e = _expect_http(422, API.cleanup_clean, API.CleanupIn(
        kind="martians", rows=[]), actor)
    assert e.detail["code"] == "unknown_kind"


def test_normalize_returns_engine_results_verbatim():
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    actor = _actor(DCA, _issue(ADM, USER1)["key"])
    out = run(API.normalize(API.NormalizeIn(
        type="phone", values=["(512) 555-0134", "not a phone"]), actor))
    assert out["shape"] == "dataclean.result/1"
    r0, r1 = out["results"]
    assert r0["shape"] == "dataclean.result/1"
    assert r0["ok"] is True and r0["normalized"] == "+15125550134"
    assert r0["original"] == "(512) 555-0134" and "rule" in r0
    assert r1["ok"] is False and r1["normalized"] is None
    out = run(API.normalize(API.NormalizeIn(
        type="date", values=["03/04/2026"]), actor))
    assert out["results"][0]["normalized"] == "2026-03-04"
    e = _expect_http(422, API.normalize, API.NormalizeIn(
        type="blood-type", values=["A+"]), actor)
    assert e.detail["code"] == "unknown_type"


def test_dedupe_scan_evidence_shape_and_cap():
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    actor = _actor(DCA, _issue(ADM, USER1)["key"])
    out = run(API.dedupe_scan(API.DedupeIn(records=DUPE_RECORDS), actor))
    assert out["engine"] in ("deterministic", "splink",
                             "deterministic-fallback")
    assert len(out["pairs"]) == 1
    pair = out["pairs"][0]
    assert {pair["a_id"], pair["b_id"]} == {"c1", "c2"}
    assert set(pair["evidence"]) == {"name_similarity", "email_match",
                                     "phone_match", "address_similarity",
                                     "why"}
    big = [{"name": f"Co {i}"} for i in range(501)]
    e = _expect_http(413, API.dedupe_scan, API.DedupeIn(records=big), actor)
    assert e.detail["code"] == "payload_too_large"


def test_dedupe_scan_accepts_clean_output_verbatim():
    """Regression: clean's records carry an address_parts DICT; the scan
    handler stringifies values, and linkage.features() used to call
    .get() on that string — posting clean's output verbatim into
    dedupe/scan was an HTTP 500 for every consumer except the Studio
    (which projected client-side). The verbatim composition must scan
    cleanly and answer in the standard shape."""
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    actor = _actor(DCA, _issue(ADM, USER1)["key"])
    cleaned = run(API.cleanup_clean(API.CleanupIn(kind="customers", rows=[
        {"name": "Acme  Supply Inc", "email": "OPS@ACME.EXAMPLE",
         "phone": "(512) 555-0134", "address": "9 Oak Ave, Dallas, TX 75001"},
        {"name": "ACME SUPPLY, INC.", "email": "ops@acme.example",
         "phone": "512.555.0134", "address": "9 Oak Ave, Dallas, TX 75001"},
    ]), actor))
    records = cleaned["records"]
    assert all(isinstance(r.get("address_parts"), dict) for r in records)
    out = run(API.dedupe_scan(API.DedupeIn(records=records), actor))
    assert out["shape"] == "dataclean.result/1"
    assert out["records"] == 2
    assert len(out["pairs"]) == 1
    pair = out["pairs"][0]
    assert {pair["a_id"], pair["b_id"]} == {"row-1", "row-2"}
    assert set(pair["evidence"]) == {"name_similarity", "email_match",
                                     "phone_match", "address_similarity",
                                     "why"}


# ---------------------------------------------------------------- metering
def test_every_compute_call_is_metered_and_counted():
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    out = _issue(ADM, USER1)
    actor = _actor(DCA, out["key"])
    run(API.cleanup_preview(API.CleanupIn(
        kind="customers", csv_text=CSV_TWO), actor))
    run(API.normalize(API.NormalizeIn(type="phone", values=["5125550134"]),
                      actor))
    rows = list(fake.dataclean_api_usage.docs.values())
    assert len(rows) == 2
    by_ep = {r["endpoint"]: r for r in rows}
    assert by_ep["cleanup/preview"]["rows_processed"] == 2
    assert by_ep["normalize"]["rows_processed"] == 1
    for r in rows:
        assert r["tenant_id"] == "t1" and r["key_id"] == out["id"]
        assert r["status"] == "ok" and r["created_at"]
    doc = list(fake.dataclean_api_keys.docs.values())[0]
    assert doc["calls_total"] == 2 and doc["rows_total"] == 3
    assert doc["last_used_at"]
    own = run(API.own_usage(actor))
    assert own["calls_recorded"] == 2 and own["rows_this_month"] == 3
    assert own["by_endpoint"] == {"cleanup/preview": 1, "normalize": 1}


def test_rate_limit_429_is_honest_and_metered():
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    out = _issue(ADM, USER1, rate_limit_per_minute=2)
    actor = _actor(DCA, out["key"])
    body = API.NormalizeIn(type="phone", values=["5125550134"])
    run(API.normalize(body, actor))
    run(API.normalize(body, actor))
    e = _expect_http(429, API.normalize, body, actor)
    assert e.detail["code"] == "rate_limited"
    assert e.detail["retry_after_seconds"] >= 1
    assert e.headers["Retry-After"] == str(e.detail["retry_after_seconds"])
    statuses = [r["status"] for r in fake.dataclean_api_usage.docs.values()]
    assert statuses.count("rate_limited") == 1 and statuses.count("ok") == 2


def test_monthly_row_quota_429():
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    out = _issue(ADM, USER1, monthly_row_quota=3)
    actor = _actor(DCA, out["key"])
    rows2 = [{"name": "A Co"}, {"name": "B Co"}]
    run(API.cleanup_clean(API.CleanupIn(kind="customers", rows=rows2), actor))
    e = _expect_http(429, API.cleanup_clean, API.CleanupIn(
        kind="customers", rows=rows2), actor)
    assert e.detail["code"] == "quota_exceeded"
    assert e.detail["rows_used_this_month"] == 2
    assert e.detail["monthly_row_quota"] == 3
    statuses = [r["status"] for r in fake.dataclean_api_usage.docs.values()]
    assert "quota_exceeded" in statuses


def test_admin_usage_aggregates_come_from_the_rows():
    fake = FakeDB()
    DCA, API, ADM = _patch_all(fake)
    k1 = _issue(ADM, USER1, label="One")
    k2 = _issue(ADM, USER1, label="Two")
    a1, a2 = _actor(DCA, k1["key"]), _actor(DCA, k2["key"])
    run(API.normalize(API.NormalizeIn(type="phone", values=["5125550134"]),
                      a1))
    run(API.cleanup_preview(API.CleanupIn(
        kind="customers", csv_text=CSV_TWO), a2))
    agg = run(ADM.usage(30, USER1))
    assert agg["totals"] == {"calls": 2, "rows": 3}
    assert len(agg["by_day"]) == 30
    assert sum(d["calls"] for d in agg["by_day"]) == 2
    assert agg["tracking_since"]
    assert {b["label"] for b in agg["by_key"]} == {"One", "Two"}
    # The other tenant sees an honest empty aggregate, not t1's numbers.
    agg2 = run(ADM.usage(30, USER2))
    assert agg2["totals"] == {"calls": 0, "rows": 0}
    assert agg2["tracking_since"] is None and agg2["by_key"] == []


def test_pricing_config_is_placeholder_flagged_config_only():
    fake = FakeDB()
    DCA, API, _ADM = _patch_all(fake)
    cfg = run(API.pricing())
    assert cfg["placeholder"] is True and cfg["billing_mode"] == "test"
    assert cfg["currency"] == "USD"
    assert [p["id"] for p in cfg["plans"]] == ["starter", "growth", "scale"]
    for p in cfg["plans"]:
        assert p["monthly_price_cents"] == 0 and p["rows_included"] > 0
    assert cfg["playground"]["max_rows"] == 200


def test_pricing_config_carries_the_bundle_with_honest_flags():
    fake = FakeDB()
    _DCA, API, _ADM = _patch_all(fake)
    cfg = run(API.pricing())
    # The plan prices stay placeholders; the bundle rides alongside.
    assert cfg["placeholder"] is True
    assert len(cfg["bundles"]) == 1
    bundle = cfg["bundles"][0]
    assert bundle["id"] == "dataclean-security"
    assert bundle["name"] == "DataClean + Security"
    # The price is the owner's 2026-10-31 decision — a real price,
    # flagged as such, not a $0 placeholder.
    assert bundle["monthly_price_cents"] == 1899
    assert bundle["placeholder"] is False
    halves = {c["id"]: c for c in bundle["components"]}
    assert set(halves) == {"dataclean", "security"}
    assert halves["dataclean"]["status"] == "live"
    assert halves["dataclean"]["built"] is True
    assert halves["dataclean"]["available"] is True
    # The security half is NOT built: honestly coming soon, never
    # available, and carrying no feature claims of any kind.
    assert halves["security"]["status"] == "coming_soon"
    assert halves["security"]["built"] is False
    assert halves["security"]["available"] is False
    assert "features" not in halves["security"]


# --------------------------------------------------------------- playground
def test_playground_runs_real_engine_and_persists_nothing():
    fake = FakeDB()
    _DCA, API, _ADM = _patch_all(fake)
    out = run(API.playground_preview(
        API.PlaygroundIn(kind="customers", csv_text=CSV_TWO),
        FakeRequest()))
    assert out["demo"] is True and out["persisted"] is False
    assert out["rows"] == 2 and out["can_import"] is True
    assert out["mess_report"]["rows"] == 2
    assert out["changes_in_first_50_rows"] > 0
    assert out["caps"]["max_file_bytes"] == 65536
    for name in COLL_NAMES:
        assert getattr(fake, name).docs == {}, \
            f"playground wrote to {name}"


def test_playground_caps_and_ip_rate_limit(monkeypatch=None):
    fake = FakeDB()
    DCA, API, _ADM = _patch_all(fake)
    big = "Name,Phone\n" + "\n".join(
        f"Co {i},51255501{i:02d}" for i in range(201))
    e = _expect_http(413, API.playground_preview, API.PlaygroundIn(
        kind="customers", csv_text=big), FakeRequest())
    assert e.detail["code"] == "playground_limit"
    huge = "Name\n" + "x" * 70000
    e = _expect_http(413, API.playground_preview, API.PlaygroundIn(
        kind="customers", csv_text=huge), FakeRequest())
    assert e.detail["code"] == "playground_limit"
    os.environ["DATACLEAN_PLAYGROUND_RATE"] = "2"
    try:
        req = FakeRequest(host="198.51.100.9")
        body = API.PlaygroundIn(kind="customers", csv_text=CSV_TWO)
        run(API.playground_preview(body, req))
        run(API.playground_preview(body, req))
        e = _expect_http(429, API.playground_preview, body, req)
        assert e.detail["code"] == "rate_limited"
        # A different IP is a different bucket.
        run(API.playground_preview(body, FakeRequest(host="198.51.100.10")))
    finally:
        del os.environ["DATACLEAN_PLAYGROUND_RATE"]
