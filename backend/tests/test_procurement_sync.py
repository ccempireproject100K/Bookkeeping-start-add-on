"""Procurement sync tests (Addendum 2026-11-05, INTEGRATION_CONTRACT v1.1):
POST /integrations/procurement/bills — AI Office supplier bills land as
open AP bills (one GL entry), job-linked lines feed job costing off
that same entry, vendors/jobs resolve or are created once.

Same harness as tests/test_integration_sync.py: an in-memory fake db
(with unique-index emulation) is monkeypatched into every module that
holds a `db` reference; the real endpoint, ledger posting path, and
auth dependencies run against it.

Run from the backend directory with:
    python -m pytest tests/test_procurement_sync.py -n 0
"""
import asyncio
import copy
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_procurement_sync")
os.environ.setdefault("JWT_SECRET", "procurement-sync-test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

import lib.audit as audit_mod  # noqa: E402
import lib.auth as auth_mod  # noqa: E402
import lib.idempotency as idem  # noqa: E402
import lib.integration_auth as iauth  # noqa: E402
import routers.integrations as integrations_r  # noqa: E402
import routers.jobs as jobs_r  # noqa: E402
import routers.procurement_sync as proc_r  # noqa: E402

T1 = "tenant-1"
T2 = "tenant-2"
PATH = "/api/integrations/procurement/bills"


# ---------------- fake in-memory db ----------------
def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict) and any(str(kk).startswith("$") for kk in v):
            actual = doc.get(k)
            for op, operand in v.items():
                if op == "$exists" and (actual is not None) != bool(operand) \
                        and (k in doc) != bool(operand):
                    return False
                if op == "$gte" and not (actual is not None and actual >= operand):
                    return False
                if op == "$lte" and not (actual is not None and actual <= operand):
                    return False
                if op == "$ne" and actual == operand:
                    return False
                if op == "$in" and actual not in operand:
                    return False
            continue
        if doc.get(k) != v:
            return False
    return True


def _project(doc, projection):
    if not projection:
        return doc
    inclusion = any(v == 1 for k, v in projection.items() if k != "_id")
    if inclusion:
        out = {k: doc[k] for k, v in projection.items()
               if v == 1 and k in doc}
        if projection.get("_id", 1) and "_id" in doc:
            out["_id"] = doc["_id"]
        return out
    out = dict(doc)
    for k, v in projection.items():
        if v == 0:
            out.pop(k, None)
    return out


class _Result:
    def __init__(self, matched=0, modified=0):
        self.matched_count = matched
        self.modified_count = modified
        self.inserted_id = None
        self.deleted_count = matched


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        pairs = key if isinstance(key, list) else [(key, direction)]
        for k, d in reversed(pairs):
            self._docs = sorted(
                self._docs,
                key=lambda doc: (doc.get(k) is None, str(doc.get(k))),
                reverse=(d < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)


class FakeCollection:
    def __init__(self, unique_keys=()):
        self.docs = []
        self.unique_keys = [tuple(u) for u in unique_keys]
        self._seq = 0

    def _check_unique(self, doc, ignore=None):
        for keys in self.unique_keys:
            if any(doc.get(k) is None for k in keys):
                continue
            for existing in self.docs:
                if existing is ignore:
                    continue
                if all(existing.get(k) == doc.get(k) for k in keys):
                    raise DuplicateKeyError(f"unique index violation on {keys}")

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        self._check_unique(doc)
        self._seq += 1
        doc.setdefault("_id", f"oid-{self._seq}-{id(self)}")
        self.docs.append(doc)
        res = _Result(0, 0)
        res.inserted_id = doc["_id"]
        return res

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                return _project(copy.deepcopy(d), projection)
        return None

    def find(self, q=None, projection=None):
        return FakeCursor([_project(copy.deepcopy(d), projection)
                           for d in self.docs if _matches(d, q)])

    def _apply(self, doc, update):
        for op, fields in update.items():
            if op == "$set":
                for k, v in fields.items():
                    doc[k] = copy.deepcopy(v)
            elif op == "$inc":
                for k, v in fields.items():
                    doc[k] = doc.get(k, 0) + v
            elif op == "$push":
                for k, v in fields.items():
                    doc.setdefault(k, []).append(copy.deepcopy(v))
            elif op == "$unset":
                for k in fields:
                    doc.pop(k, None)
            else:
                doc.update(copy.deepcopy(update))
                return

    async def update_one(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                return _Result(1, 1)
        return _Result(0, 0)

    async def update_many(self, q, update):
        n = 0
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                n += 1
        return _Result(n, n)

    async def find_one_and_update(self, q, update, upsert=False,
                                  return_document=False, **kw):
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                return copy.deepcopy(d)
        if upsert:
            doc = dict(q or {})
            self._apply(doc, update)
            self._seq += 1
            doc.setdefault("_id", f"oid-{self._seq}-{id(self)}")
            self.docs.append(doc)
            return copy.deepcopy(doc)
        return None

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q))

    async def delete_one(self, q=None):
        for i, d in enumerate(self.docs):
            if _matches(d, q):
                del self.docs[i]
                return _Result(1, 1)
        return _Result(0, 0)


