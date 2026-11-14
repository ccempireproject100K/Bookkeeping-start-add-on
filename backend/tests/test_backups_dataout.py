"""Backups & data-out (Top-20 #20): full export, validated restore,
scheduled backups with retention, restore test, tenant isolation, and the
legacy backup + CPA paths untouched.

Direct-call tests: no mongod, no network. Router endpoints are awaited
directly with a fake user dict and an in-memory FakeDB monkeypatched into
the router + audit modules; lib/fullexport takes the db as a parameter.
"""
import asyncio
import copy
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from lib import fullexport as FX
from routers import dataout
import lib.audit as audit_mod

TID = "tenant-a"
TID2 = "tenant-b"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Olivia Owner",
        "role": "owner", "tenant_id": TID,
        "memberships": [{"tenant_id": TID, "role": "owner"}]}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other Owner",
         "role": "owner", "tenant_id": TID2,
         "memberships": [{"tenant_id": TID2, "role": "owner"}]}


def run(coro):
    return asyncio.run(coro)


# ============================ FakeDB ============================

def _matches(doc, query):
    for key, cond in (query or {}).items():
        if doc.get(key) != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return copy.deepcopy(self._docs[:length] if length else self._docs)


class _Result:
    def __init__(self, n):
        self.deleted_count = n
        self.modified_count = n


class FakeCollection:
    def __init__(self):
        self.docs = []

    def find(self, query=None, projection=None):
        return FakeCursor([d for d in self.docs if _matches(d, query)])

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return copy.deepcopy(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))
        return _Result(1)

    async def insert_many(self, docs):
        self.docs.extend(copy.deepcopy(list(docs)))
        return _Result(len(docs))

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, query)]
        return _Result(before - len(self.docs))

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return _Result(1)
        return _Result(0)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                return _Result(1)
        if upsert:
            doc = dict(query)
            doc.update(update.get("$set", {}))
            self.docs.append(doc)
            return _Result(1)
        return _Result(0)

    async def count_documents(self, query):
        return len([d for d in self.docs if _matches(d, query)])


class FakeDB:
    def __init__(self):
        self._cols = {}

    def __getitem__(self, name):
        return self._cols.setdefault(name, FakeCollection())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


# ============================ Seeding ============================

