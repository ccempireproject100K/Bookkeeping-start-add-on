"""Top-20 #19 — period-close lock suite.

Covers: hard-lock refusal at the ledger chokepoint (direct journal,
draft posting, and the bill/invoice paths that post through it), the
close checklist + exception acknowledgment, close snapshots (frozen at
close, immutable across reopen/reclose), the soft->hard two-stage close
with grace, the reasoned/timed reopen with tagging + spotlight +
notifications, and the owner's three tenant switches (snapshot,
soft stage, reopen apparatus) — all default ON, each proven absent
when switched OFF with the rest of the system unaffected.

Direct-call tests: no mongod, no network. Router endpoints are awaited
directly with fake user dicts and an in-memory FakeDB monkeypatched
into every module under test (harness shared with the sibling lanes,
extended with dotted-path $set, update_many, and $match/$group
aggregation for the snapshot math).
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
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest
from fastapi import HTTPException

from lib import ledger as L
from lib import periods as PC
from lib.base import Scoped
from lib.coa import seed_coa
from routers import bills as bills_router
from routers import invoices as invoices_router
from routers import ledger as ledger_router

TID = "tenant-test"
TID2 = "tenant-other"
OWNER = {"id": "user-owner", "email": "owner@test.com", "name": "Olivia Owner",
         "role": "owner", "tenant_id": TID}
ACCT = {"id": "user-acct", "email": "acct@test.com", "name": "Adam Accountant",
        "role": "accountant", "tenant_id": TID}
BKPR = {"id": "user-bk", "email": "bk@test.com", "name": "Beth Bookkeeper",
        "role": "bookkeeper", "tenant_id": TID}
OWNER2 = {"id": "user-owner2", "email": "owner2@test.com", "name": "Other Owner",
          "role": "owner", "tenant_id": TID2}

JAN = "2026-01"
T0 = "2026-02-01T09:00:00+00:00"


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
                elif op == "$lt":
                    if val is _MISSING or not val < operand:
                        return False
                elif op == "$gt":
                    if val is _MISSING or not val > operand:
                        return False
                else:
                    raise NotImplementedError(f"FakeDB: operator {op}")
        else:
            if val is _MISSING or val != cond:
                return False
    return True


def _set_path(doc, path, value):
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = copy.deepcopy(value)


def _get_path(doc, path):
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _apply_update(doc, update):
    for op, fields in (update or {}).items():
        if op == "$set":
            for k, v in fields.items():
                _set_path(doc, k, v)
        elif op == "$inc":
            for k, v in fields.items():
                _set_path(doc, k, (_get_path(doc, k) or 0) + v)
        elif op == "$push":
            for k, v in fields.items():
                lst = _get_path(doc, k)
                if lst is None:
                    _set_path(doc, k, [])
                    lst = _get_path(doc, k)
                lst.append(copy.deepcopy(v))
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

    # Merge-train (2026-11-14): main's lock guard async-iterates find
    # cursors; the fake cursor supports it like motor's.
    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class FakeAggCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return copy.deepcopy(self._docs)


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
            res = await self.insert_one(d)
            ids.append(res.inserted_id)
        return SimpleNamespace(inserted_ids=ids)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
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

    def aggregate(self, pipeline):
        rows = [copy.deepcopy(d) for d in self.docs]
        for stage in pipeline:
            if "$match" in stage:
                rows = [d for d in rows if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                idf = spec["_id"]
                field = idf[1:] if isinstance(idf, str) and idf.startswith("$") else idf
                groups = {}
                for d in rows:
                    g = groups.setdefault(d.get(field), {"_id": d.get(field)})
                    for out_f, expr in spec.items():
                        if out_f == "_id":
                            continue
                        src = expr["$sum"]
                        sf = src[1:] if isinstance(src, str) and src.startswith("$") else src
                        g[out_f] = g.get(out_f, 0) + (d.get(sf) or 0)
                rows = list(groups.values())
            else:
                raise NotImplementedError(f"FakeDB aggregate: {stage}")
        return FakeAggCursor(rows)


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
    for mod in (bills_router, invoices_router, ledger_router):
        monkeypatch.setattr(mod, "db", fake)
    # two tenants, each with a workspace doc + seeded chart
    for tid, name in ((TID, "Test Shop"), (TID2, "Other Shop")):
        run(fake.tenants.insert_one({"id": tid, "name": name, "settings": {},
                                     "created_at": T0}))
        run(seed_coa(Scoped(fake.accounts, tid), "barbershop"))
    # staff seats for tenant 1 (notification targets)
    for u in (OWNER, ACCT, BKPR):
        run(fake.users.insert_one({**u, "password_hash": "x"}))
    codes = {a["code"] for a in fake.accounts.docs if a["tenant_id"] == TID}
    assert {"1020", "2010", "4000", "5300"} <= codes
    return fake


def post(env, user, date, lines, **kw):
    return run(L.post_journal_entry(env, user["tenant_id"], user,
                                    {"entry_date": date, "lines": lines, **kw},
                                    status="posted"))


def expense(date="2026-01-15", cents=100_00):
    return [{"account_code": "5300", "debit": cents, "credit": 0},
            {"account_code": "1020", "debit": 0, "credit": cents}]


def close_hard(env, user=OWNER, key=JAN, **kw):
    kw.setdefault("grace_days", 0)
    return run(PC.close_period(env, user["tenant_id"], user, key, **kw))


def period(env, key=JAN, tid=TID):
    return next(p for p in env.fiscal_periods.docs
                if p["tenant_id"] == tid and p["key"] == key)


def expect_status(status, fn):
    with pytest.raises(HTTPException) as ei:
        fn()
    assert ei.value.status_code == status, ei.value.detail
    return ei.value


# ==================== the hard lock at the chokepoint ====================
def test_hard_lock_refuses_direct_post(env):
    close_hard(env)
    assert period(env)["status"] == "locked"
    err = expect_status(423, lambda: post(env, OWNER, "2026-01-15", expense()))
    assert JAN in err.detail and "owner@test.com" in err.detail  # names period + locker
    assert env.journal_entries.docs == []  # nothing was written


def test_lock_refuses_via_bill_and_invoice_paths(env):
    # a bill dated in the period posts at creation -> refused once locked
    close_hard(env)
    expect_status(423, lambda: run(bills_router.create_bill(
        bills_router.BillBody(vendor="Supply Co", expense_code="5300",
                              amount=25.0, bill_date="2026-01-10"), OWNER)))
    assert env.bills.docs == []

    # an open-period bill paid with a pay_date inside the locked period
    bill = run(bills_router.create_bill(
        bills_router.BillBody(vendor="Rent A", expense_code="5300",
                              amount=40.0, bill_date="2026-02-10"), OWNER))
    expect_status(423, lambda: run(bills_router.pay_bill(
        bill["id"], bills_router.PayBody(pay_date="2026-01-20"), OWNER)))

    # an invoice payment dated inside the locked period
    inv = run(invoices_router.issue_invoice(invoices_router.InvoiceBody(
        client_name="Client", lines=[invoices_router.InvLine(
            name="Service", revenue_code="4000", amount=80.0)]), OWNER))
    expect_status(423, lambda: run(invoices_router.pay_invoice(
        inv["id"], invoices_router.PayBody(pay_date="2026-01-21"), OWNER)))


def test_draft_allowed_but_posting_draft_refused(env):
    close_hard(env)
    draft = run(L.post_journal_entry(env, TID, OWNER,
                                     {"entry_date": "2026-01-15", "lines": expense()},
                                     status="draft"))
    assert draft["status"] == "draft"  # no ledger effect -> allowed
    expect_status(423, lambda: run(L.post_draft(env, TID, OWNER, draft["id"])))


def test_other_periods_and_tenants_unaffected(env):
    close_hard(env)
    e = post(env, OWNER, "2026-02-15", expense("2026-02-15"))  # February is open
    assert e["status"] == "posted"
    e2 = post(env, OWNER2, "2026-01-15", expense())            # other tenant's January is open
    assert e2["status"] == "posted"


# ==================== soft close: warn + override ====================
def test_soft_close_override_flow(env):
    res = run(PC.close_period(env, TID, OWNER, JAN))  # defaults: soft stage ON, grace 7
    assert res["period"]["status"] == "soft_closed"
    assert res["period"]["hard_lock_at"] is not None

    expect_status(409, lambda: post(env, OWNER, "2026-01-15", expense()))
    entry = post(env, OWNER, "2026-01-15", expense(),
                 period_override_reason="Late vendor invoice for January")
    assert entry["period_override_reason"] == "Late vendor invoice for January"
    audit = [a for a in env.audit_log.docs if a.get("action") == "period.soft_override"]
    assert len(audit) == 1 and audit[0]["details"]["reason"].startswith("Late vendor")


def test_auto_hard_lock_after_grace(env):
    res = run(PC.close_period(env, TID, OWNER, JAN, now=T0))
    assert res["period"]["hard_lock_at"] == "2026-02-08T09:00:00+00:00"
    # time travel: resolve with a clock past the grace deadline
    p = run(L.resolve_period(env, TID, period(env), now="2026-02-09T00:00:00+00:00"))
    assert p["status"] == "locked" and p["auto_locked"] is True
    assert period(env)["status"] == "locked"
    # and the chokepoint now refuses (state-based: hard_lock_at is past)
    expect_status(423, lambda: post(env, OWNER, "2026-01-15", expense()))


def test_lock_now_from_soft(env):
    run(PC.close_period(env, TID, OWNER, JAN))
    locked = run(PC.lock_now(env, TID, OWNER, JAN))
    assert locked["status"] == "locked"
    expect_status(409, lambda: run(PC.lock_now(env, TID, OWNER, "2026-03")))


# ==================== checklist + exceptions ====================
def test_checklist_truth_and_acknowledgment_gate(env):
    # fixtures with known contents
    run(L.post_journal_entry(env, TID, OWNER,
                             {"entry_date": "2026-01-05", "lines": expense("2026-01-05")},
                             status="draft"))
    run(L.post_journal_entry(env, TID, OWNER,
                             {"entry_date": "2026-01-06", "lines": expense("2026-01-06")},
                             status="draft"))
    txns = Scoped(env.bank_transactions, TID)
    run(txns.insert_one({"id": "t1", "txn_date": "2026-01-11", "status": "unmatched",
                         "reconciled": False, "amount_cents": 500}))
    run(txns.insert_one({"id": "t2", "txn_date": "2026-01-12", "status": "cleared",
                         "reconciled": False, "amount_cents": 700}))
    run(txns.insert_one({"id": "t3", "txn_date": "2026-01-13", "status": "cleared",
                         "reconciled": True, "amount_cents": 900}))
    run(bills_router.create_bill(bills_router.BillBody(
        vendor="Rent", expense_code="5300", amount=40.0, bill_date="2026-01-10"), OWNER))

    cl = run(PC.compute_checklist(env, TID, JAN))
    counts = {i["key"]: i["count"] for i in cl["items"]}
    assert counts == {"draft_entries": 2, "unposted_bank_txns": 1,
                      "unreconciled_bank_txns": 1, "open_bills": 1, "open_invoices": 0,
                      "out_of_balance_reconciliations": 0}
    assert any("cannot be checked" in n for n in cl["notes"])  # honest limits stated

    expect_status(422, lambda: run(PC.close_period(env, TID, OWNER, JAN)))
    expect_status(422, lambda: run(PC.close_period(
        env, TID, OWNER, JAN, acknowledge=["draft_entries"])))
    res = run(PC.close_period(env, TID, OWNER, JAN,
                              acknowledge=["draft_entries", "unposted_bank_txns",
                                           "unreconciled_bank_txns"]))
    stored = res["period"]["close_checklist"]
    assert stored["acknowledged"] == ["draft_entries", "unposted_bank_txns",
                                      "unreconciled_bank_txns"]
    assert stored["acknowledged_by"] == "owner@test.com"


def test_out_of_balance_reconciliation_blocks_close(env):
    # Safeguard gap 5: a reconciliation dated in the period that
    # finished out of balance (status in_progress, difference != 0)
    # blocks the close like any other blocking checklist item.
    run(Scoped(env.reconciliations, TID).insert_one({
        "id": "rec-1", "bank_account_id": "ba-1",
        "statement_date": "2026-01-31", "status": "in_progress",
        "difference_cents": -1500, "created_at": T0}))
    cl = run(PC.compute_checklist(env, TID, JAN))
    item = next(i for i in cl["items"]
                if i["key"] == "out_of_balance_reconciliations")
    assert item["blocking"] is True and item["count"] == 1
    expect_status(422, lambda: run(PC.close_period(env, TID, OWNER, JAN)))
    res = run(PC.close_period(
        env, TID, OWNER, JAN,
        acknowledge=["out_of_balance_reconciliations"]))
    assert res["period"]["close_checklist"]["acknowledged"] == [
        "out_of_balance_reconciliations"]


def test_completed_reconciliation_does_not_block_close(env):
    run(Scoped(env.reconciliations, TID).insert_one({
        "id": "rec-2", "bank_account_id": "ba-1",
        "statement_date": "2026-01-31", "status": "completed",
        "difference_cents": 0, "created_at": T0}))
    cl = run(PC.compute_checklist(env, TID, JAN))
    item = next(i for i in cl["items"]
                if i["key"] == "out_of_balance_reconciliations")
    assert item["count"] == 0
    close_hard(env)
    assert period(env)["status"] == "locked"


# ==================== snapshots ====================
def test_snapshot_frozen_and_immutable_across_reclose(env):
    post(env, OWNER, "2026-01-10", expense("2026-01-10"))                       # -100.00 cash, +100.00 expense
    post(env, OWNER, "2026-01-12", [{"account_code": "1020", "debit": 250_00, "credit": 0},
                                    {"account_code": "4090", "debit": 0, "credit": 250_00}])
    res = close_hard(env)
    snap = res["snapshot"]
    assert snap is not None and snap["kind"] == "close"
    tb = snap["trial_balance"]
    assert tb["balanced"] and tb["total_debit"] == 25_000 and tb["total_credit"] == 25_000
    assert snap["pnl"]["net_income"] == 15_000

    # reopen, change the books, re-close: a NEW snapshot; the first is untouched
    run(PC.reopen_period(env, TID, OWNER, JAN, reason="Fix a miscoded expense",
                         now="2026-12-02T09:00:00+00:00"))
    # accrued expense (Dr expense / Cr AP): moves the TB column totals,
    # unlike a bank-paid expense, which only shifts asset -> expense
    post(env, OWNER, "2026-01-20", [{"account_code": "5300", "debit": 50_00, "credit": 0},
                                    {"account_code": "2010", "debit": 0, "credit": 50_00}])
    res2 = run(PC.close_period(env, TID, OWNER, JAN, grace_days=0,
                               now="2026-12-02T10:00:00+00:00"))
    assert res2["snapshot"]["kind"] == "reclose"
    assert res2["snapshot"]["trial_balance"]["total_debit"] == 30_000
    snaps = run(PC.list_snapshots(env, TID, JAN))
    assert len(snaps) == 2
    assert snaps[0]["trial_balance"]["total_debit"] == 25_000  # frozen forever
    assert period(env)["times_closed"] == 2


# ==================== reopen apparatus ====================
def test_reopen_reason_timer_tagging_spotlight_notifications(env):
    close_hard(env)
    expect_status(403, lambda: run(PC.reopen_period(env, TID, BKPR, JAN, reason="please")))
    expect_status(422, lambda: run(PC.reopen_period(env, TID, OWNER, JAN, reason="  ")))
    p = run(PC.reopen_period(env, TID, ACCT, JAN, reason="Client sent a missing invoice",
                             window_hours=2, now="2026-12-02T09:00:00+00:00"))
    assert p["reopen"]["status"] == "active"
    assert p["reopen"]["closes_at"] == "2026-12-02T11:00:00+00:00"
    rid = p["reopen"]["id"]

    # notification went to the owner seat, not the actor (accountant), not the bookkeeper
    notes = env.app_notifications.docs
    assert len(notes) == 1 and notes[0]["user_id"] == OWNER["id"]
    assert notes[0]["type"] == "period_reopened"

    # posting inside the window is allowed and tagged
    e = post(env, OWNER, "2026-01-15", expense())
    assert e["reopen_id"] == rid

    view = run(PC.changes_after_close(env, TID, JAN))
    assert view["reopen_count"] == 1
    grp = view["periods"][0]["reopens"][0]
    assert grp["reopen"]["reason"] == "Client sent a missing invoice"
    assert grp["reopen"]["by_name"] == "Adam Accountant"
    assert [x["id"] for x in grp["entries"]] == [e["id"]]

    # window expiry re-locks (state-based time travel), and posting is refused again
    doc = period(env)
    doc["reopen"]["closes_at"] = "2026-11-01T00:00:00+00:00"  # stored doc, mutated in place
    expect_status(423, lambda: post(env, OWNER, "2026-01-16", expense()))
    assert period(env)["status"] == "locked"
    rec = next(r for r in env.period_reopens.docs if r["id"] == rid)
    assert rec["status"] == "expired" and rec["end_reason"] == "window_expired"


def test_end_reopen_relocks_manually(env):
    run(PC.close_period(env, TID, OWNER, JAN, now="2026-12-01T09:00:00+00:00"))  # soft
    run(PC.reopen_period(env, TID, OWNER, JAN, reason="Quick correction",
                         now="2026-12-02T09:00:00+00:00"))
    p = run(PC.end_reopen(env, TID, OWNER, JAN, now="2026-12-02T09:30:00+00:00"))
    assert p["status"] == "soft_closed"  # back to its previous stage
    assert p["reopen"]["status"] == "ended"


# ==================== the owner's three switches ====================
def _set_switches(env, **patch):
    return run(PC.update_settings(env, TID, OWNER, patch))


def test_switches_default_on_for_fresh_tenant(env):
    s = run(PC.get_settings(env, TID))
    assert s == {"snapshot_on_close": True, "soft_close_stage": True,
                 "reopen_apparatus": True, "grace_days": 7, "reopen_window_hours": 24}


def test_switch_snapshot_off(env):
    _set_switches(env, snapshot_on_close=False)
    res = close_hard(env)
    assert res["snapshot"] is None
    assert env.period_snapshots.docs == []          # verifiably absent
    assert period(env)["status"] == "locked"        # the lock itself unaffected


def test_switch_soft_stage_off_goes_straight_to_hard(env):
    _set_switches(env, soft_close_stage=False)
    res = run(PC.close_period(env, TID, OWNER, JAN))  # grace still defaults to 7
    assert res["period"]["status"] == "locked"
    assert res["period"]["hard_lock_at"] is None
    expect_status(423, lambda: post(env, OWNER, "2026-01-15", expense()))


def test_switch_reopen_apparatus_off(env):
    _set_switches(env, reopen_apparatus=False)
    close_hard(env)
    p = run(PC.reopen_period(env, TID, OWNER, JAN))  # no reason needed, none recorded
    assert p["status"] == "open" and not p.get("reopen")
    assert env.period_reopens.docs == []             # no apparatus records
    assert env.app_notifications.docs == []          # no notifications
    e = post(env, OWNER, "2026-01-15", expense())
    assert not e.get("reopen_id")                    # no tagging
    view = run(PC.changes_after_close(env, TID))
    assert view["reopen_count"] == 0                 # spotlight empty


def test_grace_zero_goes_straight_to_hard_even_with_soft_on(env):
    res = run(PC.close_period(env, TID, OWNER, JAN, grace_days=0))
    assert res["period"]["status"] == "locked"


def test_settings_validation(env):
    expect_status(422, lambda: _set_switches(env, grace_days=31))
    expect_status(422, lambda: _set_switches(env, reopen_window_hours=0))
    expect_status(422, lambda: _set_switches(env, snapshot_on_close="yes"))
    s = _set_switches(env, grace_days=3, snapshot_on_close=False)
    assert s["grace_days"] == 3 and s["snapshot_on_close"] is False
    assert s["soft_close_stage"] is True  # untouched switches keep their values


# ==================== legacy status setter delegates ====================
def test_legacy_setter_cannot_silently_reopen(env):
    close_hard(env)
    expect_status(409, lambda: run(ledger_router.set_period_status(
        JAN, ledger_router.PeriodAction(status="open"), OWNER)))
    assert period(env)["status"] == "locked"


def test_legacy_setter_close_flows_through_workflow(env):
    out = run(ledger_router.set_period_status(
        JAN, ledger_router.PeriodAction(status="locked"), OWNER))
    assert out["status"] == "locked"
    assert len(env.period_snapshots.docs) == 1  # snapshot taken even via the old door