class FakeDB:
    def __init__(self):
        self._colls = {
            "idempotency_records": FakeCollection(
                unique_keys=[("tenant_id", "key")]),
            "integration_credentials": FakeCollection(
                unique_keys=[("client_id",)]),
            "jobs": FakeCollection(
                unique_keys=[("tenant_id", "job_number")]),
        }

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())


# ---------------- fixtures / helpers ----------------
ACCOUNT_CODES = ["2010", "5000", "5005", "5006", "5007", "5008", "5700"]


def make_user(tenant_id, role="owner", email=None, uid=None):
    return {
        "id": uid or f"user-{tenant_id}",
        "email": email or f"owner@{tenant_id}.example",
        "name": "Owner",
        "role": role,
        "tenant_id": tenant_id,
        "token_version": 0,
    }


@pytest.fixture()
def fdb(monkeypatch):
    fake = FakeDB()
    for tenant in (T1, T2):
        fake._colls.setdefault("accounts", FakeCollection()).docs.extend(
            {"id": f"acct-{tenant}-{c}", "tenant_id": tenant, "code": c,
             "archived": False} for c in ACCOUNT_CODES)
        fake._colls.setdefault("tenants", FakeCollection()).docs.append(
            {"id": tenant, "name": f"Shop {tenant}", "settings": {}})
        fake._colls.setdefault("users", FakeCollection()).docs.append(
            make_user(tenant))
    for mod in (proc_r, jobs_r, integrations_r, idem, iauth,
                auth_mod, audit_mod):
        monkeypatch.setattr(mod, "db", fake)
    return fake


def run(coro):
    return asyncio.run(coro)


def req(headers=None, path=PATH, cookies=None):
    return SimpleNamespace(headers=headers or {}, cookies=cookies or {},
                           url=SimpleNamespace(path=path))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def issue_credential(fdb, tenant=T1, label="AI Office sync"):
    async def _go():
        return await integrations_r.issue_credential(
            integrations_r.IssueBody(label=label), user=make_user(tenant))
    return run(_go())


def sync_actor(credential):
    async def _go():
        return await iauth.get_sync_actor(req(bearer(credential)))
    return run(_go())


def actor(fdb):
    return sync_actor(issue_credential(fdb)["credential"])


def make_body(**over):
    data = {
        "source": "aioffice",
        "source_bill_id": "SB-1001",
        "source_po_id": "PO-9",
        "supplier_invoice_no": "INV-778",
        "vendor_name": "Ferguson HVAC Supply",
        "vendor_external_id": "sup-42",
        "vendor_is_1099": False,
        "bill_date": "2026-11-01",
        "due_date": "2026-12-01",
        "lines": [
            {"description": "Copper line sets", "amount": 250.00,
             "job_ref": "aio-job-1", "customer_name": "Smith Residence"},
            {"description": "Shop filters", "amount": 50.00,
             "expense_code": "5700"},
        ],
    }
    data.update(over)
    return proc_r.ProcurementBillBody(**data)


def push(fdb_actor, key=None, **over):
    headers = {"Idempotency-Key": key} if key else {}

    async def _go():
        return await proc_r.create_procurement_bill(
            make_body(**over), user=fdb_actor, request=req(headers))
    return run(_go())


def coll_docs(fdb, name, tenant=None):
    docs = fdb._colls[name].docs
    if tenant:
        docs = [d for d in docs if d.get("tenant_id") == tenant]
    return docs


def entries(fdb, tenant=T1):
    return coll_docs(fdb, "journal_entries", tenant)


