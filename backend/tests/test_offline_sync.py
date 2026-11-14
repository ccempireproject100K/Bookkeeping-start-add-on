"""Offline field capture — server-side idempotency (feat/offline-field-capture).

Direct-call tests: no mongod, no network. Endpoint functions are awaited
directly with a fake user dict and an in-memory FakeDB monkeypatched into
the `db` attribute of the modules under test. The one posting seam
(ledger.post_journal_entry for vendor expenses) is faked; everything else
— the client_key dedupe in time entries, clock in/out, receipt upload and
vendor expenses — runs for real.

The contract under test: the PWA queue sends each captured item with a
stable client_key; if the response is lost and the item is sent again,
the server returns the record it already created. One item, one record.
"""
import asyncio
import copy
import os
import sys
import uuid
from io import BytesIO
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException, UploadFile

from lib import audit as audit_lib
from routers import receipts, time, vendors

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "tech@test.com", "name": "Tech",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
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

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))


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
def env(monkeypatch, tmp_path):
    fake = FakeDB()
    monkeypatch.setattr(time, "db", fake)
    monkeypatch.setattr(receipts, "db", fake)
    monkeypatch.setattr(vendors, "db", fake)
    monkeypatch.setattr(audit_lib, "db", fake)
    # Merge-train (2026-11-14): main moved receipt bytes behind
    # lib/receipt_storage (local disk or R2); point its cached store at
    # the tmp root instead of the old UPLOAD_ROOT module attribute.
    from lib import receipt_storage
    monkeypatch.setattr(receipt_storage, "_cached",
                        receipt_storage.LocalStorage(tmp_path / "receipts"))

    journal_calls = []

    async def fake_post_journal_entry(db_, tenant_id, user, draft, status="posted",
                                      idempotency_key=None):
        journal_calls.append(draft)
        return {"number": f"JE-{len(journal_calls)}", "id": uuid.uuid4().hex}

    monkeypatch.setattr(vendors.L, "post_journal_entry", fake_post_journal_entry)
    env_obj = SimpleNamespace(db=fake, journal_calls=journal_calls, tmp=tmp_path)
    return env_obj


def manual_body(**kw):
    base = {"date": "2026-11-14", "hours": 2.0, "notes": "", "client_name": "",
            "billable": True}
    base.update(kw)
    return time.ManualEntryBody(**base)


# ============================ time entries ============================
def test_manual_entry_sync_twice_creates_one_record(env):
    body = manual_body(client_key="fc_item_1")
    first = run(time.log_entry(body, USER))
    second = run(time.log_entry(body, USER))  # the retry after a lost response
    assert first["id"] == second["id"]
    assert len(env.db.time_entries.docs) == 1


def test_manual_entry_without_key_still_creates_each_time(env):
    run(time.log_entry(manual_body(), USER))
    run(time.log_entry(manual_body(), USER))
    assert len(env.db.time_entries.docs) == 2


def test_manual_entry_keeps_the_job_link(env):
    # Merge-train (2026-11-14): the merged tree validates job links at
    # entry time (main + field-capture lanes, 404 on unknown job) — seed
    # the job this entry links to.
    env.db.jobs.docs.append({"id": "job-9", "tenant_id": USER["tenant_id"],
                             "job_number": "J-9", "customer_name": "Ada"})
    out = run(time.log_entry(manual_body(client_key="fc_job", job_id="job-9"), USER))
    assert out["job_id"] == "job-9"
    assert env.db.time_entries.docs[0]["job_id"] == "job-9"


def test_client_key_is_scoped_per_tenant(env):
    run(time.log_entry(manual_body(client_key="fc_shared"), USER))
    run(time.log_entry(manual_body(client_key="fc_shared"), USER2))
    assert len(env.db.time_entries.docs) == 2


# ============================ clock in / out ============================
def test_clock_in_sync_twice_is_one_open_shift(env):
    body = time.ClockInBody(notes="", client_name="", client_key="fc_in_1")
    first = run(time.clock_in(body, USER))
    second = run(time.clock_in(body, USER))
    assert first["id"] == second["id"]
    assert len(env.db.time_entries.docs) == 1


def test_clock_in_conflict_with_existing_shift_is_a_409(env):
    run(time.clock_in(time.ClockInBody(notes="office"), USER))  # no key — the office clock-in
    with pytest.raises(HTTPException) as ei:
        run(time.clock_in(time.ClockInBody(notes="phone", client_key="fc_in_2"), USER))
    assert ei.value.status_code == 409
    assert len(env.db.time_entries.docs) == 1  # server shift untouched


def test_clock_out_sync_twice_closes_once(env):
    run(time.clock_in(time.ClockInBody(client_key="fc_in_3"), USER))
    out_body = time.ClockOutBody(client_key="fc_out_3")
    first = run(time.clock_out(out_body, USER))
    second = run(time.clock_out(out_body, USER))  # retry — must not 404 or double-close
    assert first["id"] == second["id"]
    assert second["status"] == "logged"
    assert len(env.db.time_entries.docs) == 1


def test_clock_out_with_a_fresh_key_and_no_shift_is_a_404(env):
    with pytest.raises(HTTPException) as ei:
        run(time.clock_out(time.ClockOutBody(client_key="fc_out_none"), USER))
    assert ei.value.status_code == 404


# ============================ receipts ============================
def _upload(name="receipt.jpg", data=b"\xff\xd8\xff\xe0jpeg-bytes"):
    return UploadFile(filename=name, file=BytesIO(data),
                      headers={"content-type": "image/jpeg"})


def test_receipt_upload_sync_twice_creates_one_receipt(env):
    first = run(receipts.upload_receipt(_upload(), client_key="fc_rcpt_1", user=USER))
    second = run(receipts.upload_receipt(_upload(), client_key="fc_rcpt_1", user=USER))
    assert first["id"] == second["id"]
    assert len(env.db.receipts.docs) == 1
    files = list((env.tmp / "receipts").rglob("*.jpg"))
    assert len(files) == 1  # the retry didn't even write a second file


def test_receipt_upload_without_key_creates_each_time(env):
    run(receipts.upload_receipt(_upload(), user=USER))
    run(receipts.upload_receipt(_upload(), user=USER))
    assert len(env.db.receipts.docs) == 2


# ============================ vendor expenses ============================
def expense_body(**kw):
    base = {"vendor": "Supply House", "amount": 42.50, "description": "tape",
            "category_code": "5700"}
    base.update(kw)
    return vendors.ExpenseBody(**base)


def test_expense_sync_twice_posts_one_journal_entry(env):
    body = expense_body(client_key="fc_exp_1")
    first = run(vendors.create_expense(body, USER))
    second = run(vendors.create_expense(body, USER))
    assert first["id"] == second["id"]
    assert len(env.db.expenses.docs) == 1
    assert len(env.journal_calls) == 1  # the GL was hit exactly once


def test_expense_client_key_scoped_per_tenant(env):
    run(vendors.create_expense(expense_body(client_key="fc_exp_2"), USER))
    run(vendors.create_expense(expense_body(client_key="fc_exp_2"), USER2))
    assert len(env.db.expenses.docs) == 2
    assert len(env.journal_calls) == 2
