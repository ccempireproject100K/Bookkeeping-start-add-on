"""Audit-chain production wiring (safeguard audit 2026-11-19, gap 4).

Before this pass: verify_audit_chain was called only by its own
test; period-close events (lib/ledger._period_audit) and time-billing
events (routers/time_billing._audit) wrote raw inserts that bypassed
the hash chain; vendor and bank-transaction hard deletes wrote no
audit row at all. Now the writers chain, the deletes audit, and
lib/integrity.run_integrity_pass verifies the chain and records the
outcome (it runs at startup and daily from the scheduler).

Direct-call tests against an in-memory FakeDB; no mongod, no network.
"""
import asyncio
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from pymongo import ReturnDocument

from lib import audit as AU
from lib import integrity
from lib import ledger as L
from routers import time_billing as TB
from routers import vendors as V
from routers import banking as BK

TID = "tenant-audit"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Olivia Owner",
        "role": "owner", "tenant_id": TID}


def run(coro):
    return asyncio.run(coro)


# ============================ FakeDB ============================

_MISSING = object()


def _matches(doc, query):
    for key, cond in (query or {}).items():
        val = doc.get(key, _MISSING)
        if isinstance(cond, dict) and cond and all(
                k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if op == "$exists":
                    if (val is not _MISSING) != bool(operand):
                        return False
                elif op == "$in":
                    if val is _MISSING or val not in operand:
                        return False
                elif op == "$ne":
                    if val is not _MISSING and val == operand:
                        return False
                elif op == "$or":
                    if not any(_matches(doc, sub) for sub in operand):
                        return False
                else:
                    raise NotImplementedError(f"FakeDB: operator {op}")
        else:
            if val is _MISSING or val != cond:
                return False
    return True


def _apply_update(doc, update, inserting=False):
    for op, fields in (update or {}).items():
        if op == "$set":
            for k, v in fields.items():
                doc[k] = copy.deepcopy(v)
        elif op == "$setOnInsert":
            if inserting:
                for k, v in fields.items():
                    doc.setdefault(k, copy.deepcopy(v))
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = (doc.get(k) or 0) + v
        elif op == "$push":
            for k, v in fields.items():
                doc.setdefault(k, []).append(copy.deepcopy(v))
        elif op == "$unset":
            for k in fields:
                doc.pop(k, None)
        else:
            raise NotImplementedError(f"FakeDB: update operator {op}")


def _project(doc, projection):
    out = copy.deepcopy(doc)
    if projection:
        for k, v in projection.items():
            if not v:
                out.pop(k, None)
    return out


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec, direction=None):
        if isinstance(spec, str):
            items = [(spec, 1 if direction is None else direction)]
        elif isinstance(spec, tuple):
            items = [spec]
        else:
            items = list(spec)
        for field, dirn in reversed(items):
            self._docs.sort(key=lambda d: (d.get(field) is None, d.get(field)),
                            reverse=(dirn < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)


class _Result:
    def __init__(self, n=1):
        self.deleted_count = n
        self.modified_count = n
        self.matched_count = n


class FakeCollection:
    def __init__(self):
        self.docs = []

    def find(self, query=None, projection=None):
        return FakeCursor([_project(d, projection) for d in self.docs
                           if _matches(d, query)])

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return _project(d, projection)
        return None

    async def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))
        return _Result(1)

    async def insert_many(self, docs):
        self.docs.extend(copy.deepcopy(list(docs)))
        return _Result(len(docs))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return _Result(1)
        if upsert:
            doc = {k: v for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(doc, update, inserting=True)
            self.docs.append(doc)
            return _Result(1)
        return _Result(0)

    async def find_one_and_update(self, query, update, upsert=False,
                                  return_document=None):
        for d in self.docs:
            if _matches(d, query):
                before = copy.deepcopy(d)
                _apply_update(d, update)
                return before if return_document == ReturnDocument.BEFORE \
                    else copy.deepcopy(d)
        if upsert:
            doc = {k: v for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(doc, update, inserting=True)
            self.docs.append(doc)
            return copy.deepcopy(doc)
        return None

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return _Result(1)
        return _Result(0)

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, query)]
        return _Result(before - len(self.docs))

    async def count_documents(self, query=None):
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


@pytest.fixture()
def env(monkeypatch):
    db = FakeDB()
    for mod in (TB, V, BK, AU):
        monkeypatch.setattr(mod, "db", db)
    return db


def _chained(db, action):
    return [r for r in db.audit_log.docs
            if r.get("action") == action and "chain_seq" in r]


# ============================ Chained writers ============================

def test_period_audit_rows_join_the_chain(env):
    run(L._period_audit(env, TID, USER, "period.closed", "2026-11",
                        {"via": "checklist"}))
    rows = _chained(env, "period.closed")
    assert len(rows) == 1
    assert rows[0]["entity"] == "fiscal_period"
    assert rows[0]["tenant_id"] == TID
    result = run(AU.verify_audit_chain(env))
    assert result["ok"] and result["checked"] == 1


def test_time_billing_audit_rows_join_the_chain(env):
    run(TB._audit(USER, "time_billing.issue", "invoice", "inv-1",
                  {"count": 2}))
    rows = _chained(env, "time_billing.issue")
    assert len(rows) == 1
    result = run(AU.verify_audit_chain(env))
    assert result["ok"] and result["checked"] == 1


# ============================ Hard deletes are audited ============================

def test_vendor_delete_is_audited(env):
    env.vendors.docs.append({"id": "ven-1", "tenant_id": TID,
                             "name": "Supply Co"})
    out = run(V.delete_vendor("ven-1", False, USER))
    assert out == {"ok": True, "deleted_id": "ven-1"}
    assert env.vendors.docs == []
    rows = _chained(env, "vendor.delete")
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "ven-1"
    assert rows[0]["details"]["name"] == "Supply Co"
    result = run(AU.verify_audit_chain(env))
    assert result["ok"]


def test_bank_transaction_delete_is_audited(env):
    env.bank_transactions.docs.append(
        {"id": "txn-1", "tenant_id": TID, "status": "unmatched",
         "amount_cents": -4200, "txn_date": "2026-11-05",
         "description": "Card purchase"})
    out = run(BK.delete_txn("txn-1", USER))
    assert out == {"deleted": True}
    assert env.bank_transactions.docs == []
    rows = _chained(env, "bank_transaction.delete")
    assert len(rows) == 1
    assert rows[0]["details"]["amount_cents"] == -4200
    result = run(AU.verify_audit_chain(env))
    assert result["ok"]


# ============================ The integrity pass ============================

def test_integrity_pass_records_ok_then_detects_tamper(env):
    run(AU.audit(USER, "invoice.create", "invoice", "inv-1", {"n": 1}))
    run(AU.audit(USER, "invoice.pay", "invoice", "inv-1", {"n": 2}))
    row = run(integrity.run_integrity_pass(env))
    assert row["ok"] is True and row["checked"] == 2
    recorded = [d for d in env.integrity_checks.docs
                if d["kind"] == "audit_chain"]
    assert len(recorded) == 1 and recorded[0]["ok"] is True

    # Tamper with history: the next pass must report the break.
    env.audit_log.docs[0]["details"] = {"n": 999999}
    row2 = run(integrity.run_integrity_pass(env))
    assert row2["ok"] is False
    assert row2["break"] and row2["break"]["seq"] == 1
    recorded = [d for d in env.integrity_checks.docs
                if d["kind"] == "audit_chain"]
    assert len(recorded) == 2 and recorded[1]["ok"] is False
