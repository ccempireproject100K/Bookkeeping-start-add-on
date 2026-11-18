"""Job-costing #3 — field capture feeding job costs (owner-approved 2026-11-14).

Direct-call tests: no mongod, no network. Endpoint functions are awaited
directly with fake user dicts and an in-memory FakeDB monkeypatched into the
`db` attribute of every module under test. The REAL ledger runs against the
fake, so journal-entry balance/reversal behavior is exercised for real.
"""
import asyncio
import copy
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib.coa import ACCOUNT_TYPE_OF
from lib import audit as audit_lib
from lib import jobalerts as jobalerts_lib
from lib import jobcosting as jobcosting_lib
from routers import jobs as jobs_r
from routers import receipts as receipts_r
from routers import time as time_r

TID = "tenant-test"
TID2 = "tenant-other"
OWNER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
         "role": "owner", "tenant_id": TID}
TECH = {"id": "user-2", "email": "tech@test.com", "name": "Tech",
        "role": "technician", "tenant_id": TID}
OTHER = {"id": "user-9", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}


def run(coro):
    return asyncio.run(coro)


# ============================ FakeDB ============================
_MISSING = object()


def _matches(doc, query):
    for key, cond in (query or {}).items():
        val = doc.get(key, _MISSING)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if op == "$in":
                    if val is _MISSING or val not in operand:
                        return False
                elif op == "$ne":
                    if val is not _MISSING and val == operand:
                        return False
                elif op == "$gte":
                    if val is _MISSING or not val >= operand:
                        return False
                elif op == "$lte":
                    if val is _MISSING or not val <= operand:
                        return False
                elif op == "$exists":
                    if (val is not _MISSING) != bool(operand):
                        return False
                else:
                    raise NotImplementedError(f"FakeDB: operator {op}")
        else:
            if val is _MISSING or val != cond:
                return False
    return True


def _apply_update(doc, update):
    for op, fields in (update or {}).items():
        if op == "$set":
            for k, v in fields.items():
                doc[k] = copy.deepcopy(v)
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = (doc.get(k) or 0) + v
        elif op == "$unset":
            for k in fields:
                doc.pop(k, None)
        elif op == "$push":
            for k, v in fields.items():
                doc.setdefault(k, []).append(copy.deepcopy(v))
        else:
            raise NotImplementedError(f"FakeDB: update operator {op}")


def _project(doc, projection):
    if not projection:
        return copy.deepcopy(doc)
    exc = [k for k, v in projection.items() if not v]
    out = copy.deepcopy(doc)
    for k in exc:
        out.pop(k, None)
    return out


