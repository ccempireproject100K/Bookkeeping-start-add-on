"""Duplicate-entry guards (safeguard audit 2026-11-19, gap 2).

Native invoice issue, bill create, and unkeyed payments had no
duplicate detection: double-entering a bill silently posted it twice.
The guards in lib/duplicates.py now 409 with the customer pattern's
shape (the detail names the existing record), BEFORE any journal
entry posts. Internal flows with their own dedupe (recurring
materialization, splits, PO conversion) pass allow_duplicate=True.

Direct-call tests: no mongod, no network. The REAL ledger posts
against an in-memory FakeDB so "no journal entry on a refused
duplicate" is proven from the journal store itself.
"""
import asyncio
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException
from pymongo import ReturnDocument

from lib.base import today_str
from routers import invoices as inv_router
from routers import bills as bills_router
import lib.audit as audit_mod
import lib.developer as dev_mod

TID = "tenant-dup"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Olivia Owner",
        "role": "owner", "tenant_id": TID}
TODAY = today_str()


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
                if op == "$in":
                    if val is _MISSING or val not in operand:
                        return False
                elif op == "$ne":
                    if val is not _MISSING and val == operand:
                        return False
                elif op == "$exists":
                    if (val is not _MISSING) != bool(operand):
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
    for mod in (inv_router, bills_router, audit_mod, dev_mod):
        monkeypatch.setattr(mod, "db", db)
    db.tenants.docs.append({"id": TID, "name": "Dup Test Co",
                            "currency": "USD"})
    # The ledger refuses to post to accounts outside the chart (I3).
    for code, typ in (("1010", "asset"), ("1020", "asset"),
                      ("1200", "asset"), ("2010", "liability"),
                      ("4020", "revenue"), ("5700", "expense")):
        db.accounts.docs.append({"id": f"acc-{code}", "tenant_id": TID,
                                 "code": code, "name": code, "type": typ})
    return db


def _posted_entries(db):
    return [e for e in db.journal_entries.docs if e.get("status") == "posted"]


# ============================ Bills ============================

def _bill_body(**kw):
    base = {"vendor": "Supply Co", "amount": 250.0,
            "bill_date": "2026-11-10"}
    base.update(kw)
    return bills_router.BillBody(**base)


def test_double_entered_bill_is_blocked(env):
    first = run(bills_router.create_bill(_bill_body(), USER))
    assert first["status"] == "open"
    entries_before = len(_posted_entries(env))
    with pytest.raises(HTTPException) as ei:
        run(bills_router.create_bill(_bill_body(), USER))
    assert ei.value.status_code == 409
    assert ei.value.detail["existing_id"] == first["id"]
    # The refusal happened BEFORE posting: no second bill, no second
    # journal entry. Double-entering a bill no longer posts twice.
    assert len(env.bills.docs) == 1
    assert len(_posted_entries(env)) == entries_before


def test_bill_same_ref_blocked_even_with_new_date(env):
    run(bills_router.create_bill(_bill_body(bill_ref="INV-778"), USER))
    with pytest.raises(HTTPException) as ei:
        run(bills_router.create_bill(
            _bill_body(bill_ref="INV-778", bill_date="2026-11-12"), USER))
    assert ei.value.status_code == 409
    assert ei.value.detail["existing_bill_ref"] == "INV-778"


def test_bill_variants_are_not_duplicates(env):
    run(bills_router.create_bill(_bill_body(), USER))
    # Different amount, different date, different vendor: all fine.
    run(bills_router.create_bill(_bill_body(amount=300.0), USER))
    run(bills_router.create_bill(_bill_body(bill_date="2026-11-11"), USER))
    run(bills_router.create_bill(_bill_body(vendor="Other Co"), USER))
    assert len(env.bills.docs) == 4


def test_bill_duplicate_override(env):
    run(bills_router.create_bill(_bill_body(), USER))
    second = run(bills_router.create_bill(
        _bill_body(allow_duplicate=True), USER))
    assert second["id"]
    assert len(env.bills.docs) == 2


def test_bill_payment_duplicate_blocked(env):
    bill = run(bills_router.create_bill(_bill_body(amount=500.0), USER))
    pay = bills_router.PayBody(amount=200.0, pay_date="2026-11-15")
    r1 = run(bills_router.pay_bill(bill["id"], pay, USER))
    assert r1["paid_cents"] == 20000
    entries_before = len(_posted_entries(env))
    with pytest.raises(HTTPException) as ei:
        run(bills_router.pay_bill(
            bill["id"], bills_router.PayBody(amount=200.0,
                                             pay_date="2026-11-15"), USER))
    assert ei.value.status_code == 409
    assert ei.value.detail["message"] == "Possible duplicate payment"
    assert len(_posted_entries(env)) == entries_before
    # A genuinely different second installment still works.
    r2 = run(bills_router.pay_bill(
        bill["id"], bills_router.PayBody(amount=200.0,
                                         pay_date="2026-11-16"), USER))
    assert r2["paid_cents"] == 40000


# ============================ Invoices ============================

def _inv_body(**kw):
    base = {"client_name": "Ann A",
            "lines": [inv_router.InvLine(name="Service", amount=100.0)]}
    base.update(kw)
    return inv_router.InvoiceBody(**base)


def test_double_issued_invoice_is_blocked(env):
    first = run(inv_router.issue_invoice(_inv_body(), USER))
    entries_before = len(_posted_entries(env))
    with pytest.raises(HTTPException) as ei:
        run(inv_router.issue_invoice(_inv_body(), USER))
    assert ei.value.status_code == 409
    assert ei.value.detail["existing_id"] == first["id"]
    assert len(env.invoices.docs) == 1
    assert len(_posted_entries(env)) == entries_before


def test_invoice_variants_are_not_duplicates(env):
    run(inv_router.issue_invoice(_inv_body(), USER))
    run(inv_router.issue_invoice(_inv_body(client_name="Bob B"), USER))
    run(inv_router.issue_invoice(_inv_body(
        lines=[inv_router.InvLine(name="Service", amount=150.0)]), USER))
    assert len(env.invoices.docs) == 3


def test_invoice_duplicate_override(env):
    run(inv_router.issue_invoice(_inv_body(), USER))
    run(inv_router.issue_invoice(_inv_body(allow_duplicate=True), USER))
    assert len(env.invoices.docs) == 2


def test_invoice_payment_duplicate_blocked(env):
    inv = run(inv_router.issue_invoice(_inv_body(
        lines=[inv_router.InvLine(name="Service", amount=500.0)]), USER))
    pay = inv_router.PayBody(amount=200.0, pay_date="2026-11-15")
    r1 = run(inv_router._pay_invoice_inner(inv["id"], pay, USER, None, _Sess()))
    assert r1["paid_cents"] == 20000
    entries_before = len(_posted_entries(env))
    with pytest.raises(HTTPException) as ei:
        run(inv_router._pay_invoice_inner(
            inv["id"], inv_router.PayBody(amount=200.0,
                                          pay_date="2026-11-15"),
            USER, None, _Sess()))
    assert ei.value.status_code == 409
    assert ei.value.detail["message"] == "Possible duplicate payment"
    assert len(_posted_entries(env)) == entries_before
    # A different installment on another day still posts.
    r2 = run(inv_router._pay_invoice_inner(
        inv["id"], inv_router.PayBody(amount=200.0, pay_date="2026-11-16"),
        USER, None, _Sess()))
    assert r2["paid_cents"] == 40000


class _Sess:
    """The no-key idempotency session shape _pay_invoice_inner needs."""
    is_replay = False

    async def complete(self, result, resource_id=None):
        return result

    async def fail(self, status, error):
        return None
