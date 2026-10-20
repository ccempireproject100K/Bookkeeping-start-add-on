"""Audit wave-1 defect fixes (parity audit 2026-10-20):

  1. AP/AR aging dropped partially-paid bills/invoices (routers/aging.py
     selected only status "open"/"issued" at full face value).
  2. routers/vendors.py stored vendor TINs in PLAINTEXT and ran a duplicate
     1099 computation; TINs are now Fernet-encrypted like contractor TINs
     in routers/tax.py, and /vendors/1099 delegates to the tax system.
  3. The scheduler never ran recurring invoices or Plaid syncs; both are
     wired into lib/scheduler.py, and Plaid webhooks kick an immediate
     plan-gated sync for the owning tenant.
  4. The PWA was uninstallable (no manifest / service worker); static
     contract checks for the manifest, worker, and registration wiring.

Run from the backend directory with: python -m pytest tests/test_audit_wave1_fixes.py
No live MongoDB or Plaid keys needed — the db is faked in-memory and the
Plaid HTTP layer is mocked.
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_audit_wave1")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

from lib.pii import pii_decrypt, pii_encrypt, last4  # noqa: E402
from routers import aging as AG  # noqa: E402
from routers import auth as AUTH  # noqa: E402
from routers import bankfeed as BF  # noqa: E402
from routers import banking as BK  # noqa: E402
from routers import recurring_invoices as RI  # noqa: E402
from routers import tax as TX  # noqa: E402
from routers import vendors as V  # noqa: E402
from lib import scheduler as S  # noqa: E402


# ---------------------------------------------------------------- fakes

def _matches(doc, q):
    for k, v in (q or {}).items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in v):
                return False
            continue
        if isinstance(v, dict):
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
            if "$gte" in v and (doc.get(k) or "") < v["$gte"]:
                return False
            if "$lte" in v and (doc.get(k) or "") > v["$lte"]:
                return False
            if "$ne" in v and doc.get(k) == v["$ne"]:
                return False
            if "$exists" in v and (k in doc) != bool(v["$exists"]):
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, spec):
        if spec:
            for key, direction in reversed(spec):
                self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                                reverse=(direction < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs)

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _Coll:
    """Fake motor collection: the subset aging/vendors/tax/bankfeed/
    scheduler use. Enforces unique `id` (DuplicateKeyError) — that is what
    the scheduler_runs claim idiom relies on."""

    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([dict(d) for d in self.docs if _matches(d, q)])

    async def insert_one(self, doc):
        doc = dict(doc)
        if doc.get("id") is not None and any(d.get("id") == doc["id"] for d in self.docs):
            raise DuplicateKeyError(f"duplicate id {doc['id']}")
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    async def update_one(self, q, update):
        n = 0
        for d in self.docs:
            if _matches(d, q):
                n += 1
                for op, changes in update.items():
                    if op == "$set":
                        d.update(changes)
                    elif op == "$unset":
                        for k in changes:
                            d.pop(k, None)
                    elif op == "$inc":
                        for k, delta in changes.items():
                            d[k] = d.get(k, 0) + delta
                    elif op == "$push":
                        for k, val in changes.items():
                            d.setdefault(k, []).append(val)
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def delete_one(self, q):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, q)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q))

    def aggregate(self, pipeline):
        rows = [dict(d) for d in self.docs]
        for stage in pipeline:
            if "$match" in stage:
                rows = [d for d in rows if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                field = spec["total"]["$sum"].lstrip("$")
                rows = [{"_id": spec.get("_id"),
                         "total": sum(d.get(field, 0) for d in rows)}]
        return _Cursor(rows)


def _db(**colls):
    base = {name: _Coll() for name in (
        "bills", "invoices", "vendors", "expenses", "vendor_credits",
        "contractors", "contractor_payments", "audit_log", "tenants",
        "scheduler_runs", "scheduler_locks", "bank_connections",
        "bank_transactions", "bank_accounts", "bank_rules", "bank_webhooks",
        "users", "recurring_invoices")}
    base.update(colls)
    return SimpleNamespace(**base)


USER = {"id": "u1", "tenant_id": "t1", "name": "Test",
        "email": "t@example.com", "role": "owner"}


def _patch_all(monkeypatch, fdb):
    """Point every module under test at the same fake db (in production
    they all share the one `db` object from db.py)."""
    for mod in (AG, V, TX, BF, BK, AUTH, S):
        monkeypatch.setattr(mod, "db", fdb)
    return fdb


# ================================================================ 1. aging

@pytest.fixture
def aging_db(monkeypatch):
    fdb = _patch_all(monkeypatch, _db())
    fdb.bills.docs.extend([
        {"id": "b-open", "tenant_id": "t1", "vendor": "Open Co",
         "amount_cents": 100_00, "paid_cents": 0, "status": "open",
         "due_date": "2026-10-01", "issue_entry": 11},
        {"id": "b-part", "tenant_id": "t1", "vendor": "Partial Co",
         "amount_cents": 100_00, "paid_cents": 40_00, "status": "partial",
         "due_date": "2026-08-01", "issue_entry": 12},
        {"id": "b-paid", "tenant_id": "t1", "vendor": "Paid Co",
         "amount_cents": 50_00, "paid_cents": 50_00, "status": "paid",
         "due_date": "2026-08-01", "issue_entry": 13},
        {"id": "b-other-tenant", "tenant_id": "tX", "vendor": "Not Ours",
         "amount_cents": 999_00, "paid_cents": 0, "status": "partial",
         "due_date": "2026-08-01", "issue_entry": 14},
    ])
    fdb.invoices.docs.extend([
        {"id": "i-issued", "tenant_id": "t1", "client_name": "Client A",
         "total_cents": 80_00, "paid_cents": 0, "status": "issued",
         "due_date": "2026-10-05", "issue_entry": 21},
        {"id": "i-part", "tenant_id": "t1", "client_name": "Client B",
         "total_cents": 50_00, "paid_cents": 20_00, "status": "partial",
         "due_date": "2026-10-05", "issue_entry": 22},
        {"id": "i-paid", "tenant_id": "t1", "client_name": "Client C",
         "total_cents": 10_00, "paid_cents": 10_00, "status": "paid",
         "due_date": "2026-10-05", "issue_entry": 23},
    ])
    return fdb


def test_ap_aging_includes_partially_paid_bill_at_remaining_balance(aging_db):
    res = asyncio.run(AG._ap("t1", "2026-10-20"))
    by_name = {i["name"]: i for i in res["items"]}
    assert set(by_name) == {"Open Co", "Partial Co"}  # paid + other tenant out
    part = by_name["Partial Co"]
    assert part["amount_cents"] == 60_00          # remaining, not face value
    assert part["billed_cents"] == 100_00
    assert part["paid_cents"] == 40_00
    assert part["bucket"] == "61-90"               # due 2026-08-01, 80 days
    assert by_name["Open Co"]["amount_cents"] == 100_00
    assert res["total_cents"] == 160_00
    assert res["buckets"]["61-90"] == 60.0


def test_ar_aging_includes_partially_paid_invoice_at_remaining_balance(aging_db):
    res = asyncio.run(AG._ar("t1", "2026-10-20"))
    by_name = {i["name"]: i for i in res["items"]}
    assert set(by_name) == {"Client A", "Client B"}
    assert by_name["Client B"]["amount_cents"] == 30_00
    assert by_name["Client B"]["paid_cents"] == 20_00
    assert res["total_cents"] == 110_00


# ================================================================ 2. vendor TINs + 1099

@pytest.fixture
def vendor_db(monkeypatch):
    return _patch_all(monkeypatch, _db())


def _vendor_body(**kw):
    return V.VendorBody(**{"name": "Sub Co", "is_1099": True,
                           "tax_id": "12-3456789", **kw})


def test_vendor_tin_encrypted_at_rest_and_masked_in_response(vendor_db):
    resp = asyncio.run(V.create_vendor(_vendor_body(), USER))
    assert "tax_id" not in resp and "tax_id_enc" not in resp
    assert resp["tax_id_last4"] == "6789"
    assert resp["tax_id_masked"] == "•••6789"
    stored = vendor_db.vendors.docs[0]
    assert "tax_id" not in stored                        # no plaintext at rest
    assert stored["tax_id_enc"] != "12-3456789"
    assert pii_decrypt(stored["tax_id_enc"]) == "12-3456789"
    assert "12-3456789" not in json.dumps(resp)


def test_vendor_list_never_returns_full_tin(vendor_db):
    asyncio.run(V.create_vendor(_vendor_body(), USER))
    rows = asyncio.run(V.list_vendors(USER))
    assert len(rows) == 1
    assert "12-3456789" not in json.dumps(rows)
    assert rows[0]["tax_id_masked"] == "•••6789"


def test_legacy_plaintext_vendor_tin_tolerated_and_reencrypted_on_read(vendor_db):
    vendor_db.vendors.docs.append({
        "id": "v-legacy", "tenant_id": "t1", "name": "Legacy Co",
        "is_1099": True, "tax_id": "98-7654321"})        # pre-fix plaintext doc
    rows = asyncio.run(V.list_vendors(USER))
    assert rows[0]["tax_id_masked"] == "•••4321"
    assert "98-7654321" not in json.dumps(rows)
    stored = vendor_db.vendors.docs[0]
    assert "tax_id" not in stored                        # lazily re-encrypted
    assert pii_decrypt(stored["tax_id_enc"]) == "98-7654321"
    assert stored["tax_id_last4"] == "4321"


def test_vendor_update_keeps_tin_unless_a_new_one_is_supplied(vendor_db):
    resp = asyncio.run(V.create_vendor(_vendor_body(), USER))
    vid = resp["id"]
    out = asyncio.run(V.update_vendor(
        vid, V.VendorBody(name="Sub Co", is_1099=True, tax_id=None), USER))
    assert out["tax_id_masked"] == "•••6789"             # kept
    out = asyncio.run(V.update_vendor(
        vid, V.VendorBody(name="Sub Co", is_1099=True, tax_id="55-4443333"), USER))
    assert out["tax_id_last4"] == "3333"                 # replaced
    stored = vendor_db.vendors.docs[0]
    assert pii_decrypt(stored["tax_id_enc"]) == "55-4443333"


def test_auth_pii_migration_sweep_encrypts_vendor_tins(vendor_db):
    vendor_db.vendors.docs.append({
        "_id": "mongo-1", "id": "v-legacy", "tenant_id": "t1",
        "name": "Legacy Co", "is_1099": True, "tax_id": "98-7654321"})
    res = asyncio.run(AUTH.migrate_encrypt_pii(USER))
    assert res["vendors_encrypted"] == 1
    stored = vendor_db.vendors.docs[0]
    assert "tax_id" not in stored
    assert pii_decrypt(stored["tax_id_enc"]) == "98-7654321"


def _seed_contractor(fdb, cid, name, entity_type="individual", tin="123-45-6789"):
    fdb.contractors.docs.append({
        "id": cid, "tenant_id": "t1", "name": name, "email": "", "phone": "",
        "business_name": "", "entity_type": entity_type,
        "tax_id_enc": pii_encrypt(tin) if tin else "",
        "tax_id_last4": last4(tin) if tin else "",
        "address": {}, "w9_received": True, "w9_received_date": "2026-01-10",
        "efile": {}})


def test_vendors_1099_delegates_to_tax_contractor_system(vendor_db):
    _seed_contractor(vendor_db, "c1", "Jane Sub")
    _seed_contractor(vendor_db, "c2", "Corp Sub", entity_type="c_corp")
    vendor_db.contractor_payments.docs.extend([
        {"id": "p1", "tenant_id": "t1", "contractor_id": "c1",
         "date": "2026-03-01", "amount_cents": 700_00},
        {"id": "p2", "tenant_id": "t1", "contractor_id": "c1",
         "date": "2025-03-01", "amount_cents": 100_00},   # other year: excluded
        {"id": "p3", "tenant_id": "t1", "contractor_id": "c2",
         "date": "2026-03-01", "amount_cents": 900_00},
    ])
    # A bill + expense in the OLD duplicate system's shape must NOT leak in.
    vendor_db.bills.docs.append({
        "id": "b1", "tenant_id": "t1", "vendor": "Jane Sub", "status": "paid",
        "amount_cents": 5000_00, "payments": [
            {"date": "2026-04-01", "cash_cents": 5000_00}]})
    rep = asyncio.run(V.report_1099("2026", USER))
    by_name = {r["name"]: r for r in rep["vendors"]}
    assert by_name["Jane Sub"]["paid_cents"] == 700_00    # tax.py totals only
    assert by_name["Jane Sub"]["reportable"] is True      # >= $600, individual
    assert by_name["Jane Sub"]["tax_id_masked"] == "•••6789"
    assert by_name["Corp Sub"]["paid_cents"] == 900_00
    assert by_name["Corp Sub"]["reportable"] is False     # corp exempt (tax.py rule)
    assert rep["total"] == 1600.0
    assert "123-45-6789" not in json.dumps(rep)


# ================================================================ 3. scheduler wiring

@pytest.fixture
def sched_db(monkeypatch):
    fdb = _patch_all(monkeypatch, _db())
    fdb.tenants.docs.extend([{"id": "t1"}, {"id": "t2"}])
    return fdb


def test_scheduler_recurring_invoices_invokes_runner_once_per_tenant(sched_db,
                                                                     monkeypatch):
    calls = []

    async def fake_run_due(user):
        calls.append(user["tenant_id"])
        return {"generated": 2, "invoices": [{"id": "x"}, {"id": "y"}]}

    monkeypatch.setattr(RI, "run_due_for_user", fake_run_due)
    out = asyncio.run(S.run_recurring_invoices())
    assert sorted(calls) == ["t1", "t2"]
    assert out["tenants"] == 2 and out["generated"] == 4
    assert all(d["status"] == "done" for d in sched_db.scheduler_runs.docs)
    # Same hour: claims block a second pass — runner is NOT invoked again.
    out2 = asyncio.run(S.run_recurring_invoices())
    assert len(calls) == 2
    assert len(out2["skipped"]) == 2
    assert all(s["reason"] == "already_ran_this_hour" for s in out2["skipped"])


def test_scheduler_bank_sync_respects_plan_gate_and_claims(sched_db, monkeypatch):
    sched_db.tenants.docs[1].update({"plan": "light",
                                     "trial_ends_at": "2020-01-01T00:00:00+00:00"})
    sched_db.bank_connections.docs.append(
        {"id": "conn-t1", "tenant_id": "t1", "item_id": "item-t1"})
    calls = []

    async def fake_sync(user, connection_id=""):
        calls.append(user["tenant_id"])
        return {"added": 3, "duplicates": 1}

    monkeypatch.setattr(BF, "sync_connections", fake_sync)
    out = asyncio.run(S.run_bank_sync())
    assert calls == ["t1"]                                # t2 plan-gated out
    assert out["tenants"] == 1 and out["added"] == 3 and out["duplicates"] == 1
    assert any(s["tenant_id"] == "t2" and s["reason"] == "plan_gate"
               for s in out["skipped"])
    out2 = asyncio.run(S.run_bank_sync())                 # same window: claimed
    assert calls == ["t1"]
    assert any(s["reason"] == "already_synced_this_window" for s in out2["skipped"])


# ================================================================ 3b. Plaid paths

@pytest.fixture
def plaid_db(monkeypatch):
    fdb = _patch_all(monkeypatch, _db())
    monkeypatch.setenv("PLAID_CLIENT_ID", "plaid-client")
    monkeypatch.setenv("PLAID_SECRET", "plaid-secret")
    return fdb


def _seed_connection(fdb, tenant_id="t1", item_id="item-1"):
    fdb.bank_connections.docs.append({
        "id": "conn1", "tenant_id": tenant_id, "item_id": item_id,
        "access_token_enc": BF._fernet().encrypt(b"access-token").decode(),
        "institution": "ins_1", "accounts": [], "cursor": "",
        "linked": [{"plaid_account_id": "pa1", "bank_account_id": "ba1"}],
        "created_at": "2026-01-01T00:00:00+00:00"})


def test_sync_connections_pulls_pages_and_dedupes(plaid_db, monkeypatch):
    _seed_connection(plaid_db)
    page = {"added": [{
        "transaction_id": "tx1", "account_id": "pa1", "amount": 25.50,
        "date": "2026-10-01", "name": "Coffee Shop", "merchant_name": "Coffee",
        "pending": False,
        "personal_finance_category": {"primary": "FOOD_AND_DRINK"}}],
        "modified": [], "next_cursor": "cur2", "has_more": False}

    async def fake_post(base, path, payload, cid, sec):
        assert path == "/transactions/sync"
        return page

    monkeypatch.setattr(BF, "_plaid_post", fake_post)
    user = {**USER, "id": "ops"}
    res = asyncio.run(BF.sync_connections(user))
    assert res == {"added": 1, "duplicates": 0}
    txn = plaid_db.bank_transactions.docs[0]
    assert txn["amount_cents"] == -2550                   # Plaid out -> our negative
    assert txn["bank_account_id"] == "ba1" and txn["source"] == "plaid"
    conn = plaid_db.bank_connections.docs[0]
    assert conn["cursor"] == "cur2" and conn["last_sync"]     # cursor persisted
    res2 = asyncio.run(BF.sync_connections(user))             # replay: deduped
    assert res2 == {"added": 0, "duplicates": 1}
    assert len(plaid_db.bank_transactions.docs) == 1


def test_sync_item_plan_gate_and_unknown_item(plaid_db, monkeypatch):
    _seed_connection(plaid_db, tenant_id="t-light", item_id="item-light")
    plaid_db.tenants.docs.append(
        {"id": "t-light", "plan": "light",
         "trial_ends_at": "2020-01-01T00:00:00+00:00"})
    res = asyncio.run(BF.sync_item("item-light"))
    assert res == {"synced": False, "reason": "plan_gate"}
    res = asyncio.run(BF.sync_item("item-nope"))
    assert res == {"synced": False, "reason": "unknown_item"}
    # Entitled tenant: syncs.
    _seed_connection(plaid_db, tenant_id="t-med", item_id="item-med")
    plaid_db.tenants.docs.append({"id": "t-med"})
    calls = []

    async def fake_sync(user, connection_id=""):
        calls.append(user["tenant_id"])
        return {"added": 4, "duplicates": 0}

    monkeypatch.setattr(BF, "sync_connections", fake_sync)
    res = asyncio.run(BF.sync_item("item-med"))
    assert res == {"synced": True, "added": 4, "duplicates": 0}
    assert calls == ["t-med"]


class _Req:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()
        self.headers = {}

    async def body(self):
        return self._body


def test_webhook_kicks_sync_for_transactions_item(plaid_db, monkeypatch):
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)
    kicked = []

    async def fake_sync_item(item_id):
        kicked.append(item_id)
        return {"synced": True, "added": 1, "duplicates": 0}

    monkeypatch.setattr(BF, "sync_item", fake_sync_item)

    async def main():
        out = await BF.webhook(_Req({"webhook_type": "TRANSACTIONS",
                                     "webhook_code": "SYNC_UPDATES_AVAILABLE",
                                     "item_id": "item-1"}))
        await asyncio.sleep(0.2)   # let the fire-and-forget task land
        return out

    assert asyncio.run(main()) == {"ok": True}
    assert kicked == ["item-1"]
    assert plaid_db.bank_webhooks.docs[0]["item_id"] == "item-1"
    # A non-transactions webhook is logged but kicks nothing.
    kicked.clear()

    async def main2():
        await BF.webhook(_Req({"webhook_type": "ITEM",
                               "webhook_code": "ERROR", "item_id": "item-1"}))
        await asyncio.sleep(0.1)

    asyncio.run(main2())
    assert kicked == []


# ================================================================ 4. PWA contract

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_pwa_manifest_is_valid_and_complete():
    manifest = json.loads((FRONTEND / "public" / "manifest.json").read_text())
    assert manifest["name"] and manifest["short_name"]
    assert manifest["start_url"] and manifest["display"] == "standalone"
    assert manifest["theme_color"] == "#0B0D10"
    assert manifest["background_color"] == "#0B0D10"
    purposes = {i["purpose"] for i in manifest["icons"]}
    assert {"any", "maskable"} <= purposes
    for icon in manifest["icons"]:
        assert (FRONTEND / "public" / icon["src"]).exists(), icon["src"]


def test_pwa_service_worker_strategy():
    sw = (FRONTEND / "public" / "service-worker.js").read_text()
    assert "addEventListener(\"fetch\"" in sw or "addEventListener('fetch'" in sw
    assert "/api/" in sw                       # API carved out explicitly
    assert "fetch(request)" in sw              # network-first for API
    assert "caches.open" in sw                 # app-shell cache
    assert "mae-books-" in sw                  # versioned cache name


def test_pwa_registration_wired():
    index_html = (FRONTEND / "public" / "index.html").read_text()
    assert 'rel="manifest"' in index_html and "manifest.json" in index_html
    entry = (FRONTEND / "src" / "index.js").read_text()
    assert "serviceWorkerRegistration.register()" in entry
    reg = (FRONTEND / "src" / "serviceWorkerRegistration.js").read_text()
    assert "service-worker.js" in reg and "NODE_ENV" in reg  # production-only
