"""Cross-tenant IDOR probes (security audit 2026-10-26).

The audit's crown-jewels question: can a signed-in user of business A
read or mutate business B's money records by guessing ids? These
probes call the real router functions as tenant A's owner against
records owned by tenant B and require BOTH a 404 (or, for the one
delete that answers unconditionally, a verified no-op) AND that the
victim record is byte-for-byte unchanged afterwards. Positive controls
prove the same calls succeed on tenant A's own records, so a green
run cannot be an artifact of everything 404ing.

The invoices router's Idempotency-Key layer is stubbed (key=None path
in production skips it; it has its own suite in
tests/test_integration_sync.py) — tenant isolation is the subject
here. No live MongoDB: the db is faked in memory, following
tests/test_invoice_payments.py conventions.

Run from the backend directory with:
    python3 -m pytest tests/test_tenant_isolation_probes.py -n 0 -q
"""
import asyncio
import copy
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_isolation_probes")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException

import lib.audit as audit_mod
from routers import backup as BACKUP
from routers import banking as BANK
from routers import bills as BILLS
from routers import customers as CUST
from routers import invoices as INV
from routers import ledger as LED
from routers import receipts as RCPT


# ------------------------------------------------------------------ fakes

def _get(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict):
            if "$in" in v and _get(doc, k) not in v["$in"]:
                return False
            if "$ne" in v and _get(doc, k) == v["$ne"]:
                return False
            if "$gte" in v and not (_get(doc, k) is not None
                                    and _get(doc, k) >= v["$gte"]):
                return False
            if "$lte" in v and not (_get(doc, k) is not None
                                    and _get(doc, k) <= v["$lte"]):
                return False
            if "$exists" in v and ((_get(doc, k) is not None)
                                   != bool(v["$exists"])):
                return False
        elif _get(doc, k) != v:
            return False
    return True


def _apply(doc, update):
    for op, changes in (update or {}).items():
        if op == "$set":
            for k, v in changes.items():
                parts = k.split(".")
                cur = doc
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = v
        elif op == "$inc":
            for k, v in changes.items():
                doc[k] = doc.get(k, 0) + v
        elif op == "$unset":
            for k in changes:
                doc.pop(k, None)
        elif op == "$push":
            for k, v in changes.items():
                doc.setdefault(k, []).append(v)


class _Result:
    def __init__(self, n):
        self.matched_count = n
        self.modified_count = n
        self.deleted_count = n


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, spec, direction=-1):
        if isinstance(spec, list):
            for key, dirn in reversed(spec):
                self._docs.sort(key=lambda d: _get(d, key) or "",
                                reverse=(dirn < 0))
        else:
            self._docs.sort(key=lambda d: _get(d, spec) or "",
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return self._docs


class FakeColl:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                out = dict(d)
                if projection and projection.get("_id") == 0:
                    out.pop("_id", None)
                return out
        return None

    def find(self, q=None, projection=None):
        return _Cursor([dict(d) for d in self.docs if _matches(d, q)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if _matches(d, q):
                _apply(d, update)
                return _Result(1)
        return _Result(0)

    async def update_many(self, q, update):
        n = 0
        for d in self.docs:
            if _matches(d, q):
                _apply(d, update)
                n += 1
        return _Result(n)

    async def find_one_and_update(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                before = dict(d)
                _apply(d, update)
                return before
        return None

    async def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if _matches(d, q):
                del self.docs[i]
                return _Result(1)
        return _Result(0)

    async def delete_many(self, q):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, q)]
        return _Result(before - len(self.docs))

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q))

    def aggregate(self, pipeline):
        return _Cursor([])


class FakeDB:
    def __getattr__(self, name):
        coll = FakeColl()
        setattr(self, name, coll)
        return coll

    def __getitem__(self, name):
        return getattr(self, name)


A = "tenant-a"
B = "tenant-b"
ATTACKER = {"id": "u-a", "tenant_id": A, "email": "a@a.example.com",
            "name": "A Owner", "role": "owner"}