def seed(db, tmp_path):
    """Workspace A (rich) + workspace B (one customer, one entry)."""
    db.tenants.docs.append({"id": TID, "name": "Acme Plumbing",
                            "niche_id": "plumbing", "currency": "USD"})
    db.tenants.docs.append({"id": TID2, "name": "Beta Bakery",
                            "niche_id": "bakery", "currency": "USD"})
    db.accounts.docs += [
        {"id": "acc-1", "tenant_id": TID, "code": "1010", "name": "Checking", "type": "asset"},
        {"id": "acc-2", "tenant_id": TID, "code": "4010", "name": "Sales", "type": "revenue"},
    ]
    entry = {"id": "je-1", "tenant_id": TID, "number": "JE-1",
             "entry_date": "2026-11-01", "status": "posted", "memo": "sale",
             "lines": [{"account_code": "1010", "debit_cents": 10000, "credit_cents": 0},
                       {"account_code": "4010", "debit_cents": 0, "credit_cents": 10000}]}
    draft = {"id": "je-2", "tenant_id": TID, "number": "JE-2",
             "entry_date": "2026-11-02", "status": "draft", "memo": "wip",
             "lines": [{"account_code": "1010", "debit_cents": 500, "credit_cents": 0}]}
    db.journal_entries.docs += [entry, draft]
    db.journal_lines.docs += [
        {"id": "jl-1", "tenant_id": TID, "entry_id": "je-1", "account_code": "1010",
         "debit_cents": 10000, "credit_cents": 0},
        {"id": "jl-2", "tenant_id": TID, "entry_id": "je-1", "account_code": "4010",
         "debit_cents": 0, "credit_cents": 10000},
    ]
    db.customers.docs.append({"id": "cust-a", "tenant_id": TID, "name": "Ann A"})
    db.customers.docs.append({"id": "cust-b", "tenant_id": TID2, "name": "Bob B"})
    db.vendors.docs.append({"id": "ven-a", "tenant_id": TID, "name": "Supply Co", "is_1099": True})
    db.products.docs.append({"id": "prod-a", "tenant_id": TID, "name": "Widget"})
    db.invoices.docs.append({"id": "inv-a", "tenant_id": TID, "client_name": "Ann A",
                             "issue_date": "2026-11-03", "total_cents": 10000,
                             "paid_cents": 0, "status": "open"})
    db.bills.docs.append({"id": "bill-a", "tenant_id": TID, "vendor": "Supply Co",
                          "total_cents": 2500, "status": "open"})
    db.expenses.docs.append({"id": "exp-a", "tenant_id": TID, "vendor": "Gas",
                             "expense_date": "2026-11-04", "amount_cents": 4200})
    db.jobs.docs.append({"id": "job-a", "tenant_id": TID, "name": "Roof job"})
    db.mileage_trips.docs.append({"id": "mi-a", "tenant_id": TID, "trip_date": "2026-11-05",
                                  "miles": 12.5, "purpose": "site visit"})
    db.contractor_payments.docs.append({"id": "cp-a", "tenant_id": TID,
                                        "vendor_id": "ven-a", "amount_cents": 70000,
                                        "pay_date": "2026-11-06"})
    # Receipts: one with a real file on disk, one whose file is gone.
    rdir = tmp_path / "receipts" / TID
    rdir.mkdir(parents=True)
    (rdir / "r-1.jpg").write_bytes(b"\xff\xd8fake-jpeg-bytes")
    db.receipts.docs.append({"id": "r-1", "tenant_id": TID, "stored": "r-1.jpg",
                             "filename": "r-1.jpg", "status": "posted"})
    db.receipts.docs.append({"id": "r-2", "tenant_id": TID, "stored": "gone.pdf",
                             "filename": "gone.pdf", "status": "uploaded"})
    db.audit_log.docs += [
        {"id": "au-1", "tenant_id": TID, "action": "invoice.create", "actor_name": "Olivia"},
        {"id": "au-2", "tenant_id": TID, "action": "bill.pay", "actor_name": "Olivia"},
    ]
    db.journal_entries.docs.append(
        {"id": "je-b", "tenant_id": TID2, "number": "JE-B",
         "entry_date": "2026-11-01", "status": "posted", "memo": "b sale",
         "lines": [{"account_code": "1010", "debit_cents": 300, "credit_cents": 0},
                   {"account_code": "4010", "debit_cents": 0, "credit_cents": 300}]})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(FX, "RECEIPTS_UPLOAD_ROOT", tmp_path / "receipts")
    monkeypatch.setattr(FX, "BACKUPS_ROOT", tmp_path / "backups")
    monkeypatch.setattr(dataout, "db", db)
    monkeypatch.setattr(audit_mod, "db", db)
    seed(db, tmp_path)
    return db


def _upload(zip_bytes, name="export.zip"):
    return UploadFile(filename=name, file=io.BytesIO(zip_bytes))


# ============================ Export ============================

def test_full_export_completeness(env):
    zip_bytes, manifest = run(FX.build_full_export(env, TID))
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = set(zf.namelist())
    assert "manifest.json" in names and "README.md" in names
    assert "settings.json" in names
    for coll in FX.FULL_COLLECTIONS:
        assert f"data/{coll}.json" in names, coll
        assert f"csv/{coll}.csv" in names, coll
    assert json.loads(zf.read("settings.json"))["name"] == "Acme Plumbing"
    # Record fidelity
    assert len(json.loads(zf.read("data/customers.json"))) == 1
    assert len(json.loads(zf.read("data/audit_log.json"))) == 2
    assert len(json.loads(zf.read("data/journal_entries.json"))) == 2
    # Attachments: real file in, missing file honestly reported
    assert zf.read("attachments/receipts/r-1.jpg") == b"\xff\xd8fake-jpeg-bytes"
    assert manifest["attachments"]["count"] == 1
    assert manifest["attachments"]["missing_files"] == ["gone.pdf"]
    # Manifest anchor: posted entries only, balanced
    assert manifest["journal"] == {"posted_entries": 1, "debit_cents": 10000,
                                   "credit_cents": 10000, "balanced": True}
    assert manifest["collections"]["invoices"] == 1
    assert manifest["documents"] == sum(manifest["collections"].values())
    # Owned format: the README says the data works without us
    readme = zf.read("README.md").decode()
    assert "You do NOT need Bookkeeping" in readme
    assert "manifest" in zf.read("README.md").decode().lower()
    # Reports views ride along
    assert "reports/trial_balance.csv" in names
    assert "reports/general_ledger.csv" in names


