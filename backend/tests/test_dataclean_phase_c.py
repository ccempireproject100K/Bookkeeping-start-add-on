"""Phase C (continuous mode): scheduled re-scans + drift reports.

Pins:
  * schedule CRUD via the router (invalid cadence refused 422);
  * drift semantics across runs: the baseline is the PREVIOUS run's
    after-state, so a human merge between runs reads as "resolved",
    an appended duplicate reads as "new", and an edited record whose
    pair score moves reads as "changed";
  * the drift webhook is HMAC-SHA256-signed with the creating key's
    hash, and a delivery failure is recorded on the report, never
    raised into the sweep;
  * the sweep runs due schedules and skips future ones;
  * deleting a list removes its schedule and drift history too.

Same FakeDB idiom as Phases A/B (extended with delete_many).
"""
import asyncio
import hashlib
import hmac
import json
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib import dataclean_api as dca
from lib import dataclean_jobs as jobs_lib
from lib import dataclean_lists as lists_lib
from lib import dataclean_schedules as sched_lib
from lib import audit as audit_lib
from lib import scheduler as scheduler_lib
from routers import dataclean_jobs as RJ
import db as db_module

TENANT = "ws-phase-c"
KEY_HASH = "hash-of-the-creating-key"


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
    "dataclean_list_merges", "dataclean_list_schedules",
    "dataclean_list_drift", "dataclean_jobs", "dataclean_job_artifacts",
    "audit_chain", "audit_log",
)

_MODULES = (dca, jobs_lib, lists_lib, sched_lib, RJ, audit_lib,
            scheduler_lib)


def _patch_all(monkeypatch):
    fake = FakeDB()
    for name in COLL_NAMES:
        getattr(fake, name)
    for module in _MODULES:
        monkeypatch.setattr(module, "db", fake, raising=False)
    monkeypatch.setattr(db_module, "db", fake, raising=False)
    key = {"id": "key-c", "tenant_id": TENANT, "key_hash": KEY_HASH,
           "key_stub": "mdck_phase_c", "label": "phase-c",
           "daily_call_limit": 100000, "per_minute_limit": 100000,
           "monthly_row_quota": 1000000, "revoked_at": None}
    fake.dataclean_api_keys.docs.append(key)
    actor = {"id": "key-c", "tenant_id": TENANT, "api_key_id": "key-c",
             "auth_kind": "api_key", "role": "dataclean_api_key",
             "_key_doc": key}
    return fake, actor


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def env(monkeypatch):
    return _patch_all(monkeypatch)


def _make_list(run_, actor, records, name="watched"):
    out = run_(RJ.create_list(RJ.CreateListIn(name=name, kind="customers",
                                              records=records),
                              actor=actor))
    return out["list"]["id"]


def _make_schedule(run_, actor, list_id, **kw):
    return run_(RJ.put_schedule(list_id, RJ.ScheduleIn(**kw), actor=actor))


DUP_A = {"name": "Acme Supply", "email": "ops@acme.example",
         "address": "9 Oak Ave, Dallas, TX 75001"}
DUP_B = {"name": "ACME SUPPLY INC", "email": "ops@acme.example",
         "address": "9 Oak Avenue, Dallas, TX 75001"}


# ------------------------------------------------------------------
# Schedule CRUD
# ------------------------------------------------------------------

def test_schedule_crud_and_validation(env):
    fake, actor = env
    lid = _make_list(run, actor, [DUP_A, DUP_B])
    with pytest.raises(HTTPException) as exc:
        run(RJ.put_schedule(lid, RJ.ScheduleIn(cadence="hourly"),
                            actor=actor))
    assert exc.value.status_code == 422
    out = _make_schedule(run, actor, lid, cadence="weekly")
    assert out["schedule"]["cadence"] == "weekly"
    assert out["schedule"]["next_run_at"] > "2027-01-01T00:00:00Z" or \
        out["schedule"]["next_run_at"] > "2026-11-10T00:00:00Z"
    got = run(RJ.get_schedule(lid, actor=actor))
    assert got["schedule"]["id"] == out["schedule"]["id"]
    # retention wording rides along for the customer to read
    assert "kept until you delete" in got["retention"]
    deleted = run(RJ.delete_schedule(lid, actor=actor))
    assert deleted["deleted"] is True
    with pytest.raises(HTTPException) as exc2:
        run(RJ.get_schedule(lid, actor=actor))
    assert exc2.value.status_code == 404


# ------------------------------------------------------------------
# Drift across runs
# ------------------------------------------------------------------

def _due(fake, schedule_id):
    """Rewind a schedule so the next run is due."""
    for d in fake.dataclean_list_schedules.docs:
        if d["id"] == schedule_id:
            d["next_run_at"] = "2020-01-01T00:00:00Z"