VICTIMS = {
    "invoices": {"id": "inv-b", "tenant_id": B, "customer_id": "cust-b",
                 "client_name": "B Client", "total_cents": 50000,
                 "paid_cents": 0, "status": "issued", "payments": [],
                 "currency": "USD", "created_at": "2026-10-01"},
    "customer_credits": {"id": "cred-b", "tenant_id": B,
                         "client_name": "B Client", "amount_cents": 5000,
                         "remaining_cents": 5000, "status": "open"},
    "bills": {"id": "bill-b", "tenant_id": B, "vendor_name": "B Vendor",
              "total_cents": 20000, "paid_cents": 0, "status": "open"},
    "journal_entries": {"id": "je-b", "tenant_id": B, "status": "draft",
                        "entry_date": "2026-10-01", "number": None,
                        "memo": "B draft"},
    "bank_accounts": {"id": "ba-b", "tenant_id": B, "name": "B Checking",
                      "gl_code": "1010"},
    "bank_transactions": {"id": "txn-b", "tenant_id": B,
                          "bank_account_id": "ba-b", "amount_cents": -4200,
                          "status": "uncategorized",
                          "description": "B txn"},
    "bank_rules": {"id": "rule-b", "tenant_id": B, "contains": "B",
                   "category_code": "5700"},
    "customers": {"id": "cust-b", "tenant_id": B, "name": "B Client",
                  "email": "b@b.example.com"},
    "receipts": {"id": "rcpt-b", "tenant_id": B, "stored": "rcpt-b.jpg",
                 "filename": "b.jpg", "content_type": "image/jpeg",
                 "status": "uploaded"},
}


def _idem_stub():
    class _Sess:
        is_replay = False

        async def complete(self, result, resource_id=None):
            return result

        async def fail(self, status, detail):
            return None

        def replay_response(self):
            raise AssertionError("no replay without a key")

    async def _begin(**kwargs):
        return _Sess()

    return SimpleNamespace(begin=_begin,
                           request_hash=lambda *a, **k: "hash")


@pytest.fixture()
def fdb(monkeypatch):
    db = FakeDB()
    for mod in (INV, BILLS, LED, BANK, CUST, RCPT, BACKUP, audit_mod):
        monkeypatch.setattr(mod, "db", db)
    monkeypatch.setattr(INV, "idem", _idem_stub())
    for coll_name, doc in VICTIMS.items():
        getattr(db, coll_name).docs.append(copy.deepcopy(doc))
    db.invoices.docs.append(
        {"id": "inv-a", "tenant_id": A, "customer_id": "cust-a",
         "client_name": "A Client", "total_cents": 1000, "paid_cents": 0,
         "status": "issued", "payments": [], "currency": "USD",
         "created_at": "2026-10-02"})
    db.customers.docs.append(
        {"id": "cust-a", "tenant_id": A, "name": "A Client",
         "email": "a@a.example.com"})
    db.journal_entries.docs.append(
        {"id": "je-a", "tenant_id": A, "status": "draft",
         "entry_date": "2026-10-02", "number": None, "memo": "A draft"})
    return db


def _victim(fdb, coll):
    return getattr(fdb, coll).docs[
        [d["id"] for d in getattr(fdb, coll).docs].index(VICTIMS[coll]["id"])]


def _assert_404(fn, fdb, coll):
    before = copy.deepcopy(_victim(fdb, coll))
    with pytest.raises(HTTPException) as e:
        asyncio.run(fn())
    assert e.value.status_code == 404
    assert _victim(fdb, coll) == before  # untouched, byte for byte


# ------------------------------------------------------------------ probes

def test_pay_other_tenants_invoice_404(fdb):
    _assert_404(lambda: INV.pay_invoice(
        "inv-b", INV.PayBody(amount=500.0), dict(ATTACKER), None),
        fdb, "invoices")


def test_refund_other_tenants_invoice_404(fdb):
    _assert_404(lambda: INV.refund_invoice_payment(
        "inv-b", INV.InvoiceRefundBody(amount=10.0), dict(ATTACKER), None),
        fdb, "invoices")


