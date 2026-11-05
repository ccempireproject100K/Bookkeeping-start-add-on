"""DataClean power-up Phase B tests (2026-11-03).

P1: async batch jobs — chunked dedupe over a 12,000-record list with
planted duplicates split ACROSS chunks (candidate harvesting must
find them), byte-identical pair sets vs the sync scan on the same
data, kill-and-resume, idempotent creation, tenant isolation, quota
ceiling at creation.
P7: the Studio merge-approval queue over stored lists — merge
semantics mirrored from the internal console (fill-blanks only,
absorbed flagged never deleted, snapshots stored, reversal restores
both records BYTE-EXACT), dismiss/restore, merge-decision export.
Clean jobs: chunked clean into an output list + change artifacts.

Same fake-db idiom as test_dataclean_phase3.py, extended with
insert_many / delete_one.
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

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)
        return type("R", (), {"inserted_ids": [d["_id"] for d in docs]})()

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

    async def delete_one(self, q):
        for k, d in list(self.docs.items()):
            if self._match(d, q):
                del self.docs[k]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))


COLL_NAMES = ("dataclean_api_keys", "dataclean_api_usage", "audit_log",
              "audit_chain", "customers", "vendors", "products",
              "import_profiles", "dataclean_lists",
              "dataclean_list_records", "dataclean_list_suggestions",
              "dataclean_list_merges", "dataclean_list_schedules",
              "dataclean_list_drift", "dataclean_jobs",
              "dataclean_job_artifacts", "dataclean_lookup_cache",
              "dataclean_connector_tokens")


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


def _patch_all(fake):
    import lib.audit as AUD
    import lib.dataclean_api as DCA
    import lib.dataclean_jobs as JOBS
    import lib.dataclean_lists as LISTS
    import lib.dataclean_schedules as SCHED
    import routers.dataclean_admin as ADM
    import routers.dataclean_api as API
    import routers.dataclean_jobs as RJ
    for mod in (DCA, JOBS, LISTS, SCHED, ADM, API):
        mod.db = fake
    AUD.db = fake
    RJ_db = RJ  # router uses libs, not db directly
    _ = RJ_db
    DCA.key_limiter._hits.clear()
    DCA.playground_limiter._hits.clear()
    return DCA, API, ADM, RJ


def _actor(fake, user=USER1, **key_kw):
    DCA, _API, ADM, _RJ = _patch_all(fake)
    out = run(ADM.create_key(ADM.CreateKeyBody(**key_kw), user))
    return run(DCA.get_dataclean_actor(
        FakeRequest({"X-API-Key": out["key"]})))


def _expect_http(status, fn, *args, **kwargs):
    try:
        run(fn(*args, **kwargs))
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTP {status}")


def _mk_list(RJ, actor, name, records):
    out = run(RJ.create_list(RJ.CreateListIn(name=name, records=records),
                             actor))
    return out["list"]["id"]


SMALL = [
    {"id": "s1", "name": "Acme Supply Inc", "email": "ops@acme.example",
     "phone": "", "address": "9 Oak Ave, Dallas, TX 75001"},
    {"id": "s2", "name": "ACME SUPPLY, INC.", "email": "",
     "phone": "(512) 555-0111", "address": "9 Oak Ave, Dallas, TX 75001"},
    {"id": "s3", "name": "Unrelated Co", "email": "u@u.example",
     "phone": "(512) 555-0999", "address": "1 Elm St, Plano, TX 75023"},
]


# ------------------------------------------------------------------ lists
def test_list_crud_append_and_tenant_isolation():
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    actor = _actor(fake)
    lid = _mk_list(RJ, actor, "vendors", SMALL[:1])
    out = run(RJ.append_records(lid, RJ.AppendIn(records=SMALL[1:]), actor))
    assert out["record_count"] == 3
    got = run(RJ.get_list(lid, actor))
    assert got["list"]["record_count"] == 3
    assert got["list"]["retention"]  # retention wording on the list
    other = _actor(fake, user=USER2)
    _expect_http(404, RJ.get_list, lid, other)
    _expect_http(404, RJ.append_records, lid,
                 RJ.AppendIn(records=[]), other)


# ------------------------------------------------------------------- queue
def test_queue_merge_parity_fill_blanks_snapshots_reverse():
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    actor = _actor(fake)
    lid = _mk_list(RJ, actor, "vendors", SMALL)
    scan = run(RJ.scan_list(lid, RJ.ScanIn(), actor))
    assert scan["suggested"] == 1
    sugg = run(RJ.get_suggestions(lid, actor=actor))["suggestions"]
    assert len(sugg) == 1
    sid = sugg[0]["id"]
    before = {r["id"]: r for r in
              run(RJ.get_records(lid, actor=actor))["records"]}
    out = run(RJ.merge(lid, sid, RJ.MergeIn(), actor))
    assert out["absorbed_id"] == "s2"  # default survivor: first record
    survivor = out["survivor"]
    # Fill-blanks ONLY: survivor keeps its own email, gains the phone
    # it lacked; its name/address are untouched.
    assert survivor["email"] == "ops@acme.example"
    assert survivor["phone"] == "(512) 555-0111"
    assert survivor["name"] == "Acme Supply Inc"
    recs = {r["id"]: r for r in run(RJ.get_records(lid, actor=actor))["records"]}
    assert recs["s2"]["status"] == "merged"
    assert recs["s2"]["merged_into"] == "s1"
    merges = run(RJ.get_merges(lid, actor))["merges"]
    assert len(merges) == 1
    assert merges[0]["survivor_before"]["phone"] == ""
    assert merges[0]["absorbed_before"]["email"] == ""
    # Export carries the decision + evidence for replay.
    exp = run(RJ.export_merges(lid, actor))
    assert exp["merges"][0]["fills"] == {"phone": "(512) 555-0111"}
    assert exp["merges"][0]["evidence"]["email_match"] is False
    # Reversal restores BOTH records byte-exact.
    mid = merges[0]["id"]
    run(RJ.reverse_merge(lid, mid, actor))
    after = {r["id"]: r for r in run(RJ.get_records(lid, actor=actor))["records"]}
    for rid in ("s1", "s2", "s3"):
        assert after[rid] == before[rid], rid


def test_queue_dismiss_restore_and_conflicts():
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    actor = _actor(fake)
    lid = _mk_list(RJ, actor, "vendors", SMALL)
    run(RJ.scan_list(lid, RJ.ScanIn(), actor))
    sid = run(RJ.get_suggestions(lid, actor=actor))["suggestions"][0]["id"]
    out = run(RJ.dismiss(lid, sid, actor))
    assert out["status"] == "dismissed"
    assert run(RJ.get_suggestions(lid, actor=actor))["total"] == 0
    dismissed = run(RJ.get_suggestions(lid, status="dismissed",
                                       actor=actor))
    assert dismissed["total"] == 1
    _expect_http(409, RJ.dismiss, lid, sid, actor)
    out = run(RJ.restore(lid, sid, actor))
    assert out["status"] == "pending"
    exp = run(RJ.export_merges(lid, actor))
    assert len(exp["dismissals"]) == 0  # restored -> no longer dismissed
    _expect_http(404, RJ.dismiss, lid, "nope", actor)


def test_cross_list_scan_via_lists():
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    actor = _actor(fake)
    la = _mk_list(RJ, actor, "crm", SMALL)
    lb = _mk_list(RJ, actor, "books", [
        {"id": "b1", "name": "Acme Supply Inc",
         "email": "ops@acme.example", "phone": "(512) 555-0111",
         "address": "9 Oak Ave, Dallas, TX 75001"},
        {"id": "b2", "name": "Other LLC", "email": "o@o.example",
         "phone": "", "address": ""}])
    out = run(RJ.scan_list(la, RJ.ScanIn(other_list_id=lb), actor))
    assert out["mode"] == "cross" and out["suggested"] >= 1
    sugg = run(RJ.get_suggestions(la, actor=actor))["suggestions"]
    assert all(s["mode"] == "cross" for s in sugg)
    assert {(s["a_id"], s["b_id"]) for s in sugg} >= {("s1", "b1")}


# -------------------------------------------------------------------- jobs
def _big_records(n=12000):
    recs = []
    for i in range(n):
        recs.append({"id": f"r{i}", "name": f"Vendor {i} Enterprises",
                     "email": f"v{i}@v.example",
                     "phone": f"(512) 555-{1000 + i % 9000}",
                     "address": f"{i} Main St, Austin, TX 78701"})
    # Planted duplicates: one inside chunk 1, one SPLIT across chunk 1
    # and chunk 3, one inside chunk 3.
    recs[10] = {"id": "r10", "name": "Planted Dup Co",
                "email": "dup@dup.example", "phone": "(512) 555-0111",
                "address": "5 Dup Rd, Dallas, TX 75001"}
    recs[20] = {"id": "r20", "name": "PLANTED DUP CO",
                "email": "dup@dup.example", "phone": "512.555.0111",
                "address": "5 Dup Rd, Dallas, TX 75001"}
    recs[11000] = {"id": "r11000", "name": "Planted Dup Co",
                   "email": "dup@dup.example", "phone": "(512) 555-0111",
                   "address": "5 Dup Rd, Dallas, TX 75001"}
    return recs


def test_dedupe_job_end_to_end_cross_chunk_and_resume():
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    import lib.dataclean_jobs as JOBS
    actor = _actor(fake)
    recs = _big_records()
    lid = _mk_list(RJ, actor, "big", recs[:5000])
    run(RJ.append_records(lid, RJ.AppendIn(records=recs[5000:10000]),
                          actor))
    run(RJ.append_records(lid, RJ.AppendIn(records=recs[10000:]),
                          actor))
    out = run(RJ.create_job(RJ.CreateJobIn(
        kind="dedupe_scan", list_id=lid, idempotency_key="job-1"), actor))
    job = out["job"]
    assert job["rows_total"] == 12000 and job["chunks_total"] == 3
    # Idempotent re-POST returns the same job.
    again = run(RJ.create_job(RJ.CreateJobIn(
        kind="dedupe_scan", list_id=lid, idempotency_key="job-1"), actor))
    assert again["job"]["id"] == job["id"]
    done = run(JOBS.process_job(actor, job["id"]))
    assert done["status"] == "complete", done.get("error")
    assert done["result_counts"]["suggested"] >= 2
    pairs, total = run(JOBS.job_results(actor, job["id"], band="suggested",
                                        limit=1000)), None
    pairset = {(p["a_id"], p["b_id"]) for p in pairs[0]} | \
              {(p["b_id"], p["a_id"]) for p in pairs[0]}
    # The cross-chunk planted pair (chunk 1 x chunk 3) MUST be found —
    # that is the candidate-harvesting proof.
    assert ("r10", "r11000") in pairset
    assert ("r10", "r20") in pairset
    # Suggestions also landed in the queue store.
    sugg = run(RJ.get_suggestions(lid, actor=actor, limit=500))
    assert sugg["total"] >= 2
    # Kill-and-resume: rewind to a stale running state with partial
    # chunk marks and cleared artifacts; processing again completes
    # with the same pair set.
    import lib.dataclean_lists as LISTS
    coll = fake.dataclean_job_artifacts
    coll.docs.clear()
    fake.dataclean_jobs.docs[next(iter(fake.dataclean_jobs.docs))].update(
        {"status": "running", "chunks_done": [f"{lid}:0"],
         "result_counts": None})
    done2 = run(JOBS.process_job(actor, job["id"]))
    assert done2["status"] == "complete"
    pairs2 = run(JOBS.job_results(actor, job["id"], band="suggested",
                                  limit=1000))
    pairset2 = {(p["a_id"], p["b_id"]) for p in pairs2[0]} | \
               {(p["b_id"], p["a_id"]) for p in pairs2[0]}
    assert pairset2 == pairset


def test_job_matches_sync_scan_byte_identical_pairs():
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    import lib.dataclean_jobs as JOBS
    import lib.dataclean_lists as LISTS
    actor = _actor(fake)
    lid = _mk_list(RJ, actor, "vendors", SMALL)
    out = run(RJ.create_job(RJ.CreateJobIn(kind="dedupe_scan",
                                           list_id=lid), actor))
    done = run(JOBS.process_job(actor, out["job"]["id"]))
    assert done["status"] == "complete", done.get("error")
    job_pairs = {(p["a_id"], p["b_id"]) for p in
                 run(JOBS.job_results(actor, out["job"]["id"],
                                      band="suggested"))[0]}
    lid2 = _mk_list(RJ, actor, "vendors2", SMALL)
    run(RJ.scan_list(lid2, RJ.ScanIn(), actor))
    sync_pairs = {(s["a_id"], s["b_id"]) for s in
                  run(RJ.get_suggestions(lid2, actor=actor))["suggestions"]}
    assert job_pairs == sync_pairs == {("s1", "s2")}


def test_job_quota_ceiling_and_too_large():
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    actor = _actor(fake, monthly_row_quota=2)
    lid = _mk_list(RJ, actor, "vendors", SMALL)
    e = _expect_http(429, RJ.create_job,
                     RJ.CreateJobIn(kind="dedupe_scan", list_id=lid),
                     actor)
    assert e.detail["code"] == "quota_exceeded"
    # The refused job left nothing in the queue.
    assert run(RJ.get_jobs(actor))["jobs"] == []
    _expect_http(404, RJ.create_job,
                 RJ.CreateJobIn(kind="dedupe_scan", list_id="nope"),
                 actor)
    _expect_http(422, RJ.create_job,
                 RJ.CreateJobIn(kind="explode", list_id=lid), actor)


def test_clean_job_chunks_into_output_list():
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    import lib.dataclean_jobs as JOBS
    actor = _actor(fake)
    messy = [{"id": f"m{i}", "name": "Messy  Vendor",
              "email": "OPS@EXAMPLE.COM ", "phone": "(512) 555-0111",
              "address": "9  Oak   Ave, Dallas, TX 75001"}
             for i in range(3)]
    lid = _mk_list(RJ, actor, "messy", messy)
    out = run(RJ.create_job(RJ.CreateJobIn(kind="clean", list_id=lid),
                            actor))
    done = run(JOBS.process_job(actor, out["job"]["id"]))
    assert done["status"] == "complete", done.get("error")
    assert done["result_counts"]["changes"] > 0
    out_list = run(RJ.get_list(done["output_list_id"], actor))["list"]
    assert out_list["record_count"] == 3
    recs = run(RJ.get_records(done["output_list_id"], actor=actor))["records"]
    assert recs[0]["phone"] == "+15125550111"
    assert recs[0]["email"] == "ops@example.com"
    changes = run(RJ.get_job_result(out["job"]["id"], kind="change",
                                    actor=actor))
    assert changes["total"] == done["result_counts"]["changes"]
