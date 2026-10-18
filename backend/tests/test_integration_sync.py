"""Track A audit tests: integration credentials + Idempotency-Key
enforcement on the Momo sync surface (POST /customers, POST /invoices,
POST /invoices/{id}/pay).

No live Mongo needed — an in-memory fake db (with unique-index
emulation, which the idempotency claim relies on) is monkeypatched into
every module that holds a `db` reference, following the conventions of
tests/test_stripe_integration.py / test_audit_quick_wins.py. The real
endpoint functions, the real ledger posting path, and the real auth
dependencies run against the fake.

Run from the backend directory with:
    python -m pytest tests/test_integration_sync.py -n 0
"""
import asyncio
import copy
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_integration_sync")
os.environ.setdefault("JWT_SECRET", "integration-sync-test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

import lib.audit as audit_mod  # noqa: E402
import lib.auth as auth_mod  # noqa: E402
import lib.idempotency as idem  # noqa: E402
import lib.integration_auth as iauth  # noqa: E402
import routers.customers as customers_r  # noqa: E402
import routers.integrations as integrations_r  # noqa: E402
import routers.invoices as invoices_r  # noqa: E402

T1 = "tenant-1"
T2 = "tenant-2"


# ---------------- fake in-memory db ----------------
def _matches(doc, q):
    for k, v in (q or {}).items():
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


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs = sorted(
            self._docs,
            key=lambda d: (d.get(key) is None, str(d.get(key))),
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
    """Minimal motor stand-in. insert_one enforces the unique key sets
    it was constructed with — synchronously, so the check+insert is
    atomic under the event loop exactly as a Mongo unique index is."""

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
                    raise DuplicateKeyError(
                        f"unique index violation on {keys}")

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
            else:  # replacement-style update
                doc.update(copy.deepcopy(update))
                return

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


class FakeDB:
    def __init__(self):
        self._colls = {
            "idempotency_records": FakeCollection(
                unique_keys=[("tenant_id", "key")]),
            "integration_credentials": FakeCollection(
                unique_keys=[("client_id",)]),
        }

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())


# ---------------- fixtures / helpers ----------------
ACCOUNT_CODES = ["1200", "4020", "1050", "1010", "1020", "2210"]


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
    for mod in (customers_r, invoices_r, integrations_r, idem, iauth,
                auth_mod, audit_mod):
        monkeypatch.setattr(mod, "db", fake)
    return fake


def run(coro):
    return asyncio.run(coro)


def req(headers=None, path="/api/customers", cookies=None):
    return SimpleNamespace(headers=headers or {}, cookies=cookies or {},
                           url=SimpleNamespace(path=path))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def issue_credential(fdb, tenant=T1, label="Momo sync"):
    owner = make_user(tenant)

    async def _go():
        return await integrations_r.issue_credential(
            integrations_r.IssueBody(label=label), user=owner)
    return run(_go())


def sync_actor_for(credential, path="/api/customers"):
    async def _go():
        return await iauth.get_sync_actor(req(bearer(credential), path))
    return run(_go())


def create_customer(actor, name="Acme Co", email="a@acme.example",
                    phone="555-0100", key=None, path="/api/customers"):
    headers = {"Idempotency-Key": key} if key else {}

    async def _go():
        return await customers_r.create_customer(
            customers_r.CustomerBody(name=name, email=email, phone=phone),
            user=actor, request=req(headers, path))
    return run(_go())


def issue_invoice(actor, key=None, amount=100.0, client="Acme Co",
                  path="/api/invoices"):
    headers = {"Idempotency-Key": key} if key else {}
    body = invoices_r.InvoiceBody(
        client_name=client,
        lines=[invoices_r.InvLine(name="Service", amount=amount)],
        tax=0)

    async def _go():
        return await invoices_r.issue_invoice(
            body, user=actor, request=req(headers, path))
    return run(_go())


def pay_invoice(actor, invoice_id, key=None, amount=100.0,
                method="card"):
    headers = {"Idempotency-Key": key} if key else {}
    body = invoices_r.PayBody(payment_method=method, amount=amount)

    async def _go():
        return await invoices_r.pay_invoice(
            invoice_id, body, user=actor,
            request=req(headers, f"/api/invoices/{invoice_id}/pay"))
    return run(_go())


def coll_docs(fdb, name, tenant=None):
    docs = fdb._colls[name].docs
    if tenant:
        docs = [d for d in docs if d.get("tenant_id") == tenant]
    return docs


# ---------------- credential lifecycle ----------------
class TestCredentialLifecycle:
    def test_issue_shows_secret_once_and_stores_only_hash(self, fdb):
        out = issue_credential(fdb)
        assert out["credential"].startswith("mae1.ci_")
        assert out["client_id"] in out["credential"]
        stored = coll_docs(fdb, "integration_credentials")[0]
        secret = out["credential"].rsplit(".", 1)[1]
        assert stored["secret_hash"] != secret
        assert secret not in str(stored)
        assert stored["tenant_id"] == T1
        # list view carries no secret material
        async def _list():
            return await integrations_r.list_credentials(
                user=make_user(T1))
        listed = run(_list())
        assert len(listed) == 1
        assert "credential" not in listed[0]
        assert "secret_hash" not in listed[0]

    def test_credential_authenticates_and_health_check(self, fdb):
        out = issue_credential(fdb)
        actor = sync_actor_for(out["credential"])
        assert actor["tenant_id"] == T1
        assert actor["role"] == "integration"
        assert actor["auth_kind"] == "integration"

        async def _me():
            return await integrations_r.integration_me(actor=actor)
        me = run(_me())
        assert me["ok"] is True
        assert me["tenant_id"] == T1
        assert me["tenant_name"] == f"Shop {T1}"
        stored = coll_docs(fdb, "integration_credentials")[0]
        assert stored["last_used_at"] is not None

    def test_revoked_credential_rejected(self, fdb):
        out = issue_credential(fdb)
        run(_revoke(out["id"]))
        with pytest.raises(HTTPException) as ei:
            sync_actor_for(out["credential"])
        assert ei.value.status_code == 401
        assert "revoked" in str(ei.value.detail).lower()

    def test_rotation_kills_old_secret(self, fdb):
        out = issue_credential(fdb)
        old = out["credential"]

        async def _rotate():
            return await integrations_r.rotate_credential(
                out["id"], user=make_user(T1))
        rotated = run(_rotate())
        assert rotated["credential"] != old
        assert rotated["client_id"] == out["client_id"]
        with pytest.raises(HTTPException) as ei:
            sync_actor_for(old)
        assert ei.value.status_code == 401
        actor = sync_actor_for(rotated["credential"])
        assert actor["tenant_id"] == T1

    def test_expired_credential_rejected(self, fdb):
        out = issue_credential(fdb)
        stored = coll_docs(fdb, "integration_credentials")[0]
        stored["expires_at"] = "2020-01-01T00:00:00+00:00"
        with pytest.raises(HTTPException) as ei:
            sync_actor_for(out["credential"])
        assert ei.value.status_code == 401
        assert "expired" in str(ei.value.detail).lower()

    def test_bad_secret_rejected_generically(self, fdb):
        out = issue_credential(fdb)
        bad = out["credential"][:-2] + "xx"
        with pytest.raises(HTTPException) as ei:
            sync_actor_for(bad)
        assert ei.value.status_code == 401

    def test_non_owner_role_gate_rejects_issuance(self, fdb):
        # The management endpoints hang off require("ledger:admin");
        # exercise that exact dependency with a non-owner user.
        dep = auth_mod.require("ledger:admin")
        bookkeeper = make_user(T1, role="bookkeeper")

        async def _go():
            return await dep(user=bookkeeper)
        with pytest.raises(HTTPException) as ei:
            run(_go())
        assert ei.value.status_code == 403


async def _revoke(cred_id):
    return await integrations_r.revoke_credential(
        cred_id, user=make_user(T1))


# ---------------- tenant isolation ----------------
class TestTenantIsolation:
    def test_credential_writes_only_its_own_tenant(self, fdb):
        cred1 = issue_credential(fdb, T1)["credential"]
        actor1 = sync_actor_for(cred1)
        doc = create_customer(actor1, key="k-cust-1")
        assert doc["name"] == "Acme Co"
        rows = coll_docs(fdb, "customers", T1)
        assert len(rows) == 1
        assert coll_docs(fdb, "customers", T2) == []

    def test_owner_of_other_tenant_cannot_revoke(self, fdb):
        out = issue_credential(fdb, T1)
        with pytest.raises(HTTPException) as ei:
            run(integrations_r.revoke_credential(
                out["id"], user=make_user(T2)))
        assert ei.value.status_code == 404
        # still usable by its own tenant afterwards
        assert sync_actor_for(out["credential"])["tenant_id"] == T1

    def test_same_key_string_is_independent_per_tenant(self, fdb):
        a1 = sync_actor_for(issue_credential(fdb, T1)["credential"])
        a2 = sync_actor_for(issue_credential(fdb, T2)["credential"])
        d1 = create_customer(a1, key="shared-key")
        d2 = create_customer(a2, key="shared-key")
        assert d1["id"] != d2["id"]
        assert len(coll_docs(fdb, "customers", T1)) == 1
        assert len(coll_docs(fdb, "customers", T2)) == 1
        assert len(coll_docs(fdb, "idempotency_records")) == 2


# ---------------- least privilege / user auth untouched ----------------
class TestLeastPrivilege:
    def test_integration_token_rejected_by_user_auth(self, fdb):
        cred = issue_credential(fdb)["credential"]

        async def _go():
            return await auth_mod.get_current_user(req(bearer(cred)))
        with pytest.raises(HTTPException) as ei:
            run(_go())
        assert ei.value.status_code == 401

    def test_user_jwt_rejected_on_integration_health(self, fdb):
        token = auth_mod.create_access_token("user-tenant-1",
                                             "owner@tenant-1.example", 0)

        async def _go():
            return await iauth.get_integration_actor(req(bearer(token)))
        with pytest.raises(HTTPException) as ei:
            run(_go())
        assert ei.value.status_code == 401

    def test_jwt_user_flow_unchanged(self, fdb):
        token = auth_mod.create_access_token("user-tenant-1",
                                             "owner@tenant-1.example", 0)

        async def _via_header():
            return await iauth.get_sync_actor(
                req(bearer(token), "/api/invoices"))

        async def _via_cookie():
            return await iauth.get_sync_actor(
                req({}, "/api/invoices", cookies={"access_token": token}))
        actor = run(_via_header())
        assert actor["role"] == "owner" and actor["tenant_id"] == T1
        assert run(_via_cookie())["id"] == "user-tenant-1"

    def test_non_owner_user_cannot_use_sync_surface(self, fdb):
        bookkeeper = make_user(T1, role="bookkeeper", uid="user-bk",
                               email="bk@tenant-1.example")
        coll_docs(fdb, "users").append(bookkeeper)
        token = auth_mod.create_access_token("user-bk",
                                             "bk@tenant-1.example", 0)

        async def _go():
            return await iauth.get_sync_actor(req(bearer(token)))
        with pytest.raises(HTTPException) as ei:
            run(_go())
        assert ei.value.status_code == 403


# ---------------- idempotency: customers ----------------
class TestCustomerIdempotency:
    def test_duplicate_push_replays_without_duplicating(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        first = create_customer(actor, key="cust-key-1")
        second = create_customer(actor, key="cust-key-1")
        # second call is a replayed JSONResponse
        assert getattr(second, "status_code", 200) == 200
        assert second.headers.get("Idempotency-Replayed") == "true"
        import json as _json
        replayed = _json.loads(bytes(second.body))
        assert replayed["id"] == first["id"]
        assert len(coll_docs(fdb, "customers", T1)) == 1
        rec = coll_docs(fdb, "idempotency_records")[0]
        assert rec["status"] == "succeeded"
        assert rec["resource_id"] == first["id"]
        assert rec["actor"].startswith("integration:")

    def test_unkeyed_create_behaves_as_before(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        create_customer(actor, name="One")
        create_customer(actor, name="Two", email="t@x.example")
        assert len(coll_docs(fdb, "customers", T1)) == 2
        assert coll_docs(fdb, "idempotency_records") == []


# ---------------- idempotency: invoices + payments ----------------
class TestInvoicePaymentIdempotency:
    def test_duplicate_invoice_delivery_posts_once(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        first = issue_invoice(actor, key="inv-key-1")
        second = issue_invoice(actor, key="inv-key-1")
        import json as _json
        replayed = _json.loads(bytes(second.body))
        assert replayed["id"] == first["id"]
        assert len(coll_docs(fdb, "invoices", T1)) == 1
        entries = [e for e in coll_docs(fdb, "journal_entries", T1)
                   if e.get("source") == "invoice_issue"]
        assert len(entries) == 1

    def test_duplicate_payment_delivery_posts_once(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        inv = issue_invoice(actor, key="inv-key-2")
        first = pay_invoice(actor, inv["id"], key="pay-key-2")
        assert first["status"] == "paid"
        second = pay_invoice(actor, inv["id"], key="pay-key-2")
        import json as _json
        replayed = _json.loads(bytes(second.body))
        assert replayed == first
        stored = coll_docs(fdb, "invoices", T1)[0]
        assert len(stored["payments"]) == 1
        assert stored["paid_cents"] == 10000
        pays = [e for e in coll_docs(fdb, "journal_entries", T1)
                if e.get("source") == "invoice_paid"]
        assert len(pays) == 1

    def test_same_key_different_payload_is_409(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        issue_invoice(actor, key="inv-key-3", amount=100.0)
        with pytest.raises(HTTPException) as ei:
            issue_invoice(actor, key="inv-key-3", amount=250.0)
        assert ei.value.status_code == 409
        assert len(coll_docs(fdb, "invoices", T1)) == 1

    def test_concurrent_duplicate_submissions_post_once(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])

        async def _both():
            body = invoices_r.InvoiceBody(
                client_name="Acme Co",
                lines=[invoices_r.InvLine(name="Service", amount=100.0)],
                tax=0)
            headers = {"Idempotency-Key": "inv-key-conc"}
            return await asyncio.gather(
                invoices_r.issue_invoice(
                    body, user=actor, request=req(headers, "/api/invoices")),
                invoices_r.issue_invoice(
                    body, user=actor, request=req(headers, "/api/invoices")),
            )
        r1, r2 = run(_both())
        import json as _json
        ids = set()
        for r in (r1, r2):
            if hasattr(r, "body") and getattr(r, "headers", None) is not None \
                    and r.headers.get("Idempotency-Replayed"):
                ids.add(_json.loads(bytes(r.body))["id"])
            else:
                ids.add(r["id"])
        assert len(ids) == 1
        assert len(coll_docs(fdb, "invoices", T1)) == 1
        entries = [e for e in coll_docs(fdb, "journal_entries", T1)
                   if e.get("source") == "invoice_issue"]
        assert len(entries) == 1

    def test_interrupted_then_retried_sync_recovers(self, fdb, monkeypatch):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        import lib.ledger as ledger_mod
        real_post = ledger_mod.post_journal_entry
        state = {"calls": 0}

        async def flaky(*a, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                raise HTTPException(status_code=503,
                                    detail="simulated interruption")
            return await real_post(*a, **kw)
        monkeypatch.setattr(ledger_mod, "post_journal_entry", flaky)

        with pytest.raises(HTTPException) as ei:
            issue_invoice(actor, key="inv-key-retry")
        assert ei.value.status_code == 503
        rec = coll_docs(fdb, "idempotency_records")[0]
        assert rec["status"] == "failed"
        assert "simulated interruption" in rec["error"]
        assert coll_docs(fdb, "invoices", T1) == []

        retried = issue_invoice(actor, key="inv-key-retry")
        assert retried["total_cents"] == 10000
        assert len(coll_docs(fdb, "invoices", T1)) == 1
        entries = [e for e in coll_docs(fdb, "journal_entries", T1)
                   if e.get("source") == "invoice_issue"]
        assert len(entries) == 1
        rec = coll_docs(fdb, "idempotency_records")[0]
        assert rec["status"] == "succeeded"
        assert rec["attempts"] == 2

    def test_crash_window_stamp_returns_existing_resource(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        first = create_customer(actor, key="cust-key-crash")
        # Simulate the crash window: the customer landed (stamped) but
        # the idempotency record never recorded the response.
        rec = fdb._colls["idempotency_records"].docs[0]
        rec["status"] = "failed"
        rec["response_body"] = None
        rec["resource_id"] = None
        again = create_customer(actor, key="cust-key-crash")
        assert again["id"] == first["id"]
        assert len(coll_docs(fdb, "customers", T1)) == 1

    def test_partial_payment_then_duplicate_retry(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        inv = issue_invoice(actor, key="inv-key-4", amount=100.0)
        p1 = pay_invoice(actor, inv["id"], key="pay-key-4a", amount=40.0)
        assert p1["status"] == "partial"
        assert p1["paid_cents"] == 4000
        p1b = pay_invoice(actor, inv["id"], key="pay-key-4a", amount=40.0)
        import json as _json
        assert _json.loads(bytes(p1b.body)) == p1
        stored = coll_docs(fdb, "invoices", T1)[0]
        assert len(stored["payments"]) == 1
        assert stored["paid_cents"] == 4000


# ---------------- sync records / monitoring ----------------
class TestSyncRecords:
    def test_records_capture_success_and_failure(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        create_customer(actor, key="rec-ok")
        with pytest.raises(HTTPException):
            pay_invoice(actor, "missing-invoice", key="rec-fail",
                        amount=10.0)

        async def _records():
            return await integrations_r.sync_records(
                limit=100, user=make_user(T1))
        rows = run(_records())
        by_key = {r["key"]: r for r in rows}
        assert by_key["rec-ok"]["status"] == "succeeded"
        assert by_key["rec-ok"]["endpoint"] == "POST /customers"
        assert by_key["rec-fail"]["status"] == "failed"
        assert by_key["rec-fail"]["response_status"] == 404
        assert "not found" in by_key["rec-fail"]["error"].lower()
        # another tenant sees none of these
        async def _records_t2():
            return await integrations_r.sync_records(
                limit=100, user=make_user(T2))
        assert run(_records_t2()) == []


# ---------------- request hash ----------------
class TestRequestHash:
    def test_hash_is_order_insensitive_but_payload_sensitive(self):
        h1 = idem.request_hash("POST", "/api/invoices",
                               {"a": 1, "b": {"x": 1, "y": 2}})
        h2 = idem.request_hash("POST", "/api/invoices",
                               {"b": {"y": 2, "x": 1}, "a": 1})
        h3 = idem.request_hash("POST", "/api/invoices",
                               {"a": 1, "b": {"x": 1, "y": 3}})
        h4 = idem.request_hash("POST", "/api/invoices/abc/pay",
                               {"a": 1, "b": {"x": 1, "y": 2}})
        assert h1 == h2
        assert h1 != h3
        assert h1 != h4