def test_export_tenant_isolation(env):
    zip_bytes, _ = run(FX.build_full_export(env, TID))
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    blob = b"".join(zf.read(n) for n in zf.namelist()
                    if n.startswith("data/"))
    assert b"cust-b" not in blob and b"Bob B" not in blob and b"je-b" not in blob


def test_export_summary(env):
    s = run(FX.export_summary(env, TID))
    assert s["documents"] > 10
    assert s["attachments"] == {"count": 1, "bytes": 17}
    assert s["collections"]["customers"] == 1


# ============================ Validation refusals ============================

def _tampered(zip_bytes, fn):
    zin = zipfile.ZipFile(io.BytesIO(zip_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in zin.namelist():
            content = zin.read(n)
            content = fn(n, content)
            z.writestr(n, content)
    return buf.getvalue()


def test_validate_refuses_tampered_counts(env):
    zip_bytes, _ = run(FX.build_full_export(env, TID))

    def bump(n, c):
        if n == "manifest.json":
            m = json.loads(c)
            m["collections"]["invoices"] = 99
            return json.dumps(m).encode()
        return c
    ok, errors, *_ = FX.parse_and_validate(_tampered(zip_bytes, bump))
    assert not ok and any("invoices" in e for e in errors)


def test_validate_refuses_tampered_journal(env):
    zip_bytes, _ = run(FX.build_full_export(env, TID))

    def bump(n, c):
        if n == "manifest.json":
            m = json.loads(c)
            m["journal"]["debit_cents"] += 1
            return json.dumps(m).encode()
        return c
    ok, errors, *_ = FX.parse_and_validate(_tampered(zip_bytes, bump))
    assert not ok and any("journal" in e for e in errors)


def test_validate_refuses_version_and_garbage(env):
    zip_bytes, _ = run(FX.build_full_export(env, TID))

    def bump(n, c):
        if n == "manifest.json":
            m = json.loads(c)
            m["version"] = 99
            return json.dumps(m).encode()
        return c
    ok, errors, *_ = FX.parse_and_validate(_tampered(zip_bytes, bump))
    assert not ok and any("version 99" in e for e in errors)
    ok, errors, *_ = FX.parse_and_validate(b"not a zip at all")
    assert not ok
    ok, errors, m, *_ = FX.parse_and_validate(zip_bytes, expect_tenant_id=TID2)
    assert not ok and any("different workspace" in e for e in errors)


def test_restore_refusal_changes_nothing(env):
    zip_bytes, _ = run(FX.build_full_export(env, TID2))
    before = run(FX.export_summary(env, TID))
    with pytest.raises(HTTPException) as ei:
        run(dataout.restore(file=_upload(zip_bytes), target_tenant_id=None, confirm_text=None, user=USER))
    assert ei.value.status_code in (403, 422)
    assert run(FX.export_summary(env, TID)) == before


# ============================ Restore ============================

def test_restore_round_trip(env, tmp_path):
    zip_bytes, manifest = run(FX.build_full_export(env, TID))
    # Disaster: wipe workspace A entirely, files included.
    run(FX.wipe_tenant(env, TID))
    assert run(FX._tenant_doc_count(env, TID)) == 0
    r = run(dataout.restore(file=_upload(zip_bytes), target_tenant_id=None, confirm_text=None, user=USER))
    assert r["ok"] is True
    assert r["verification"]["documents"] == manifest["documents"]
    assert r["verification"]["journal"]["debit_cents"] == 10000
    ver = run(FX.verify_against_manifest(env, TID, manifest))
    assert ver["ok"], ver["problems"]
    # Attachment file is back on disk
    assert (tmp_path / "receipts" / TID / "r-1.jpg").read_bytes() == b"\xff\xd8fake-jpeg-bytes"
    # Tenant B untouched by A's restore
    assert [d["id"] for d in env.customers.docs if d["tenant_id"] == TID2] == ["cust-b"]


def test_restore_over_nonempty_needs_typed_name(env):
    zip_bytes, _ = run(FX.build_full_export(env, TID))
    with pytest.raises(HTTPException) as ei:
        run(dataout.restore(file=_upload(zip_bytes), target_tenant_id=None, confirm_text=None, user=USER))
    assert ei.value.status_code == 422 and "Acme Plumbing" in ei.value.detail
    with pytest.raises(HTTPException):
        run(dataout.restore(file=_upload(zip_bytes), target_tenant_id=None, confirm_text="Wrong Name",
                            user=USER))
    r = run(dataout.restore(file=_upload(zip_bytes), target_tenant_id=None,
                            confirm_text="Acme Plumbing", user=USER))
    assert r["ok"] is True


def test_restore_into_foreign_workspace_refused(env):
    zip_bytes, _ = run(FX.build_full_export(env, TID))
    with pytest.raises(HTTPException) as ei:
        run(dataout.restore(file=_upload(zip_bytes), target_tenant_id=TID2,
                            confirm_text="Beta Bakery", user=USER))
    assert ei.value.status_code == 403


def test_validate_endpoint_is_dry_run(env):
    zip_bytes, _ = run(FX.build_full_export(env, TID))
    before = run(FX._tenant_doc_count(env, TID))
    r = run(dataout.validate_restore(file=_upload(zip_bytes), user=USER))
    assert r["ok"] is True and r["manifest"]["documents"] > 10
    assert run(FX._tenant_doc_count(env, TID)) == before


# ============================ Scheduled backups ============================

def test_schedule_default_off_then_due(env):
    assert run(FX.maybe_run_scheduled(env, TID)) is None  # default OFF
    run(dataout.put_settings(dataout.ScheduleBody(enabled=True, cadence_days=7,
                                                  keep=8), user=USER))
    # Enabling runs the first one straight away (never ran before)
    assert len(env.full_backups.docs) == 1
    assert env.full_backups.docs[0]["kind"] == "scheduled"
    # Not due again immediately
    assert run(FX.maybe_run_scheduled(env, TID)) is None
    assert len(env.full_backups.docs) == 1


def test_retention_pruning(env, tmp_path):
    env.backup_settings.docs.append(
        {"tenant_id": TID, "enabled": False, "cadence_days": 7, "keep": 3,
         "last_run_at": None})
    for _ in range(5):
        run(FX.create_stored_backup(env, TID, kind="manual"))
    kept = [d for d in env.full_backups.docs if d["tenant_id"] == TID]
    assert len(kept) == 3
    files = list((tmp_path / "backups" / TID).glob("*.zip"))
    assert len(files) == 3  # pruned files are really gone


def test_restore_test_passes_and_scratch_discarded(env):
    doc = run(FX.create_stored_backup(env, TID, kind="manual"))
    result = run(dataout.test_backup(doc["id"], user=USER))
    assert result["passed"] is True
    assert result["documents"] > 10
    # Scratch workspace fully discarded
    leftovers = 0
    for name in FX.FULL_COLLECTIONS:
        leftovers += len([d for d in env[name].docs
                          if str(d.get("tenant_id", "")).startswith("__restore_test__")])
    assert leftovers == 0
    saved = [d for d in env.full_backups.docs if d["id"] == doc["id"]][0]
    assert saved["last_test"]["passed"] is True
    assert saved["last_test"]["at"]


def test_backup_now_and_download(env):
    doc = run(dataout.backup_now(user=USER))
    assert doc["kind"] == "manual" and doc["documents"] > 10
    resp = run(dataout.download_backup(doc["id"], user=USER))
    ok, errors, manifest, *_ = FX.parse_and_validate(resp.body,
                                                     expect_tenant_id=TID)
    assert ok, errors
    with pytest.raises(HTTPException) as ei:
        run(dataout.download_backup(doc["id"], user=USER2))
    assert ei.value.status_code == 404


# ============================ Legacy paths untouched ============================

def test_legacy_backup_export_still_works(env, monkeypatch):
    from routers import backup as legacy
    monkeypatch.setattr(legacy, "db", env)
    resp = run(legacy.export_backup(encrypted=False, offsite=False, user=USER))
    dump = json.loads(resp.body)
    assert dump["format"] == "bookkeeping-backup"
    assert any(d["id"] == "cust-a" for d in dump["collections"]["customers"])
    assert not any(d["id"] == "cust-b" for d in dump["collections"]["customers"])