def test_refund_other_tenants_credit_404(fdb):
    _assert_404(lambda: INV.refund_credit(
        "cred-b", INV.RefundBody(), dict(ATTACKER)),
        fdb, "customer_credits")


def test_invoice_list_shows_only_own(fdb):
    rows = asyncio.run(INV.list_invoices(dict(ATTACKER)))
    ids = [r["id"] for r in rows]
    assert ids == ["inv-a"]


def test_pay_other_tenants_bill_404(fdb):
    _assert_404(lambda: BILLS.pay_bill(
        "bill-b", BILLS.PayBody(amount=200.0), dict(ATTACKER)),
        fdb, "bills")


def test_get_other_tenants_journal_entry_404(fdb):
    _assert_404(lambda: LED.get_entry("je-b", dict(ATTACKER)),
                fdb, "journal_entries")


def test_post_other_tenants_draft_404(fdb):
    _assert_404(lambda: LED.post_entry("je-b", dict(ATTACKER)),
                fdb, "journal_entries")


def test_reverse_other_tenants_entry_404(fdb):
    _assert_404(lambda: LED.reverse("je-b", dict(ATTACKER)),
                fdb, "journal_entries")


def test_categorize_other_tenants_txn_404(fdb):
    _assert_404(lambda: BANK.categorize(
        "txn-b", BANK.CategorizeBody(category_code="5700"),
        dict(ATTACKER)), fdb, "bank_transactions")


def test_delete_other_tenants_txn_404(fdb):
    _assert_404(lambda: BANK.delete_txn("txn-b", dict(ATTACKER)),
                fdb, "bank_transactions")


def test_transfer_from_other_tenants_account_404(fdb):
    _assert_404(lambda: BANK.transfer(
        BANK.TransferBody(from_account_id="ba-b", to_account_id="ba-b",
                          amount=10.0), dict(ATTACKER)),
        fdb, "bank_accounts")


def test_delete_other_tenants_rule_is_noop(fdb):
    before = copy.deepcopy(_victim(fdb, "bank_rules"))
    out = asyncio.run(BANK.delete_rule("rule-b", dict(ATTACKER)))
    assert out == {"deleted": True}  # unconditional answer…
    assert _victim(fdb, "bank_rules") == before  # …but nothing deleted


def test_get_other_tenants_customer_404(fdb):
    _assert_404(lambda: CUST.get_customer("cust-b", dict(ATTACKER)),
                fdb, "customers")


def test_update_other_tenants_customer_404(fdb):
    _assert_404(lambda: CUST.update_customer(
        "cust-b", CUST.CustomerBody(name="Hacked"), dict(ATTACKER)),
        fdb, "customers")


def test_delete_other_tenants_customer_404(fdb):
    _assert_404(lambda: CUST.delete_customer("cust-b", dict(ATTACKER)),
                fdb, "customers")


def test_statement_other_tenants_customer_404(fdb):
    _assert_404(lambda: CUST.statement("cust-b", dict(ATTACKER)),
                fdb, "customers")


def test_get_other_tenants_receipt_404(fdb):
    _assert_404(lambda: RCPT.get_receipt("rcpt-b", dict(ATTACKER)),
                fdb, "receipts")


def test_backup_export_contains_only_own_tenant(fdb):
    resp = asyncio.run(BACKUP.export_backup(
        encrypted=False, offsite=False, user=dict(ATTACKER)))
    dump = json.loads(resp.body.decode())
    assert dump["tenant_id"] == A
    inv_ids = [d["id"] for d in dump["collections"]["invoices"]]
    assert inv_ids == ["inv-a"]


# ------------------------------------------------------- positive controls

def test_positive_controls_own_records(fdb):
    cust = asyncio.run(CUST.get_customer("cust-a", dict(ATTACKER)))
    assert cust["name"] == "A Client"
    entry = asyncio.run(LED.get_entry("je-a", dict(ATTACKER)))
    assert entry["memo"] == "A draft"
    stmt = asyncio.run(CUST.statement("cust-a", dict(ATTACKER)))
    assert stmt is not None