def lines_for(fdb, entry_number, tenant=T1):
    return [ln for ln in coll_docs(fdb, "journal_lines", tenant)
            if ln.get("entry_number") == entry_number]


# ---------------- the bill lands in AP, one GL entry ----------------
class TestBillLands:
    def test_supplier_bill_lands_open_with_balanced_entry(self, fdb):
        out = push(actor(fdb))
        assert out["deduplicated"] is False
        assert out["total_cents"] == 30000
        assert out["entry_number"]

        bills = coll_docs(fdb, "bills", T1)
        assert len(bills) == 1
        bill = bills[0]
        assert bill["id"] == out["bill_id"]
        assert bill["kind"] == "procurement"
        assert bill["source"] == "aioffice"
        assert bill["source_bill_id"] == "SB-1001"
        assert bill["source_po_id"] == "PO-9"
        assert bill["supplier_invoice_no"] == "INV-778"
        assert bill["status"] == "open"
        assert bill["amount_cents"] == 30000
        assert bill["vendor"] == "Ferguson HVAC Supply"
        assert bill["vendor_id"] == out["vendor_id"]
        assert bill["due_date"] == "2026-12-01"
        assert bill["entry_number"] == out["entry_number"]
        assert bill["issue_entry"] == out["entry_number"]
        # line expense rules: job-linked line defaults to 5000 material,
        # explicit code wins on the other line; job_ref preserved.
        by_desc = {li["description"]: li for li in bill["line_items"]}
        assert by_desc["Copper line sets"]["expense_code"] == "5000"
        assert by_desc["Copper line sets"]["job_ref"] == "aio-job-1"
        assert by_desc["Shop filters"]["expense_code"] == "5700"

        # exactly one journal entry: Dr lines / Cr 2010, balanced
        assert len(entries(fdb)) == 1
        entry = entries(fdb)[0]
        assert entry["number"] == out["entry_number"]
        assert entry["source"] == "bill"
        jl = lines_for(fdb, entry["number"])
        debits = {(ln["account_code"], ln["debit"]) for ln in jl if ln["debit"]}
        credits = {(ln["account_code"], ln["credit"]) for ln in jl if ln["credit"]}
        assert debits == {("5000", 25000), ("5700", 5000)}
        assert credits == {("2010", 30000)}
        assert sum(ln["debit"] for ln in jl) == sum(ln["credit"] for ln in jl) == 30000

    def test_non_job_line_defaults_to_5700(self, fdb):
        out = push(actor(fdb), lines=[
            {"description": "Misc supplies", "amount": 12.34}])
        jl = lines_for(fdb, out["entry_number"])
        assert {(ln["account_code"], ln["debit"]) for ln in jl if ln["debit"]} \
            == {("5700", 1234)}
        assert out["job_costs"] == []


# ---------------- job costing fed by the same entry ----------------
class TestJobCosts:
    def test_job_linked_lines_create_costs_on_one_entry(self, fdb):
        out = push(actor(fdb))
        assert len(out["job_costs"]) == 1
        jc = out["job_costs"][0]
        assert jc["amount_cents"] == 25000

        jobs = coll_docs(fdb, "jobs", T1)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["external_ref"] == "aio-job-1"
        assert job["source"] == "aioffice_procurement"
        assert job["status"] == "active"
        assert job["customer_name"] == "Smith Residence"
        assert job["job_number"].startswith("EXT-")
        assert jc["job_id"] == job["id"]

        rows = coll_docs(fdb, "job_costs", T1)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == jc["job_cost_id"]
        assert row["category"] == "material"
        assert row["bill_id"] == out["bill_id"]
        assert row["entry_number"] == out["entry_number"]
        assert row["vendor_name"] == "Ferguson HVAC Supply"
        assert "INV-778" in row["description"]

        # the jobs read path (profit aggregation) includes the material
        async def _get():
            return await jobs_r.get_job(job["id"], user=make_user(T1))
        got = run(_get())
        assert got["profit"]["actual_by_category_cents"]["material"] == 25000
        assert got["profit"]["actual_cost_cents"] == 25000
        assert len(got["costs"]) == 1

        # ONE journal entry total for the whole spend — the bill's.
        assert len(entries(fdb)) == 1

    def test_existing_job_is_resolved_not_recreated(self, fdb):
        coll_docs(fdb, "jobs")
        fdb._colls["jobs"].docs.append({
            "id": "job-existing", "tenant_id": T1, "job_number": "JOB-0042",
            "customer_name": "Jones", "customer_id": None,
            "service_location": "", "job_type": "repair", "description": "",
            "status": "active", "estimated_revenue_cents": 0,
            "estimated_costs_cents": {}, "change_orders": [],
            "external_ref": "aio-job-9", "created_at": "2026-10-01T00:00:00+00:00"})
        out = push(actor(fdb), lines=[
            {"description": "Valve", "amount": 80.00, "job_ref": "aio-job-9",
             "expense_code": "5700"}])
        assert out["job_costs"][0]["job_id"] == "job-existing"
        assert len(coll_docs(fdb, "jobs", T1)) == 1
        # explicit expense_code wins even on a job-linked line
        jl = lines_for(fdb, out["entry_number"])
        assert {(ln["account_code"], ln["debit"]) for ln in jl if ln["debit"]} \
            == {("5700", 8000)}