def test_drift_new_resolved_changed_across_runs(env):
    fake, actor = env
    lid = _make_list(run, actor, [DUP_A, DUP_B])
    sched = _make_schedule(run, actor, lid, cadence="daily")["schedule"]

    # Run 1: the dup pair is new.
    rep1 = run(sched_lib.run_scheduled_scan(actor, sched))
    assert rep1["counts"] == {"new": 1, "resolved": 0, "changed": 0}

    # The human merges the pair; a fresh duplicate arrives.
    pend = run(RJ.get_suggestions(lid, actor=actor))["suggestions"]
    assert len(pend) == 1
    run(RJ.merge(lid, pend[0]["id"], RJ.MergeIn(), actor=actor))
    run(RJ.append_records(lid, RJ.AppendIn(records=[
        {"name": "Bluebonnet Bakery", "email": "hi@bb.example",
         "phone": "5125550100",
         "address": "1 Rose St, Austin, TX 78701"},
        {"name": "BLUEBONNET BAKERY", "email": "hi@bb.example",
         "phone": "(512) 555-0100",
         "address": "1 Rose Street, Austin, TX 78701"},
    ]), actor=actor))

    # Run 2: merged pair is "resolved", bakery pair is "new".
    sched2 = run(sched_lib.get_schedule(actor, lid))
    rep2 = run(sched_lib.run_scheduled_scan(actor, sched2))
    assert rep2["counts"] == {"new": 1, "resolved": 1, "changed": 0}
    assert rep2["resolved_pairs"][0]["a_id"] in {DUP_A and pend[0]["a_id"],
                                                 pend[0]["b_id"]}

    # Edit one bakery record's phone AWAY from its twin: the pair is
    # pending in both runs, but the phone agreement that propped its
    # score up is gone, so the score drops materially (a shared phone
    # that stops matching is exactly the drift a watcher wants).
    recs = run(RJ.get_records(lid, actor=actor))["records"]
    target = next(r for r in recs
                  if r["name"] == "BLUEBONNET BAKERY")
    coll = fake.dataclean_list_records
    for d in coll.docs:
        if d["id"] == target["id"]:
            d["phone"] = "5125557777"
    sched3 = run(sched_lib.get_schedule(actor, lid))
    rep3 = run(sched_lib.run_scheduled_scan(actor, sched3))
    assert rep3["counts"]["changed"] == 1
    assert rep3["counts"]["new"] == 0
    assert rep3["counts"]["resolved"] == 0
    chg = rep3["changed_pairs"][0]
    assert abs(chg["score_after"] - chg["score_before"]) > 0.001

    # Drift history is served newest-first.
    out = run(RJ.get_drift(lid, actor=actor))
    assert out["latest"]["id"] == rep3["id"]
    assert len(out["history"]) == 3


# ------------------------------------------------------------------
# Webhook: signed, failures recorded
# ------------------------------------------------------------------

def test_drift_webhook_signed_and_failure_recorded(env, monkeypatch):
    fake, actor = env
    lid = _make_list(run, actor, [DUP_A, DUP_B])
    _make_schedule(run, actor, lid, cadence="daily",
                   webhook_url="https://hooks.example.com/dc")
    # The router's view strips the signing hash (correctly); the run
    # works on the full stored doc, like the sweep does.
    sched = run(sched_lib.get_schedule(actor, lid))

    sent = {}

    class Resp:
        status_code = 202

    def fake_post(url, payload, headers):
        sent.update(url=url, payload=payload, headers=headers)
        return Resp()

    monkeypatch.setattr(sched_lib, "http_post_json", fake_post)
    rep = run(sched_lib.run_scheduled_scan(actor, sched))
    assert rep["webhook_status"] == 202
    body = json.dumps(sent["payload"], sort_keys=True).encode()
    expected = hmac.new(KEY_HASH.encode(), body,
                        hashlib.sha256).hexdigest()
    assert sent["headers"]["X-DataClean-Signature"] == f"sha256={expected}"
    assert sent["payload"]["drift_counts"] == {"new": 1, "resolved": 0,
                                               "changed": 0}

    # A failing webhook is recorded on the report, never raised.
    def boom(url, payload, headers):
        raise ConnectionError("no route")

    monkeypatch.setattr(sched_lib, "http_post_json", boom)
    sched2 = run(sched_lib.get_schedule(actor, lid))
    rep2 = run(sched_lib.run_scheduled_scan(actor, sched2))
    assert rep2["webhook_status"] == "failed"
    assert "no route" in rep2["webhook_error"]


# ------------------------------------------------------------------
# Sweep + cascade
# ------------------------------------------------------------------

def test_sweep_runs_due_schedules_only(env):
    fake, actor = env
    lid = _make_list(run, actor, [DUP_A, DUP_B])
    sched = _make_schedule(run, actor, lid, cadence="daily")["schedule"]
    # Fresh schedule is a day out: the sweep skips it.
    out = run(sched_lib.run_dataclean_list_scans())
    assert out == {"schedules_ran": 0, "schedules_failed": 0}
    _due(fake, sched["id"])
    out = run(sched_lib.run_dataclean_list_scans())
    assert out == {"schedules_ran": 1, "schedules_failed": 0}
    assert len(fake.dataclean_list_drift.docs) == 1


def test_delete_list_cascades_schedule_and_drift(env):
    fake, actor = env
    lid = _make_list(run, actor, [DUP_A, DUP_B])
    sched = _make_schedule(run, actor, lid, cadence="daily")["schedule"]
    run(sched_lib.run_scheduled_scan(actor, sched))
    assert len(fake.dataclean_list_drift.docs) == 1
    out = run(RJ.delete_list(lid, actor=actor))
    assert out["deleted"] is True
    assert out["removed"]["drift_reports"] == 1
    assert fake.dataclean_list_schedules.docs == []
    assert fake.dataclean_list_drift.docs == []
    assert fake.dataclean_list_records.docs == []