def _sort_key(v):
    return (v is None, v if isinstance(v, (str, int, float)) else str(v))


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        items = [spec] if isinstance(spec, tuple) else list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: _sort_key(d.get(field)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return _project(d, projection)
        return None

    def find(self, query=None, projection=None):
        return FakeCursor([_project(d, projection) for d in self.docs
                           if _matches(d, query)])

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", uuid.uuid4().hex)
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)
        return SimpleNamespace(inserted_ids=[])

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return SimpleNamespace(matched_count=0, modified_count=0)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, query, update):
        n = 0
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                n += 1
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def find_one_and_update(self, query, update, upsert=False,
                                  return_document=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return copy.deepcopy(d)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return copy.deepcopy(new)
        return None


class FakeDB:
    def __init__(self):
        object.__setattr__(self, "_colls", {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        colls = object.__getattribute__(self, "_colls")
        if name not in colls:
            colls[name] = FakeCollection(name)
        return colls[name]


# ============================ fixtures ============================
@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    for tid in (TID, TID2):
        fake.tenants.docs.append({"id": tid, "settings": {}})
        for code in ACCOUNT_TYPE_OF:
            fake.accounts.docs.append(
                {"code": code, "tenant_id": tid, "archived": False})
    fake.users.docs.append(
        {"id": TECH["id"], "tenant_id": TID, "name": "Tech",
         "pay_settings": {"hourly_rate_cents": 4000}})
    monkeypatch.setattr(time_r, "db", fake)
    monkeypatch.setattr(jobs_r, "db", fake)
    monkeypatch.setattr(receipts_r, "db", fake)
    monkeypatch.setattr(audit_lib, "db", fake)
    # Merged tree (2026-11-14): the manual cost path also resolves burden
    # templates (job-costing lane) and evaluates budget alerts (job-overrun
    # lane) through those lib modules' own db handles.
    monkeypatch.setattr(jobalerts_lib, "db", fake)
    monkeypatch.setattr(jobcosting_lib, "db", fake)
    from lib import jobphases as jobphases_lib, retainage as retainage_lib
    monkeypatch.setattr(jobphases_lib, "db", fake)
    monkeypatch.setattr(retainage_lib, "db", fake)
    from lib import jobforecast as jobforecast_lib  # merge-train: forecast hook in add_cost
    monkeypatch.setattr(jobforecast_lib, "db", fake)
    return fake


async def _make_job(user=OWNER, status="active"):
    job = await jobs_r.create_job(
        jobs_r.JobBody(customer_name="Acme HOA", estimated_revenue=1000,
                       estimated_costs={"labor": 500, "material": 200}), user)
    if status != "estimate":
        await jobs_r.update_status(job["id"], jobs_r.StatusBody(status=status), user)
        job["status"] = status
    return job


async def _job_cost_rows(fake, job_id, tid=TID):
    return [d for d in fake.job_costs.docs
            if d.get("job_id") == job_id and d.get("tenant_id") == tid]


# ============================ TIME -> JOB ============================
def test_linked_time_posts_to_job_actuals_exact(env):
    async def go():
        job = await _make_job()
        entry = await time_r.log_entry(time_r.ManualEntryBody(
            date="2026-11-14", hours=2.0, notes="Install condenser",
            job_id=job["id"], rate=50.0), TECH)
        assert entry["job_id"] == job["id"] and entry["rate_cents"] == 5000
        # Not yet: logged entries do not post
        assert await _job_cost_rows(env, job["id"]) == []
        await time_r.approve_entry(entry["id"], OWNER)
        rows = await _job_cost_rows(env, job["id"])
        assert len(rows) == 1
        row = rows[0]
        assert row["category"] == "labor" and row["source"] == "time_entry"
        assert row["amount_cents"] == 10000          # 2h x $50.00, exact
        assert row["hours"] == 2.0 and row["rate"] == 5000
        assert row["technician_id"] == TECH["id"]
        # Job profit sees it immediately (same read path as the job page)
        detail = await jobs_r.get_job(job["id"], OWNER)
        assert detail["profit"]["actual_by_category_cents"]["labor"] == 10000
        assert detail["field_capture"]["includes_today"] is True
        assert detail["field_capture"]["field_cost_count"] == 1
        # Exactly one balanced journal entry behind it (Dr 5005 / Cr 2320)
        lines = [d for d in env.journal_lines.docs
                 if d.get("entry_id") == row["journal_entry_id"]]
        assert sum(d["debit"] for d in lines) == sum(d["credit"] for d in lines) == 10000
        by_code = {d["account_code"]: d for d in lines}
        assert by_code["5005"]["debit"] == 10000 and by_code["2320"]["credit"] == 10000
        # Entry back-reference is set
        stored = await time_r.coll(TECH).find_one({"id": entry["id"]})
        assert stored["job_cost_id"] == row["id"]
        assert stored["job_cost_posted_cents"] == 10000
    run(go())


def test_unlinked_time_behaves_as_before(env):
    async def go():
        job = await _make_job()
        entry = await time_r.log_entry(time_r.ManualEntryBody(
            date="2026-11-14", hours=3.0, client_name="Walk-in"), TECH)
        assert entry["job_id"] is None and entry["job_cost_id"] is None
        await time_r.approve_entry(entry["id"], OWNER)
        assert await _job_cost_rows(env, job["id"]) == []
        assert len(env.journal_entries.docs) == 0
        stored = await time_r.coll(TECH).find_one({"id": entry["id"]})
        assert stored["status"] == "approved" and stored["job_cost_id"] is None
    run(go())


def test_clock_in_link_uses_pay_rate_default(env):
    async def go():
        job = await _make_job()
        entry = await time_r.clock_in(
            time_r.ClockInBody(notes="Rooftop unit", job_id=job["id"]), TECH)
        assert entry["job_id"] == job["id"]
        assert entry["rate_cents"] == 4000      # from pay_settings, not typed
        await time_r.clock_out(TECH)
        await time_r.update_entry(entry["id"],
                                  time_r.EntryPatchBody(hours=1.5), TECH)
        await time_r.approve_entry(entry["id"], OWNER)
        rows = await _job_cost_rows(env, job["id"])
        assert len(rows) == 1 and rows[0]["amount_cents"] == 6000   # 1.5h x $40
    run(go())


def test_edit_approved_entry_updates_line_never_duplicates(env):
    async def go():
        job = await _make_job()
        entry = await time_r.log_entry(time_r.ManualEntryBody(
            date="2026-11-14", hours=2.0, job_id=job["id"], rate=50.0), TECH)
        await time_r.approve_entry(entry["id"], OWNER)
        rows = await _job_cost_rows(env, job["id"])
        assert len(rows) == 1
        first_je = rows[0]["journal_entry_id"]

        # Edit hours 2 -> 3: line is updated in place (reverse + repost)
        await time_r.update_entry(entry["id"],
                                  time_r.EntryPatchBody(hours=3.0), OWNER)
        rows = await _job_cost_rows(env, job["id"])
        assert len(rows) == 1 and rows[0]["amount_cents"] == 15000
        old_je = [d for d in env.journal_entries.docs if d["id"] == first_je][0]
        assert old_je["status"] == "reversed"
        je_count = len(env.journal_entries.docs)

        # Re-saving the same values is a no-op — no new journal entry
        await time_r.update_entry(entry["id"],
                                  time_r.EntryPatchBody(hours=3.0), OWNER)
        assert len(env.journal_entries.docs) == je_count
        assert len(await _job_cost_rows(env, job["id"])) == 1

        # Unlinking removes the line and reverses its entry
        await time_r.update_entry(entry["id"],
                                  time_r.EntryPatchBody(clear_job=True), OWNER)
        assert await _job_cost_rows(env, job["id"]) == []
        stored = await time_r.coll(TECH).find_one({"id": entry["id"]})
        assert stored["job_id"] is None and stored["job_cost_id"] is None
        detail = await jobs_r.get_job(job["id"], OWNER)
        assert detail["profit"]["actual_cost_cents"] == 0
        assert detail["field_capture"] is None
    run(go())


def test_burden_seam_stores_burdened_alongside_base(env):
    async def go():
        # Burden lane's config shape (tenant.settings.burden_percent) present
        for d in env.tenants.docs:
            if d["id"] == TID:
                d["settings"] = {"burden_percent": 50.0}
        job = await _make_job()
        entry = await time_r.log_entry(time_r.ManualEntryBody(
            date="2026-11-14", hours=2.0, job_id=job["id"], rate=50.0), TECH)
        await time_r.approve_entry(entry["id"], OWNER)
        rows = await _job_cost_rows(env, job["id"])
        assert len(rows) == 1
        row = rows[0]
        assert row["amount_cents"] == 10000                 # base, untouched
        assert row["burden_percent"] == 50.0
        assert row["burdened_amount_cents"] == 15000        # stored alongside
        # GL still posts base only
        lines = [d for d in env.journal_lines.docs
                 if d.get("entry_id") == row["journal_entry_id"]]
        assert sum(d["debit"] for d in lines) == 10000
    run(go())


# ============================ RECEIPTS -> JOB ============================
def _seed_receipt(fake, rid="rcpt-1", tid=TID, job_id=None):
    fake.receipts.docs.append({
        "id": rid, "tenant_id": tid, "filename": "supply-house.jpg",
        "stored": f"{rid}.jpg", "content_type": "image/jpeg",
        "status": "scanned", "expense_id": None, "job_id": job_id,
        "extracted": {"vendor": "Supply House", "date": "2026-11-14",
                      "amount": 86.40, "tax": 0, "category_code": "5700",
                      "category_name": "Supplies & Office",
                      "description": "Copper fittings"},
        "created_at": "2026-11-14T15:00:00+00:00"})


def test_tagged_receipt_posts_amount_to_job(env):
    async def go():
        job = await _make_job()
        _seed_receipt(env)
        await receipts_r.tag_receipt_job(
            "rcpt-1", receipts_r.JobTagBody(job_id=job["id"]), OWNER)
        je_before = len(env.journal_entries.docs)
        exp = await receipts_r.post_receipt("rcpt-1", receipts_r.PostBody(
            vendor="Supply House", category_code="5700", amount=86.40,
            description="Copper fittings"), OWNER)
        assert exp["job_id"] == job["id"]
        rows = await _job_cost_rows(env, job["id"])
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "receipt" and row["receipt_id"] == "rcpt-1"
        assert row["category"] == "material"        # 5700 Supplies -> material
        assert row["amount_cents"] == 8640
        # The job cost rides the expense's ONE journal entry — no double count
        assert row["journal_entry_id"] == exp_entry_id(env, exp)
        assert len(env.journal_entries.docs) == je_before + 1
        detail = await jobs_r.get_job(job["id"], OWNER)
        assert detail["profit"]["actual_by_category_cents"]["material"] == 8640
    run(go())


def exp_entry_id(fake, exp):
    rows = [d for d in fake.journal_entries.docs
            if d.get("source_ref") == exp["id"]]
    assert len(rows) == 1
    return rows[0]["id"]


def test_untagged_receipt_behaves_as_before(env):
    async def go():
        job = await _make_job()
        _seed_receipt(env)
        exp = await receipts_r.post_receipt("rcpt-1", receipts_r.PostBody(
            vendor="Supply House", category_code="5700", amount=86.40), OWNER)
        assert exp["job_id"] is None
        assert await _job_cost_rows(env, job["id"]) == []
        assert len(env.expenses.docs) == 1
    run(go())


def test_receipt_post_with_inline_job_and_category_mapping(env):
    async def go():
        job = await _make_job()
        _seed_receipt(env, rid="rcpt-2")
        # job passed at post time (review screen), equipment account 5006
        await receipts_r.post_receipt("rcpt-2", receipts_r.PostBody(
            vendor="Tool Rental", category_code="5006", amount=120.0,
            job_id=job["id"]), OWNER)
        rows = await _job_cost_rows(env, job["id"])
        assert len(rows) == 1 and rows[0]["category"] == "equipment"
        assert rows[0]["amount_cents"] == 12000
    run(go())


def test_receipt_category_mapping_rules():
    f = receipts_r.job_category_for_receipt
    assert f("5000") == "material" and f("5005") == "labor"
    assert f("5006") == "equipment" and f("5007") == "subcontractor"
    assert f("5700", "Supplies & Office") == "material"
    assert f("5990", "Other / Miscellaneous") == "other"
    assert f("5800", "Marketing") == "other"


def test_upload_with_job_tag(env, monkeypatch, tmp_path):
    monkeypatch.setattr(receipts_r, "UPLOAD_ROOT", tmp_path)

    class StubFile:
        content_type = "image/png"
        filename = "dash-receipt.png"

        async def read(self):
            return b"\x89PNG-fake-bytes"

    async def go():
        job = await _make_job()
        doc = await receipts_r.upload_receipt(StubFile(), job_id=job["id"], user=OWNER)
        assert doc["job_id"] == job["id"] and doc["status"] == "uploaded"
        stored = await receipts_r.receipts(OWNER).find_one({"id": doc["id"]})
        assert stored["job_id"] == job["id"]
        # Bad job id is rejected before anything is stored
        with pytest.raises(HTTPException) as ei:
            await receipts_r.upload_receipt(StubFile(), job_id="nope", user=OWNER)
        assert ei.value.status_code == 404
        # No tag -> exactly today's behavior (job_id passed explicitly: the
        # Form(None) default only resolves to None under real FastAPI serving)
        doc2 = await receipts_r.upload_receipt(StubFile(), job_id=None, user=OWNER)
        assert doc2["job_id"] is None
    run(go())


# ============================ ISOLATION & REGRESSION ============================
def test_tenant_isolation(env):
    async def go():
        job = await _make_job()   # tenant 1's job
        # Tenant 2 cannot link tenant 1's job on a time entry
        with pytest.raises(HTTPException) as ei:
            await time_r.log_entry(time_r.ManualEntryBody(
                date="2026-11-14", hours=1.0, job_id=job["id"], rate=10.0), OTHER)
        assert ei.value.status_code == 404
        # Tenant 2 cannot tag a receipt to it either
        _seed_receipt(env, rid="rcpt-x", tid=TID2)
        with pytest.raises(HTTPException) as ei:
            await receipts_r.tag_receipt_job(
                "rcpt-x", receipts_r.JobTagBody(job_id=job["id"]), OTHER)
        assert ei.value.status_code == 404
        # Tenant 2 cannot see or edit tenant 1's entries
        entry = await time_r.log_entry(time_r.ManualEntryBody(
            date="2026-11-14", hours=1.0, job_id=job["id"], rate=10.0), TECH)
        with pytest.raises(HTTPException) as ei:
            await time_r.update_entry(entry["id"],
                                      time_r.EntryPatchBody(hours=9.0), OTHER)
        assert ei.value.status_code == 404
        assert await _job_cost_rows(env, job["id"], tid=TID2) == []
    run(go())


def test_manual_job_cost_path_unchanged(env):
    async def go():
        est = await _make_job(status="estimate")
        with pytest.raises(HTTPException) as ei:
            await jobs_r.add_cost(est["id"], jobs_r.CostBody(
                category="labor", hours=1, rate=10), OWNER)
        assert ei.value.status_code == 409     # estimate guard intact
        job = await _make_job()
        doc = await jobs_r.add_cost(job["id"], jobs_r.CostBody(
            category="labor", description="Office-keyed", hours=2, rate=25), OWNER)
        assert doc["amount_cents"] == 5000 and doc["source"] == "manual"
        detail = await jobs_r.get_job(job["id"], OWNER)
        assert detail["profit"]["actual_by_category_cents"]["labor"] == 5000
        assert detail["field_capture"] is None   # manual costs aren't field capture
    run(go())
