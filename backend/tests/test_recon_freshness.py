"""Reconciliation truthfulness (safeguard audit 2026-11-19, gap 5).

- The dashboard health score read `bank_reconciliations`, a
  collection no code writes; it now reads the real store
  (db.reconciliations, anchored on statement_date).
- The bank→entry match endpoint linked any entry by bare ID; it now
  requires a posted entry carrying the transaction's amount.
(Out-of-balance reconciliations blocking period close are covered in
test_period_close_lock.py.)

Direct-call tests against an in-memory FakeDB; no mongod, no network.
"""
import asyncio
import copy
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException
from pymongo import ReturnDocument

from lib.base import today_str
from lib import ledger as L
from routers import dashboard as DASH
from routers import banking as BK
import lib.audit as audit_mod

TID = "tenant-recon"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Olivia Owner",
        "role": "owner", "tenant_id": TID}
TODAY = date.fromisoformat(today_str())


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
                elif op == "$gte":
                    if val is _MISSING or not val >= operand:
                        return False
                elif op == "$lte":
                    if val is _MISSING or not val <= operand:
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
    for mod in (DASH, BK, audit_mod):
        monkeypatch.setattr(mod, "db", db)

    async def _health(db_, tid):
        return {"counts": {"unbalanced_entries": 0}}
    monkeypatch.setattr(L, "run_health_check", _health)
    return db


# ============================ Dashboard freshness ============================

def test_health_score_reads_real_reconciliation_store(env):
    stmt = (TODAY - timedelta(days=5)).isoformat()
    env.reconciliations.docs.append({
        "id": "rec-1", "tenant_id": TID, "bank_account_id": "ba-1",
        "statement_date": stmt, "status": "completed",
        "difference_cents": 0, "created_at": stmt + "T10:00:00+00:00"})
    out = run(DASH.health_score(USER))
    # Five days since the statement date: full freshness credit, and
    # crucially NOT the "no reconciliation on record" flag the old
    # wrong-collection read always produced.
    assert out["metrics"]["days_since_reconciliation"] == 5
    assert out["components"]["reconciliation"] == 25
    assert not any("No bank reconciliation" in f for f in out["flags"])


def test_health_score_no_reconciliation_still_flags(env):
    out = run(DASH.health_score(USER))
    assert any("No bank reconciliation on record" in f for f in out["flags"])


# ============================ Match amount check ============================

def _seed_txn(env, amount=-5000):
    env.bank_transactions.docs.append({
        "id": "txn-1", "tenant_id": TID, "status": "unmatched",
        "amount_cents": amount, "txn_date": today_str(),
        "description": "Card purchase"})


def _seed_entry(env, eid="je-1", total=5000, status="posted"):
    env.journal_entries.docs.append({
        "id": eid, "tenant_id": TID, "number": "JE-2026-000001",
        "status": status, "total_cents": total,
        "entry_date": today_str()})


def test_match_accepts_equal_amount(env):
    _seed_txn(env)
    _seed_entry(env)
    out = run(BK.match("txn-1", "je-1", USER))
    assert out["status"] == "matched"
    assert env.bank_transactions.docs[0]["matched_entry_id"] == "je-1"


def test_match_refuses_wrong_amount(env):
    _seed_txn(env)
    _seed_entry(env, total=7000)
    with pytest.raises(HTTPException) as ei:
        run(BK.match("txn-1", "je-1", USER))
    assert ei.value.status_code == 422
    assert "amount" in ei.value.detail
    assert env.bank_transactions.docs[0]["status"] == "unmatched"


def test_match_accepts_composite_entry_with_matching_leg(env):
    _seed_txn(env)
    _seed_entry(env, total=9000)
    env.journal_lines.docs += [
        {"id": "jl-1", "tenant_id": TID, "entry_id": "je-1",
         "account_code": "1020", "debit": 0, "credit": 5000,
         "status": "posted"},
        {"id": "jl-2", "tenant_id": TID, "entry_id": "je-1",
         "account_code": "5700", "debit": 5000, "credit": 0,
         "status": "posted"},
        {"id": "jl-3", "tenant_id": TID, "entry_id": "je-1",
         "account_code": "6100", "debit": 4000, "credit": 0,
         "status": "posted"},
        {"id": "jl-4", "tenant_id": TID, "entry_id": "je-1",
         "account_code": "1020", "debit": 0, "credit": 4000,
         "status": "posted"},
    ]
    out = run(BK.match("txn-1", "je-1", USER))
    assert out["status"] == "matched"


def test_match_refuses_draft_entry(env):
    _seed_txn(env)
    _seed_entry(env, status="draft")
    with pytest.raises(HTTPException) as ei:
        run(BK.match("txn-1", "je-1", USER))
    assert ei.value.status_code == 422
