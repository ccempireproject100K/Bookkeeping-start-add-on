"""Retainage sub-ledger (owner-approved 2026-11-14, job-costing ask #7).

Direct-call tests: no mongod, no network. The retainage engine and the
jobs / bills / retainage router functions are awaited directly with a
fake user dict and an in-memory FakeDB monkeypatched into the `db`
attribute of the modules under test. (End-to-end HTTP proof lives in
the live drive under ~/workspace/buildout/retainage-tracking/proof/.)

Owner correction 2026-11-14 ~10:17 CST is load-bearing here: releases
are RECORD-ONLY markings. Tests assert a marking creates no invoice,
no bill, touches no cash account, and initiates no payment.
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

from lib import retainage as R
from routers import jobs as jobs_router
from routers import bills as bills_router
from routers import retainage as ret_router

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
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


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        items = [spec] if isinstance(spec, tuple) else list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: (d.get(field) is None, d.get(field)),
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
        ids = []
        for d in docs:
            d = copy.deepcopy(d)
            d.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(d)
            ids.append(d["_id"])
        return SimpleNamespace(inserted_ids=ids)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))

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
    monkeypatch.setattr(R, "db", fake)
    monkeypatch.setattr(jobs_router, "db", fake)
    monkeypatch.setattr(bills_router, "db", fake)
    monkeypatch.setattr(ret_router, "db", fake)
    # Base chart for the tenant — deliberately WITHOUT 1210/2030 so the
    # lane's ensure_accounts proves itself on first use.
    for code, name, atype, normal in [
            ("1200", "Accounts Receivable", "asset", "debit"),
            ("2010", "Accounts Payable", "liability", "credit"),
            ("2100", "Sales Tax Payable", "liability", "credit"),
            ("4000", "Job Revenue", "revenue", "credit"),
            ("5007", "Job Cost — Subcontractors", "expense", "debit"),
            ("1020", "Cash — Bank Checking", "asset", "debit")]:
        fake.accounts.docs.append({
            "id": f"acc-{code}", "code": code, "name": name, "type": atype,
            "normal_balance": normal, "archived": False,
            "tenant_id": TID, "_id": f"acc-{code}"})
    return fake


def seed_job(env, tenant=TID, **kw):
    job = {"id": kw.pop("id", "job-1"), "job_number": "JOB-0001",
           "customer_name": "Acme Builders", "status": "active",
           "estimated_revenue_cents": 500_000,
           "estimated_costs_cents": {"material": 100_000},
           "change_orders": [], "phases": [],
           "retainage_pct": 0, "retainage_payable_pct": None,
           "retainage_phase_pcts": {},
           "retainage_release_days": None,
           "retainage_release_due_date": None,
           "substantial_completion_date": None,
           "tenant_id": tenant, "_id": kw.pop("id", "job-1") + "-doc"}
    job.update(kw)
    env.jobs.docs.append(job)
    return job


def gl_lines(env, tenant=TID):
    return [d for d in env.journal_lines.docs if d.get("tenant_id") == tenant]


def gl_net(env, code, tenant=TID):
    rows = [d for d in gl_lines(env, tenant) if d["account_code"] == code]
    return sum(d["debit"] for d in rows) - sum(d["credit"] for d in rows)


def lines_for(env, entry_number, tenant=TID):
    return [d for d in gl_lines(env, tenant) if d["entry_number"] == entry_number]


def invoice_job(env, job_id="job-1", amount=1000.0, tax=0.0, phase_id=None, user=USER):
    body = jobs_router.JobInvoiceBody(amount=amount, tax=tax, phase_id=phase_id)
    return run(jobs_router.invoice_job(job_id, body, user))


# ============================ held math ============================
def test_held_rounding():
    assert R.held_cents_for(100_000, 10) == 10_000
    assert R.held_cents_for(9_999, 10) == 1_000        # 999.9 -> 1000 half-up
    assert R.held_cents_for(333, 7.5) == 25           # 24.975 -> 25
    assert R.held_cents_for(100_000, 0) == 0
    assert R.held_cents_for(0, 10) == 0


def test_invoice_held_math_and_postings(env):
    seed_job(env, retainage_pct=10)
    inv = invoice_job(env, amount=1000.0, tax=80.0)
    # gross 1080.00; held = 10% of the 1000.00 subtotal; net due 980.00
    assert inv["total_cents"] == 108_000
    assert inv["retainage_held_cents"] == 10_000
    assert inv["net_due_cents"] == 98_000
    assert inv["retainage_pct"] == 10
    lines = lines_for(env, inv["issue_entry"])
    by_code = {(d["account_code"], "dr" if d["debit"] else "cr"): d["debit"] or d["credit"]
               for d in lines}
    assert by_code[("1200", "dr")] == 98_000
    assert by_code[("1210", "dr")] == 10_000
    assert by_code[("4000", "cr")] == 100_000
    assert by_code[("2100", "cr")] == 8_000
    assert sum(d["debit"] for d in lines) == sum(d["credit"] for d in lines)
    # sub-ledger row
    holds = env.retainage_entries.docs
    assert len(holds) == 1
    h = holds[0]
    assert h["side"] == "receivable" and h["held_cents"] == 10_000
    assert h["released_cents"] == 0 and h["status"] == "held"
    assert h["source_type"] == "job_invoice" and h["source_id"] == inv["id"]
    # ensure_accounts added the retainage accounts for this tenant
    codes = {a["code"] for a in env.accounts.docs if a["tenant_id"] == TID}
    assert "1210" in codes and "2030" in codes


def test_no_retainage_invoice_unchanged(env):
    seed_job(env, retainage_pct=0)
    inv = invoice_job(env, amount=500.0)
    assert inv["retainage_held_cents"] == 0
    lines = lines_for(env, inv["issue_entry"])
    assert {(d["account_code"]) for d in lines} == {"1200", "4000"}
    assert env.retainage_entries.docs == []


# ==================== release MARKING (record-only) ====================
def test_mark_release_is_record_only(env):
    seed_job(env, retainage_pct=10)
    inv = invoice_job(env, amount=1000.0)
    entry = env.retainage_entries.docs[0]
    n_invoices = len(env.invoices.docs)
    n_bills = len(env.bills.docs)
    n_entries = len(env.journal_entries.docs)

    out = run(ret_router.mark_entry_released(
        entry["id"], ret_router.MarkReleaseBody(note="customer paid it out"), USER))
    assert out["status"] == "released"
    assert out["outstanding_cents"] == 0
    rec = out["releases"][0]
    assert rec["amount_cents"] == 10_000
    assert rec["recorded_by"] == "owner@test.com"
    assert rec["note"] == "customer paid it out"

    # Exactly ONE new journal entry: a reclass. No cash line anywhere,
    # no invoice created, no bill created — nothing that moves money.
    assert len(env.journal_entries.docs) == n_entries + 1
    new_je = env.journal_entries.docs[-1]
    assert new_je["source"] == "retainage_release_recorded"
    lines = lines_for(env, new_je["number"])
    assert {d["account_code"] for d in lines} == {"1200", "1210"}
    assert not any(d["account_code"] in ("1010", "1020", "1050") for d in lines)
    assert len(env.invoices.docs) == n_invoices
    assert len(env.bills.docs) == n_bills
    # GL truth: retainage receivable cleared, AR carries the amount now.
    assert gl_net(env, "1210") == 0
    assert gl_net(env, "1200") == 100_000


def test_double_mark_refused_no_new_entry(env):
    seed_job(env, retainage_pct=10)
    invoice_job(env, amount=1000.0)
    entry = env.retainage_entries.docs[0]
    run(ret_router.mark_entry_released(entry["id"], ret_router.MarkReleaseBody(), USER))
    n = len(env.journal_entries.docs)
    with pytest.raises(HTTPException) as ei:
        run(ret_router.mark_entry_released(entry["id"], ret_router.MarkReleaseBody(), USER))
    assert ei.value.status_code == 409
    assert len(env.journal_entries.docs) == n


def test_partial_then_over_mark(env):
    seed_job(env, retainage_pct=10)
    invoice_job(env, amount=1000.0)
    entry = env.retainage_entries.docs[0]
    out = run(ret_router.mark_entry_released(
        entry["id"], ret_router.MarkReleaseBody(amount=40.0), USER))
    assert out["status"] == "partially_released"
    assert out["outstanding_cents"] == 6_000
    with pytest.raises(HTTPException) as ei:
        run(ret_router.mark_entry_released(
            entry["id"], ret_router.MarkReleaseBody(amount=60.01), USER))
    assert ei.value.status_code == 422
    out = run(ret_router.mark_entry_released(
        entry["id"], ret_router.MarkReleaseBody(amount=60.0), USER))
    assert out["status"] == "released" and out["outstanding_cents"] == 0


def test_job_level_mark_fifo(env):
    seed_job(env, retainage_pct=10)
    invoice_job(env, amount=600.0)
    invoice_job(env, amount=400.0)
    res = run(ret_router.mark_job_released(
        "job-1", ret_router.JobReleaseBody(side="receivable", amount=70.0), USER))
    by_source = {m["source_id"]: m for m in res["marked"]}
    assert len(res["marked"]) == 2
    assert list(by_source.values())[0]["status"] == "released"
    second = res["marked"][1]
    assert second["released_cents"] == 1_000 and second["outstanding_cents"] == 3_000
    assert res["summary"]["receivable"]["outstanding_cents"] == 3_000


# ============================ reminders ============================
def test_reminder_fires_once_and_stops_after_mark(env):
    seed_job(env, retainage_pct=10, retainage_release_due_date="2026-11-01")
    invoice_job(env, amount=1000.0)
    fired = run(R.check_reminders(TID, today="2026-11-14"))
    assert len(fired) == 1 and fired[0]["kind"] == "retainage_release_due"
    assert "does not collect" in fired[0]["body"]
    fired2 = run(R.check_reminders(TID, today="2026-11-15"))
    assert fired2 == []   # deduped — no nagging
    # Mark it released; a later check must stay silent.
    entry = env.retainage_entries.docs[0]
    run(ret_router.mark_entry_released(entry["id"], ret_router.MarkReleaseBody(), USER))
    fired3 = run(R.check_reminders(TID, today="2026-11-16"))
    assert fired3 == []
    notifs = [d for d in env.app_notifications.docs
              if d.get("kind") == "retainage_release_due"]
    assert len(notifs) == 1


def test_reminder_not_due_yet(env):
    seed_job(env, retainage_pct=10, retainage_release_due_date="2027-01-01")
    invoice_job(env, amount=1000.0)
    assert run(R.check_reminders(TID, today="2026-11-14")) == []


def test_reminder_due_from_completion_plus_days(env):
    job = seed_job(env, retainage_pct=10)
    run(ret_router.save_settings("job-1", ret_router.SettingsBody(
        substantial_completion_date="2026-10-01", retainage_release_days=30), USER))
    invoice_job(env, amount=1000.0)
    summary = run(R.job_summary(TID, job))
    assert summary["release_due_date"] == "2026-10-31"
    fired = run(R.check_reminders(TID, today="2026-11-14"))
    assert len(fired) == 1


# ============================ phases ============================
def test_phase_split_sums_to_job_total(env):
    seed_job(env, retainage_pct=0,
             phases=[{"id": "ph1", "name": "Rough-in"}, {"id": "ph2", "name": "Trim-out"}],
             retainage_phase_pcts={"ph1": 10, "ph2": 10})
    invoice_job(env, amount=600.0, phase_id="ph1")
    invoice_job(env, amount=400.0, phase_id="ph2")
    job = env.jobs.docs[0]
    summary = run(R.job_summary(TID, job))
    assert summary["phases"]["ph1"]["receivable"]["held_cents"] == 6_000
    assert summary["phases"]["ph2"]["receivable"]["held_cents"] == 4_000
    phase_sum = sum(p["receivable"]["held_cents"] for p in summary["phases"].values())
    assert phase_sum == summary["receivable"]["held_cents"] == 10_000


def test_phase_override_beats_job_pct(env):
    seed_job(env, retainage_pct=10,
             phases=[{"id": "ph1", "name": "Rough-in"}],
             retainage_phase_pcts={"ph1": {"receivable": 5}})
    inv = invoice_job(env, amount=1000.0, phase_id="ph1")
    assert inv["retainage_held_cents"] == 5_000


def test_unknown_phase_rejected(env):
    seed_job(env, retainage_pct=10, phases=[{"id": "ph1", "name": "Rough-in"}])
    with pytest.raises(HTTPException) as ei:
        invoice_job(env, amount=100.0, phase_id="nope")
    assert ei.value.status_code == 422


# ============================ payable side ============================
def test_payable_hold_and_mark(env):
    seed_job(env, retainage_payable_pct=10)
    body = bills_router.BillBody(vendor="Sub Co", amount=2000.0,
                                 expense_code="5007", job_id="job-1")
    bill = run(bills_router.create_bill(body, USER))
    assert bill["gross_cents"] == 200_000
    assert bill["retainage_held_cents"] == 20_000
    assert bill["amount_cents"] == 180_000      # net rides regular AP
    lines = lines_for(env, bill["issue_entry"])
    by_code = {(d["account_code"], "dr" if d["debit"] else "cr"): d["debit"] or d["credit"]
               for d in lines}
    assert by_code[("5007", "dr")] == 200_000
    assert by_code[("2010", "cr")] == 180_000
    assert by_code[("2030", "cr")] == 20_000
    assert sum(d["debit"] for d in lines) == sum(d["credit"] for d in lines)

    holds = [e for e in env.retainage_entries.docs if e["side"] == "payable"]
    assert len(holds) == 1 and holds[0]["counterparty"] == "Sub Co"
    n_bills = len(env.bills.docs)
    out = run(ret_router.mark_entry_released(
        holds[0]["id"], ret_router.MarkReleaseBody(), USER))
    assert out["status"] == "released"
    assert gl_net(env, "2030") == 0
    # Record-only: marking created no new bill and no invoice.
    assert len(env.bills.docs) == n_bills
    assert env.invoices.docs == []


def test_bill_without_job_unchanged(env):
    body = bills_router.BillBody(vendor="Supplier", amount=250.0, expense_code="5007")
    bill = run(bills_router.create_bill(body, USER))
    assert bill["amount_cents"] == 25_000 and bill["retainage_held_cents"] == 0
    lines = lines_for(env, bill["issue_entry"])
    assert {d["account_code"] for d in lines} == {"5007", "2010"}
    assert env.retainage_entries.docs == []


# ============================ isolation + settings ============================
def test_tenant_isolation(env):
    seed_job(env, retainage_pct=10)
    invoice_job(env, amount=1000.0)
    entry = env.retainage_entries.docs[0]
    rollup2 = run(ret_router.rollup(USER2))
    assert rollup2["receivable"]["held_cents"] == 0 and rollup2["jobs"] == []
    with pytest.raises(HTTPException) as ei:
        run(ret_router.mark_entry_released(
            entry["id"], ret_router.MarkReleaseBody(), USER2))
    assert ei.value.status_code == 404


def test_settings_validation(env):
    seed_job(env)
    with pytest.raises(HTTPException) as ei:
        run(ret_router.save_settings(
            "job-1", ret_router.SettingsBody(retainage_pct=150), USER))
    assert ei.value.status_code == 422


def test_rollup_totals(env):
    seed_job(env, retainage_pct=10, retainage_payable_pct=10)
    invoice_job(env, amount=1000.0)
    run(bills_router.create_bill(
        bills_router.BillBody(vendor="Sub Co", amount=2000.0,
                              expense_code="5007", job_id="job-1"), USER))
    roll = run(ret_router.rollup(USER))
    assert roll["receivable"]["outstanding_cents"] == 10_000
    assert roll["payable"]["outstanding_cents"] == 20_000
    assert len(roll["jobs"]) == 1