# ---------------- idempotency + natural dedupe ----------------
class TestDedupe:
    def test_replay_with_same_idempotency_key(self, fdb):
        a = actor(fdb)
        first = push(a, key="proc-key-1")
        second = push(a, key="proc-key-1")
        assert getattr(second, "status_code", 200) == 200
        assert second.headers.get("Idempotency-Replayed") == "true"
        replayed = json.loads(bytes(second.body))
        assert replayed["bill_id"] == first["bill_id"]
        assert replayed["entry_number"] == first["entry_number"]
        assert len(coll_docs(fdb, "bills", T1)) == 1
        assert len(coll_docs(fdb, "job_costs", T1)) == 1
        assert len(entries(fdb)) == 1
        rec = coll_docs(fdb, "idempotency_records")[0]
        assert rec["endpoint"] == "POST /integrations/procurement/bills"
        assert rec["status"] == "succeeded"
        assert rec["resource_id"] == first["bill_id"]

    def test_same_source_bill_without_key_dedupes(self, fdb):
        a = actor(fdb)
        first = push(a)
        second = push(a)
        assert second["deduplicated"] is True
        assert second["bill_id"] == first["bill_id"]
        assert second["entry_number"] == first["entry_number"]
        assert second["job_costs"] == first["job_costs"]
        assert len(coll_docs(fdb, "bills", T1)) == 1
        assert len(coll_docs(fdb, "job_costs", T1)) == 1
        assert len(entries(fdb)) == 1

    def test_crash_window_stamp_recovers(self, fdb):
        a = actor(fdb)
        first = push(a, key="proc-key-crash")
        rec = fdb._colls["idempotency_records"].docs[0]
        rec["status"] = "failed"
        rec["response_body"] = None
        rec["resource_id"] = None
        again = push(a, key="proc-key-crash")
        assert again["bill_id"] == first["bill_id"]
        assert len(coll_docs(fdb, "bills", T1)) == 1
        assert len(entries(fdb)) == 1


