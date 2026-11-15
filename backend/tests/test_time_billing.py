"""Top-20 #15 — bill-this-time bridge: approved billable time -> invoice lines.

Direct-call tests on the same FakeDB harness as the field-capture suite: no
mongod, no network; the REAL ledger runs against the fake when a draft is
issued, so journal balance is exercised for real.
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
from routers import time as time_r
from routers import time_billing as tb

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
    monkeypatch.setattr(tb, "db", fake)
    monkeypatch.setattr(audit_lib, "db", fake)
    # Merged tree (2026-11-14): job status/cost paths also evaluate budget
    # alerts + committed-cost config through those lib modules' db handles.
    monkeypatch.setattr(jobalerts_lib, "db", fake)
    monkeypatch.setattr(jobcosting_lib, "db", fake)
    return fake


async def _make_job(customer="Acme HOA"):
    job = await jobs_r.create_job(
        jobs_r.JobBody(customer_name=customer, estimated_revenue=1000,
                       estimated_costs={"labor": 500, "material": 200}), OWNER)
    await jobs_r.update_status(job["id"], jobs_r.StatusBody(status="active"), OWNER)
    job["status"] = "active"
    return job


async def _entry(date="2026-11-10", hours=2.0, client="Acme HOA", notes="Install condenser",
                 job_id=None, pay=50.0, billable_rate=None, approve=True, billable=True):
    e = await time_r.log_entry(time_r.ManualEntryBody(
        date=date, hours=hours, notes=notes, client_name=client, billable=billable,
        job_id=job_id, rate=pay, billable_rate=billable_rate), TECH)
    if approve:
        await time_r.approve_entry(e["id"], OWNER)
        e["status"] = "approved"
    return e


def _preview(ids, grouping="entry", overrides=None):
    return tb.PreviewBody(entry_ids=ids, grouping=grouping,
                          rate_overrides=overrides or {})


def _convert(ids, grouping="entry", overrides=None, fingerprint=""):
    return tb.ConvertBody(entry_ids=ids, grouping=grouping,
                          rate_overrides=overrides or {}, fingerprint=fingerprint)


# ============================ the view ============================
def test_view_only_approved_billable_unbilled(env):
    async def go():
        job = await _make_job()
        a = await _entry(job_id=job["id"], billable_rate=90.0)
        b = await _entry(date="2026-11-11", hours=1.5, client="Beta LLC",
                         notes="Panel upgrade", billable_rate=80.0)
        await _entry(notes="not approved", approve=False, billable_rate=90.0)
        await _entry(notes="not billable", billable=False, billable_rate=90.0)
        closed = await _entry(date="2026-11-09", hours=1.0, notes="Follow-up",
                              billable_rate=90.0)
        await time_r.close_entry(closed["id"], OWNER)
        env.time_entries.docs.append({
            "id": "already-billed", "tenant_id": TID, "user_id": TECH["id"],
            "user_name": "Tech", "date": "2026-11-08", "hours": 3.0, "notes": "Old",
            "client_name": "Acme HOA", "billable": True, "status": "approved",
            "rate_cents": 5000, "billed_invoice_id": "inv-old",
            "job_id": None, "created_at": "2026-11-08T00:00:00+00:00"})
        view = await tb.unbilled(OWNER)
        ids = {v["id"] for v in view["entries"]}
        assert ids == {a["id"], b["id"], closed["id"]}
        by_client = {g["client_name"]: g for g in view["groups"]}
        assert by_client["Acme HOA"]["hours"] == 3.0
        assert by_client["Acme HOA"]["amount_cents"] == 18000 + 9000
        job_groups = {g["job_id"]: g for g in by_client["Acme HOA"]["jobs"]}
        assert job_groups[job["id"]]["job_number"] == job["job_number"]
        assert job_groups[job["id"]]["entry_ids"] == [a["id"]]
        assert by_client["Beta LLC"]["amount_cents"] == 12000
    run(go())


def test_view_needs_rate_and_pay_rate_never_billed(env):
    async def go():
        # Entry carries ONLY a pay rate (rate_cents) — a cost figure.
        e = await _entry(billable_rate=None)
        view = await tb.unbilled(OWNER)
        v = view["entries"][0]
        assert v["id"] == e["id"] and v["needs_rate"] is True
        assert v["rate_cents"] is None and v["amount_cents"] is None
        assert view["needs_rate_count"] == 1 and view["total_amount_cents"] == 0
    run(go())


def test_rate_resolution_order_entry_then_job_then_tenant(env):
    async def go():
        job = await _make_job()
        # job remembers $70
        env.jobs.docs[0]["billing_rate_cents"] = 7000
        await tb.put_settings(tb.SettingsBody(default_billing_rate=60.0), OWNER)
        e_job = await _entry(job_id=job["id"], billable_rate=None)     # -> job 70
        e_tenant = await _entry(client="Beta LLC", billable_rate=None)  # -> tenant 60
        e_own = await _entry(job_id=job["id"], billable_rate=95.0)      # -> own 95
        view = await tb.unbilled(OWNER)
        by_id = {v["id"]: v for v in view["entries"]}
        assert (by_id[e_job["id"]]["rate_cents"], by_id[e_job["id"]]["rate_source"]) == (7000, "job")
        assert (by_id[e_tenant["id"]]["rate_cents"], by_id[e_tenant["id"]]["rate_source"]) == (6000, "tenant")
        assert (by_id[e_own["id"]]["rate_cents"], by_id[e_own["id"]]["rate_source"]) == (9500, "entry")
        assert by_id[e_job["id"]]["job_remembered_rate_cents"] == 7000
    run(go())


# ============================ preview gate ============================
def test_preview_groupings_agree_on_total_and_describe(env):
    async def go():
        job = await _make_job()
        a = await _entry(date="2026-11-10", hours=2.0, job_id=job["id"],
                         notes="Install condenser", billable_rate=90.0)
        b = await _entry(date="2026-11-10", hours=1.0, job_id=job["id"],
                         notes="Wire thermostat", billable_rate=90.0)
        c = await _entry(date="2026-11-11", hours=3.0, job_id=job["id"],
                         notes="Duct run", billable_rate=90.0)
        ids = [a["id"], b["id"], c["id"]]
        p_entry = await tb.preview(_preview(ids, "entry"), OWNER)
        p_day = await tb.preview(_preview(ids, "day"), OWNER)
        p_job = await tb.preview(_preview(ids, "job"), OWNER)
        for p in (p_entry, p_day, p_job):
            assert p["can_convert"] and p["total_cents"] == 54000 and p["fingerprint"]
        assert len(p_entry["lines"]) == 3 and len(p_day["lines"]) == 2
        assert len(p_job["lines"]) == 1 and p_job["lines"][0]["hours"] == 6.0
        line = p_entry["lines"][0]
        assert "2026-11-10" in line["name"] and "Tech" in line["name"]
        assert job["job_number"] in line["name"] and "Install condenser" in line["name"]
        assert line["time_entry_ids"] == [a["id"]] and line["rate_cents"] == 9000
        assert p_day["lines"][0]["time_entry_ids"] == [a["id"], b["id"]]
    run(go())


def test_preview_is_a_pure_read(env):
    async def go():
        e = await _entry(billable_rate=90.0)
        await tb.preview(_preview([e["id"]]), OWNER)
        assert env.invoices.docs == []
        doc = env.time_entries.docs[0]
        assert not doc.get("billed_invoice_id") and doc.get("billing_history") in (None, [])
    run(go())


def test_needs_rate_blocks_until_set_in_preview(env):
    async def go():
        e = await _entry(billable_rate=None)
        p = await tb.preview(_preview([e["id"]]), OWNER)
        assert p["can_convert"] is False and p["fingerprint"] is None
        assert any("needs a billing rate" in pr["problem"] for pr in p["problems"])
        with pytest.raises(HTTPException) as ei:
            await tb.convert(_convert([e["id"]], fingerprint="x"), OWNER)
        assert ei.value.status_code == 409
        # ...but an override in the preview prices it and lets it through
        p2 = await tb.preview(_preview([e["id"]], overrides={e["id"]: 75.0}), OWNER)
        assert p2["can_convert"] and p2["lines"][0]["rate_source"] == "override"
        assert p2["total_cents"] == 15000
    run(go())


def test_convert_rejects_stale_or_fabricated_fingerprint(env):
    async def go():
        e = await _entry(billable_rate=90.0)
        p = await tb.preview(_preview([e["id"]]), OWNER)
        with pytest.raises(HTTPException) as ei:
            await tb.convert(_convert([e["id"]], fingerprint="deadbeef"), OWNER)
        assert ei.value.status_code == 409
        # rate changes after the preview was shown -> the confirmed lines no
        # longer match what conversion would create -> refused
        await time_r.update_entry(e["id"], time_r.EntryPatchBody(billable_rate=120.0), OWNER)
        with pytest.raises(HTTPException) as ei:
            await tb.convert(_convert([e["id"]], fingerprint=p["fingerprint"]), OWNER)
        assert ei.value.status_code == 409
        assert env.invoices.docs == []
    run(go())


def test_mixed_clients_blocked(env):
    async def go():
        a = await _entry(client="Acme HOA", billable_rate=90.0)
        b = await _entry(client="Beta LLC", billable_rate=90.0)
        p = await tb.preview(_preview([a["id"], b["id"]]), OWNER)
        assert p["can_convert"] is False
        assert any("more than one client" in pr["problem"] for pr in p["problems"])
    run(go())


# ============================ convert / stamp ============================
def test_convert_creates_draft_stamps_both_ways_no_cost_duplication(env):
    async def go():
        job = await _make_job()
        e = await _entry(job_id=job["id"], billable_rate=90.0)
        costs_before = [dict(d) for d in env.job_costs.docs]
        assert len(costs_before) == 1  # field capture posted the labor cost
        p = await tb.preview(_preview([e["id"]]), OWNER)
        inv = await tb.convert(_convert([e["id"]], fingerprint=p["fingerprint"]), OWNER)
        assert inv["status"] == "draft" and inv["source"] == "time_billing"
        assert inv["total_cents"] == 18000 and inv["client_name"] == "Acme HOA"
        assert inv["lines"][0]["time_entry_ids"] == [e["id"]]
        assert inv["time_entry_ids"] == [e["id"]]
        assert inv["issue_entry"] is None
        doc = env.time_entries.docs[0]
        assert doc["billed_invoice_id"] == inv["id"]
        assert doc["billed_rate_cents"] == 9000
        assert doc["billing_history"][0]["action"] == "billed"
        # revenue side only: job costs untouched, and the draft posts NO new
        # journal entry (the one JE present is field capture's labor cost,
        # posted at approval — before conversion)
        assert [dict(d) for d in env.job_costs.docs] == costs_before
        jes = env.journal_entries.docs
        assert len(jes) == 1 and jes[0]["source"] != "invoice_issue"
        # and the entry leaves the unbilled view
        assert (await tb.unbilled(OWNER))["entries"] == []
    run(go())


def test_already_billed_never_converts_twice(env):
    async def go():
        e = await _entry(billable_rate=90.0)
        p = await tb.preview(_preview([e["id"]]), OWNER)
        await tb.convert(_convert([e["id"]], fingerprint=p["fingerprint"]), OWNER)
        p2 = await tb.preview(_preview([e["id"]]), OWNER)
        assert p2["can_convert"] is False
        assert any("already billed" in pr["problem"] for pr in p2["problems"])
        with pytest.raises(HTTPException) as ei:
            await tb.convert(_convert([e["id"]], fingerprint="x"), OWNER)
        assert ei.value.status_code == 409
        assert len(env.invoices.docs) == 1
    run(go())


def test_conversion_remembers_the_jobs_rate(env):
    async def go():
        job = await _make_job()
        e1 = await _entry(job_id=job["id"], billable_rate=None)
        p = await tb.preview(_preview([e1["id"]], overrides={e1["id"]: 88.0}), OWNER)
        await tb.convert(_convert([e1["id"]], overrides={e1["id"]: 88.0},
                                  fingerprint=p["fingerprint"]), OWNER)
        job_doc = env.jobs.docs[0]
        assert job_doc["billing_rate_cents"] == 8800
        assert env.time_entries.docs[0]["billable_rate_cents"] == 8800  # override stamped
        # next entry on the same job pre-fills from the job's remembered rate
        e2 = await _entry(date="2026-11-12", job_id=job["id"], billable_rate=None)
        view = await tb.unbilled(OWNER)
        v = {x["id"]: x for x in view["entries"]}[e2["id"]]
        assert (v["rate_cents"], v["rate_source"]) == (8800, "job")
        p2 = await tb.preview(_preview([e2["id"]]), OWNER)
        assert p2["lines"][0]["rate_source"] == "job"
    run(go())


# ============================ draft lifecycle ============================
def test_void_draft_frees_entries_with_history(env):
    async def go():
        e = await _entry(billable_rate=90.0)
        p = await tb.preview(_preview([e["id"]]), OWNER)
        inv = await tb.convert(_convert([e["id"]], fingerprint=p["fingerprint"]), OWNER)
        out = await tb.void_draft(inv["id"], OWNER)
        assert out["freed_entries"] == 1
        assert env.invoices.docs[0]["status"] == "void"
        doc = env.time_entries.docs[0]
        assert doc["billed_invoice_id"] is None
        assert [h["action"] for h in doc["billing_history"]] == ["billed", "unbilled"]
        assert len((await tb.unbilled(OWNER))["entries"]) == 1  # billable again
    run(go())


def test_issue_posts_balanced_entry_and_locks_billing(env):
    async def go():
        e = await _entry(billable_rate=90.0)
        p = await tb.preview(_preview([e["id"]]), OWNER)
        inv = await tb.convert(_convert([e["id"]], fingerprint=p["fingerprint"]), OWNER)
        issued = await tb.issue_draft(inv["id"], OWNER)
        assert issued["status"] == "issued" and issued["issue_entry"]
        jes = [d for d in env.journal_entries.docs
               if d.get("status") == "posted" and d.get("source") == "invoice_issue"]
        assert len(jes) == 1 and jes[0]["total_cents"] == 18000
        lines = [d for d in env.journal_lines.docs if d["entry_id"] == jes[0]["id"]]
        dr = sum(l["debit"] for l in lines)
        cr = sum(l["credit"] for l in lines)
        assert dr == cr == 18000
        assert any(l["account_code"] == "1200" and l["debit"] == 18000 for l in lines)
        # issued = sent: voiding is refused and the entry stays billed
        with pytest.raises(HTTPException) as ei:
            await tb.void_draft(inv["id"], OWNER)
        assert ei.value.status_code == 409
        assert env.time_entries.docs[0]["billed_invoice_id"] == inv["id"]
    run(go())


def test_tenant_isolation(env):
    async def go():
        e = await _entry(billable_rate=90.0)
        assert (await tb.unbilled(OTHER))["entries"] == []
        with pytest.raises(HTTPException) as ei:
            await tb.preview(_preview([e["id"]]), OTHER)
        assert ei.value.status_code == 404
    run(go())