# ---------------- polish fixes (Claude round 1, 2026-11-05) ----------------
class TestPolishFixes:
    def test_key_reuse_with_different_payload_conflicts_409(self, fdb):
        # #2: same Idempotency-Key + DIFFERENT payload must conflict —
        # never replay the first response, never create a second bill.
        a = actor(fdb)
        first = push(a, key="proc-key-mismatch")
        assert first["total_cents"] == 30000
        with pytest.raises(HTTPException) as ei:
            push(a, key="proc-key-mismatch",
                 lines=[{"description": "Different", "amount": 1.00}])
        assert ei.value.status_code == 409
        assert "different" in str(ei.value.detail)
        assert len(coll_docs(fdb, "bills", T1)) == 1
        assert len(entries(fdb)) == 1

    def test_natural_replay_shape_matches_original(self, fdb):
        # #2 second half: a DIFFERENT key with the same natural anchor
        # returns the original bill in the same response shape, flagged.
        a = actor(fdb)
        first = push(a, key="proc-key-a")
        second = push(a, key="proc-key-b")
        assert second["deduplicated"] is True
        assert second["bill_id"] == first["bill_id"]
        assert second["entry_number"] == first["entry_number"]
        assert second["total_cents"] == first["total_cents"]
        assert second["vendor_id"] == first["vendor_id"]
        assert second["job_costs"] == first["job_costs"]
        assert len(coll_docs(fdb, "bills", T1)) == 1

    def test_half_up_conversion_at_edge(self, fdb):
        # #1 Books half: to_cents is Decimal ROUND_HALF_UP per line.
        assert proc_r.to_cents(2.675) == 268   # round() would give 267
        assert proc_r.to_cents(10.005) == 1001
        out = push(actor(fdb), lines=[
            {"description": "Half-up line", "amount": 2.675}])
        assert out["total_cents"] == 268
        jl = lines_for(fdb, out["entry_number"])
        debits = sum(ln["debit"] for ln in jl)
        credits = sum(ln["credit"] for ln in jl)
        assert debits == credits == 268

    def test_per_line_conversion_balance_invariant(self, fdb):
        # Three $0.005 lines: per-line half-up -> 1c each -> Dr 3 = Cr 3.
        out = push(actor(fdb), lines=[
            {"description": f"L{i}", "amount": 0.005} for i in range(3)])
        assert out["total_cents"] == 3
        jl = lines_for(fdb, out["entry_number"])
        assert sum(ln["debit"] for ln in jl) == sum(ln["credit"] for ln in jl) == 3

    def test_zero_total_bill_rejected_422(self, fdb):
        # #7 Books half: an all-zero bill never lands (no zero entry).
        a = actor(fdb)
        with pytest.raises(HTTPException) as ei:
            push(a, lines=[{"description": "Free sample", "amount": 0}])
        assert ei.value.status_code == 422
        assert fdb._colls.get("bills") is None \
            or len(coll_docs(fdb, "bills", T1)) == 0
        assert fdb._colls.get("journal_entries") is None \
            or len(entries(fdb)) == 0


# ---------------- vendor resolution ----------------
class TestVendorResolution:
    def test_vendor_created_once_reused_and_1099_lands(self, fdb):
        a = actor(fdb)
        first = push(a, vendor_is_1099=True)
        vendors = coll_docs(fdb, "vendors", T1)
        assert len(vendors) == 1
        v = vendors[0]
        assert v["id"] == first["vendor_id"]
        assert v["name"] == "Ferguson HVAC Supply"
        assert v["kind"] == "supplier"
        assert v["external_ref"] == "sup-42"
        assert v["source"] == "aioffice_procurement"
        assert v["is_1099"] is True
        assert v["form_1099"] is True

        # second bill: same external id, flag not asserted — reused, flag kept
        second = push(a, source_bill_id="SB-1002", vendor_is_1099=False)
        assert second["vendor_id"] == first["vendor_id"]
        assert len(coll_docs(fdb, "vendors", T1)) == 1
        assert coll_docs(fdb, "vendors", T1)[0]["is_1099"] is True
        assert len(coll_docs(fdb, "bills", T1)) == 2

        # third bill: no external id, name matches after normalization
        third = push(a, source_bill_id="SB-1003", vendor_external_id=None,
                     vendor_name="  ferguson   HVAC supply ")
        assert third["vendor_id"] == first["vendor_id"]
        assert len(coll_docs(fdb, "vendors", T1)) == 1


# ---------------- auth rails ----------------
class TestAuth:
    def test_unauthenticated_rejected(self, fdb):
        async def _go():
            return await iauth.get_sync_actor(req({}))
        with pytest.raises(HTTPException) as ei:
            run(_go())
        assert ei.value.status_code == 401

    def test_bad_secret_rejected(self, fdb):
        cred = issue_credential(fdb)["credential"]
        bad = cred[:-2] + "xx"

        async def _go():
            return await iauth.get_sync_actor(req(bearer(bad)))
        with pytest.raises(HTTPException) as ei:
            run(_go())
        assert ei.value.status_code == 401

    def test_wrong_scope_credential_rejected_403(self, fdb):
        doc, full = iauth.new_platform_credential_doc(
            platform="books", created_by="test", label="referrals",
            expires_in_days=30)
        run(fdb.integration_credentials.insert_one(doc))

        async def _go():
            return await iauth.get_sync_actor(req(bearer(full)))
        with pytest.raises(HTTPException) as ei:
            run(_go())
        assert ei.value.status_code == 403
        assert "books:sync" in str(ei.value.detail)

    def test_owner_session_actor_accepted(self, fdb):
        # ops:manage user path (owner) still works on the sync surface
        out = push(make_user(T1))
        assert out["total_cents"] == 30000
        assert len(coll_docs(fdb, "bills", T1)) == 1
