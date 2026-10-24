"""Marketplace v1 tests: catalog, developer API keys, signed webhooks.

No live Mongo, no real HTTP — an in-memory fake db is monkeypatched
into every module that holds a `db` reference (the convention of
tests/test_integration_sync.py), the webhook poster is monkeypatched,
and the real endpoint functions are called directly with fake users.
Catalog statuses are driven purely by env vars, flipped per-test.

Run from the backend directory with:
    python -m pytest tests/test_marketplace_developers.py -n 0
"""
import asyncio
import copy
import hashlib
import hmac
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_marketplace_developers")
os.environ.setdefault("JWT_SECRET", "marketplace-test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

import lib.audit as audit_mod  # noqa: E402
import lib.auth as auth_mod  # noqa: E402
import lib.developer as dev_lib  # noqa: E402
import lib.idempotency as idem  # noqa: E402
import lib.integration_auth as iauth  # noqa: E402
import lib.marketplace as mp_lib  # noqa: E402
import routers.customers as customers_r  # noqa: E402
import routers.developers as developers_r  # noqa: E402
import routers.invoices as invoices_r  # noqa: E402
import routers.marketplace as marketplace_r  # noqa: E402

T1 = "tenant-1"
T2 = "tenant-2"

GATED_ENV = (
    "STRIPE_SECRET_KEY", "PLAID_CLIENT_ID", "PLAID_SECRET",
    "TAXBANDITS_CLIENT_ID", "TAXBANDITS_CLIENT_SECRET",
    "TAXBANDITS_USER_TOKEN", "TAXBANDITS_BUSINESS_ID",
    "OPENAI_API_KEY", "SMTP_HOST",
)


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


def _sort_key(doc, key):
    return (doc.get(key) is None, str(doc.get(key)))


class _Result:
    def __init__(self, matched=0, modified=0, deleted=0):
        self.matched_count = matched
        self.modified_count = modified
        self.deleted_count = deleted
        self.inserted_id = None


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        # Accept both motor styles: sort("field", -1) and
        # sort([("field", -1), ...]) (Scoped.find_list uses the latter).
        specs = key if isinstance(key, list) else [(key, direction)]
        for k, d in reversed(specs):
            self._docs = sorted(self._docs, key=lambda doc, kk=k: _sort_key(doc, kk),
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

    async def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if _matches(d, q):
                del self.docs[i]
                return _Result(0, 0, deleted=1)
        return _Result(0, 0, deleted=0)

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
            "api_keys": FakeCollection(unique_keys=[("key_hash",)]),
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
    for name in GATED_ENV:
        monkeypatch.delenv(name, raising=False)
    fake = FakeDB()
    for tenant in (T1, T2):
        fake._colls.setdefault("accounts", FakeCollection()).docs.extend(
            {"id": f"acct-{tenant}-{c}", "tenant_id": tenant, "code": c,
             "archived": False} for c in ACCOUNT_CODES)
        fake._colls.setdefault("tenants", FakeCollection()).docs.append(
            {"id": tenant, "name": f"Shop {tenant}", "settings": {}})
        fake._colls.setdefault("users", FakeCollection()).docs.append(
            make_user(tenant))
    for mod in (customers_r, invoices_r, developers_r, idem, iauth,
                auth_mod, audit_mod, dev_lib):
        monkeypatch.setattr(mod, "db", fake)
    return fake


def run(coro):
    return asyncio.run(coro)


def req(headers=None, path="/api/customers", cookies=None):
    return SimpleNamespace(headers=headers or {}, cookies=cookies or {},
                           url=SimpleNamespace(path=path))


def coll_docs(fdb, name, tenant=None):
    docs = fdb._colls[name].docs if name in fdb._colls else []
    if tenant:
        docs = [d for d in docs if d.get("tenant_id") == tenant]
    return docs


class Poster:
    """Monkeypatched replacement for lib.developer._post."""

    def __init__(self, mode="ok"):
        self.mode = mode  # ok | http500 | boom
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        if self.mode == "boom":
            raise RuntimeError("connection refused (fake)")
        if self.mode == "http500":
            return 500
        return 200


def create_key(fdb, tenant=T1, label="CI key"):
    async def _go():
        return await developers_r.create_api_key(
            developers_r.CreateKeyBody(label=label), user=make_user(tenant))
    return run(_go())


def create_webhook(fdb, tenant=T1, url="https://hooks.example.com/mae",
                   events=("invoice.created",)):
    async def _go():
        return await developers_r.create_webhook(
            developers_r.CreateWebhookBody(url=url, events=list(events)),
            user=make_user(tenant))
    return run(_go())


def statuses():
    return {i["slug"]: i["status"] for i in mp_lib.list_listings()}


# ---------------- catalog ----------------
class TestCatalog:
    def test_no_keys_means_needs_key_for_gated_listings(self, fdb):
        s = statuses()
        assert s["stripe"] == "needs_key"
        assert s["plaid"] == "needs_key"
        assert s["taxbandits"] == "needs_key"
        assert s["receipt_ai"] == "needs_key"
        assert s["email_delivery"] == "needs_key"

    def test_builtin_listings_live_regardless_of_env(self, fdb):
        s = statuses()
        assert s["quickbooks_csv_bridge"] == "live"
        assert s["momo_sync"] == "live"

    def test_coming_soon_regardless_of_env(self, fdb, monkeypatch):
        for name in GATED_ENV:
            monkeypatch.setenv(name, "x")
        s = statuses()
        assert s["wisetack"] == "coming_soon"
        assert s["shopify"] == "coming_soon"

    def test_statuses_flip_with_env(self, fdb, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
        assert statuses()["stripe"] == "live"
        monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
        assert statuses()["plaid"] == "needs_key"  # secret still missing
        monkeypatch.setenv("PLAID_SECRET", "sec")
        assert statuses()["plaid"] == "live"
        for name in ("TAXBANDITS_CLIENT_ID", "TAXBANDITS_CLIENT_SECRET",
                     "TAXBANDITS_USER_TOKEN"):
            monkeypatch.setenv(name, "x")
        assert statuses()["taxbandits"] == "needs_key"  # business id missing
        monkeypatch.setenv("TAXBANDITS_BUSINESS_ID", "x")
        assert statuses()["taxbandits"] == "live"
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        assert statuses()["receipt_ai"] == "live"
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        assert statuses()["email_delivery"] == "live"

    def test_catalog_shape_categories_and_docs(self, fdb):
        listings = mp_lib.list_listings()
        assert len(listings) == 9
        cats = {c["name"] for c in mp_lib.list_categories(listings)}
        assert {"Payments", "Banking", "Tax", "Financing",
                "Receipts & Documents", "Scheduling", "E-commerce",
                "Data & Migration"} <= cats
        by_slug = {i["slug"]: i for i in listings}
        assert by_slug["stripe"]["setup_doc"] == "STRIPE_SETUP.md"
        assert by_slug["plaid"]["setup_doc"] == "PLAID_SETUP.md"
        assert by_slug["taxbandits"]["setup_doc"] == "TAXBANDITS_SETUP.md"
        assert by_slug["wisetack"]["setup_doc"] is None
        for item in listings:
            assert item["status"] in ("live", "needs_key", "coming_soon")
            assert item["status_label"]
            assert isinstance(item["connect_needs"], list)
        # TaxBandits copy must promise drafts/prep only, never filing.
        tb = by_slug["taxbandits"]["description"].lower()
        assert "draft" in tb and "does not file" in tb

    def test_category_filter_and_search(self, fdb):
        owner = make_user(T1)

        async def _browse(**kw):
            return await marketplace_r.list_marketplace(user=owner, **kw)
        out = run(_browse(category="Banking"))
        assert [i["slug"] for i in out["listings"]] == ["plaid"]
        assert sum(c["count"] for c in out["categories"]) == 9
        out = run(_browse(q="financing"))
        assert [i["slug"] for i in out["listings"]] == ["wisetack"]
        out = run(_browse(q="CSV"))
        assert "quickbooks_csv_bridge" in [i["slug"] for i in out["listings"]]
        out = run(_browse())
        assert len(out["listings"]) == 9

    def test_detail_and_404(self, fdb):
        owner = make_user(T1)

        async def _get(slug):
            return await marketplace_r.get_marketplace_listing(slug, user=owner)
        one = run(_get("stripe"))
        assert one["slug"] == "stripe" and one["status"] == "needs_key"
        with pytest.raises(HTTPException) as ei:
            run(_get("nope"))
        assert ei.value.status_code == 404


# ---------------- API keys ----------------
class TestApiKeys:
    def test_create_shows_key_once_and_stores_only_hash(self, fdb):
        out = create_key(fdb)
        key = out["key"]
        assert key.startswith("maek_")
        assert len(key) > len("maek_") + 32
        stored = coll_docs(fdb, "api_keys")[0]
        assert stored["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
        assert key not in str(stored)
        assert stored["key_stub"] == out["key_stub"]
        assert stored["scopes"] == ["books:read"]
        assert stored["tenant_id"] == T1
        assert stored["revoked_at"] is None

    def test_list_hides_key_and_hash(self, fdb):
        out = create_key(fdb)

        async def _list():
            return await developers_r.list_api_keys(user=make_user(T1))
        listed = run(_list())
        assert len(listed) == 1
        assert "key" not in listed[0] and "api_key" not in listed[0]
        assert "key_hash" not in listed[0]
        assert out["key"] not in str(listed)
        assert listed[0]["key_stub"] == out["key_stub"]

    def test_authenticate_via_bearer_and_header(self, fdb):
        out = create_key(fdb)
        key = out["key"]

        async def _auth(token):
            return await dev_lib.authenticate_api_key(token)
        actor = run(_auth(key))
        assert actor["tenant_id"] == T1
        assert actor["role"] == "api_key"
        assert actor["auth_kind"] == "api_key"
        assert actor["scopes"] == ["books:read"]
        # last_used_at got stamped
        assert coll_docs(fdb, "api_keys")[0]["last_used_at"] is not None

        async def _via_bearer():
            return await dev_lib.get_api_key_actor(
                req({"Authorization": f"Bearer {key}"}))
        assert run(_via_bearer())["tenant_id"] == T1

        async def _via_header():
            return await dev_lib.get_api_key_actor(req({"X-API-Key": key}))
        assert run(_via_header())["tenant_id"] == T1

    def test_bad_and_revoked_keys_401(self, fdb):
        out = create_key(fdb)

        async def _auth(token):
            return await dev_lib.authenticate_api_key(token)
        with pytest.raises(HTTPException) as ei:
            run(_auth("maek_" + "x" * 40))
        assert ei.value.status_code == 401
        with pytest.raises(HTTPException) as ei:
            run(_auth("not-a-key"))
        assert ei.value.status_code == 401

        async def _revoke():
            return await developers_r.revoke_api_key(
                out["id"], user=make_user(T1))
        res = run(_revoke())
        assert res["ok"] is True and res["revoked_at"]
        with pytest.raises(HTTPException) as ei:
            run(_auth(out["key"]))
        assert ei.value.status_code == 401

    def test_tenant_isolation(self, fdb):
        out = create_key(fdb, tenant=T1)

        async def _list_t2():
            return await developers_r.list_api_keys(user=make_user(T2))
        assert run(_list_t2()) == []
        with pytest.raises(HTTPException) as ei:
            run(developers_r.revoke_api_key(out["id"], user=make_user(T2)))
        assert ei.value.status_code == 404
        # still valid for its own tenant
        async def _auth():
            return await dev_lib.authenticate_api_key(out["key"])
        assert run(_auth())["tenant_id"] == T1


# ---------------- public v1 API ----------------
class TestV1Api:
    def _seed_books(self, fdb):
        fdb._colls.setdefault("invoices", FakeCollection()).docs.extend([
            {"id": "inv-t1-old", "tenant_id": T1, "client_name": "Acme",
             "status": "paid", "total_cents": 5000, "paid_cents": 5000,
             "currency": "USD", "issue_date": "2026-10-01",
             "due_date": "2026-10-31", "created_at": "2026-10-01T09:00:00+00:00"},
            {"id": "inv-t1-new", "tenant_id": T1, "client_name": "Beta",
             "status": "partial", "total_cents": 12345, "paid_cents": 2345,
             "currency": "USD", "issue_date": "2026-10-20",
             "due_date": "2026-11-19", "created_at": "2026-10-20T09:00:00+00:00"},
            {"id": "inv-t2", "tenant_id": T2, "client_name": "Other",
             "status": "issued", "total_cents": 999, "paid_cents": 0,
             "currency": "USD", "issue_date": "2026-10-21",
             "due_date": None, "created_at": "2026-10-21T09:00:00+00:00"},
        ])
        fdb._colls.setdefault("customers", FakeCollection()).docs.extend([
            {"id": "cust-t1", "tenant_id": T1, "name": "Acme",
             "email": "a@acme.example", "phone": "555-0100",
             "created_at": "2026-10-01T09:00:00+00:00"},
            {"id": "cust-t2", "tenant_id": T2, "name": "Other",
             "email": None, "phone": None,
             "created_at": "2026-10-02T09:00:00+00:00"},
        ])

    def _actor(self, fdb, tenant=T1):
        key = create_key(fdb, tenant=tenant)["key"]

        async def _auth():
            return await dev_lib.authenticate_api_key(key)
        return run(_auth())

    def test_whoami(self, fdb):
        actor = self._actor(fdb)

        async def _go():
            return await developers_r.v1_whoami(actor=actor)
        out = run(_go())
        assert out == {"tenant_id": T1, "scopes": ["books:read"],
                       "auth": "api_key"}

    def test_v1_invoices_integer_cents_and_tenant_scoped(self, fdb):
        self._seed_books(fdb)
        actor = self._actor(fdb)

        async def _go():
            return await developers_r.v1_invoices(limit=50, actor=actor)
        rows = run(_go())
        assert [r["id"] for r in rows] == ["inv-t1-new", "inv-t1-old"]
        first = rows[0]
        assert first["total_cents"] == 12345
        assert first["paid_cents"] == 2345
        assert first["balance_cents"] == 10000
        assert first["client_name"] == "Beta"
        assert first["currency"] == "USD"
        assert first["issue_date"] == "2026-10-20"
        assert first["due_date"] == "2026-11-19"
        for r in rows:
            for f in ("total_cents", "paid_cents", "balance_cents"):
                assert isinstance(r[f], int)

    def test_v1_customers_tenant_scoped(self, fdb):
        self._seed_books(fdb)
        actor = self._actor(fdb)

        async def _go():
            return await developers_r.v1_customers(limit=50, actor=actor)
        rows = run(_go())
        assert rows == [{"id": "cust-t1", "name": "Acme",
                         "email": "a@acme.example", "phone": "555-0100",
                         "created_at": "2026-10-01T09:00:00+00:00"}]


# ---------------- webhooks ----------------
class TestWebhooks:
    def test_create_validates_url_and_events(self, fdb):
        owner = make_user(T1)
        with pytest.raises(HTTPException) as ei:
            run(developers_r.create_webhook(
                developers_r.CreateWebhookBody(
                    url="ftp://hooks.example.com/x", events=["invoice.created"]),
                user=owner))
        assert ei.value.status_code == 422
        with pytest.raises(HTTPException) as ei:
            run(developers_r.create_webhook(
                developers_r.CreateWebhookBody(
                    url="https://hooks.example.com/x", events=["bogus.event"]),
                user=owner))
        assert ei.value.status_code == 422
        with pytest.raises(HTTPException) as ei:
            run(developers_r.create_webhook(
                developers_r.CreateWebhookBody(
                    url="not-a-url", events=["invoice.created"]),
                user=owner))
        assert ei.value.status_code == 422
        assert coll_docs(fdb, "webhook_subscriptions") == []

    def test_secret_shown_once_and_absent_from_list(self, fdb):
        out = create_webhook(fdb)
        secret = out["signing_secret"]
        assert secret
        stored = coll_docs(fdb, "webhook_subscriptions")[0]
        assert stored["signing_secret"] == secret

        async def _list():
            return await developers_r.list_webhooks(user=make_user(T1))
        listed = run(_list())
        assert len(listed) == 1
        assert "signing_secret" not in listed[0]
        assert secret not in str(listed)
        assert listed[0]["url"] == "https://hooks.example.com/mae"
        assert listed[0]["active"] is True
        # another tenant sees nothing
        async def _list_t2():
            return await developers_r.list_webhooks(user=make_user(T2))
        assert run(_list_t2()) == []

    def test_events_catalog(self, fdb):
        async def _go():
            return await developers_r.list_events(user=make_user(T1))
        rows = run(_go())
        assert [r["event"] for r in rows] == list(dev_lib.EVENT_TYPES)
        assert all(r["description"] for r in rows)

    def test_delivery_signing_and_success(self, fdb, monkeypatch):
        poster = Poster("ok")
        monkeypatch.setattr(dev_lib, "_post", poster)
        out = create_webhook(fdb, events=("invoice.created",))
        secret = out["signing_secret"]

        async def _emit():
            return await dev_lib.emit_event(
                T1, "invoice.created", {"invoice_id": "inv-1", "total_cents": 500})
        deliveries = run(_emit())
        assert len(deliveries) == 1
        assert len(poster.calls) == 1
        call = poster.calls[0]
        expected = "sha256=" + hmac.new(
            secret.encode(), call["body"], hashlib.sha256).hexdigest()
        assert call["headers"]["X-MAE-Signature-256"] == expected
        assert call["headers"]["X-MAE-Event"] == "invoice.created"
        assert call["headers"]["X-MAE-Delivery"] == deliveries[0]["id"]
        assert call["headers"]["Content-Type"] == "application/json"
        stored = coll_docs(fdb, "webhook_deliveries")[0]
        assert stored["status"] == "delivered"
        assert stored["attempts"] == 1
        assert stored["last_status"] == 200
        assert stored["payload"]["event"] == "invoice.created"
        assert stored["payload"]["data"] == {"invoice_id": "inv-1",
                                              "total_cents": 500}

    def test_delivery_failure_backoff_then_failed(self, fdb, monkeypatch):
        poster = Poster("boom")
        monkeypatch.setattr(dev_lib, "_post", poster)
        create_webhook(fdb, events=("invoice.created",))

        async def _emit():
            return await dev_lib.emit_event(T1, "invoice.created", {})
        run(_emit())
        stored = coll_docs(fdb, "webhook_deliveries")[0]
        assert stored["status"] == "pending"
        assert stored["attempts"] == 1
        assert stored["last_status"] is None
        assert "connection refused" in stored["last_error"]
        assert stored["next_retry_at"]

        async def _again():
            sub = await fdb.webhook_subscriptions.find_one(
                {"id": stored["subscription_id"]})
            delivery = await fdb.webhook_deliveries.find_one(
                {"id": stored["id"]})
            await dev_lib.deliver(delivery, sub)
            return await fdb.webhook_deliveries.find_one({"id": stored["id"]})
        after2 = run(_again())
        assert after2["attempts"] == 2 and after2["status"] == "pending"
        after3 = run(_again())
        assert after3["attempts"] == 3 and after3["status"] == "failed"
        assert after3["next_retry_at"] is None

    def test_retry_due_deliveries(self, fdb, monkeypatch):
        poster = Poster("http500")
        monkeypatch.setattr(dev_lib, "_post", poster)
        create_webhook(fdb, events=("invoice.created",))
        run(dev_lib.emit_event(T1, "invoice.created", {}))
        stored = coll_docs(fdb, "webhook_deliveries")[0]
        assert stored["status"] == "pending" and stored["last_status"] == 500
        # not due yet -> sweep is a no-op
        assert run(dev_lib.retry_due_deliveries()) == 0
        # force due, poster recovers -> delivered on the sweep
        stored["next_retry_at"] = "2020-01-01T00:00:00+00:00"
        poster.mode = "ok"
        assert run(dev_lib.retry_due_deliveries()) == 1
        after = coll_docs(fdb, "webhook_deliveries")[0]
        assert after["status"] == "delivered" and after["attempts"] == 2

    def test_emit_routes_only_to_subscribed_and_active(self, fdb, monkeypatch):
        poster = Poster("ok")
        monkeypatch.setattr(dev_lib, "_post", poster)
        create_webhook(fdb, url="https://a.example.com/h",
                       events=("invoice.created",))
        create_webhook(fdb, url="https://b.example.com/h",
                       events=("customer.created",))
        third = create_webhook(fdb, url="https://c.example.com/h",
                               events=("invoice.created",))
        for d in coll_docs(fdb, "webhook_subscriptions"):
            if d["id"] == third["id"]:
                d["active"] = False
        run(dev_lib.emit_event(T1, "invoice.created", {}))
        rows = coll_docs(fdb, "webhook_deliveries")
        assert len(rows) == 1
        sub = coll_docs(fdb, "webhook_subscriptions", )[0]
        assert rows[0]["subscription_id"] == sub["id"]
        assert poster.calls[0]["url"] == "https://a.example.com/h"
        # a different tenant's event fires nothing here
        run(dev_lib.emit_event(T2, "invoice.created", {}))
        assert len(coll_docs(fdb, "webhook_deliveries")) == 1

    def test_test_endpoint_fires_webhook_test(self, fdb, monkeypatch):
        poster = Poster("ok")
        monkeypatch.setattr(dev_lib, "_post", poster)
        sub = create_webhook(fdb, events=("invoice.created",))

        async def _test():
            return await developers_r.test_webhook(sub["id"], user=make_user(T1))
        out = run(_test())
        assert out["event"] == "webhook.test"
        assert out["status"] == "delivered"
        assert poster.calls[0]["headers"]["X-MAE-Event"] == "webhook.test"
        # delivery log endpoint returns the row (payload included, no secrets)
        async def _log():
            return await developers_r.list_deliveries(
                subscription_id=sub["id"], limit=10, user=make_user(T1))
        rows = run(_log())
        assert len(rows) == 1 and rows[0]["event"] == "webhook.test"
        with pytest.raises(HTTPException) as ei:
            run(developers_r.test_webhook(sub["id"], user=make_user(T2)))
        assert ei.value.status_code == 404

    def test_delete_webhook(self, fdb):
        sub = create_webhook(fdb)

        async def _del():
            return await developers_r.delete_webhook(sub["id"], user=make_user(T1))
        assert run(_del())["ok"] is True
        assert coll_docs(fdb, "webhook_subscriptions") == []
        with pytest.raises(HTTPException) as ei:
            run(_del())
        assert ei.value.status_code == 404

    def test_emit_never_raises(self, fdb, monkeypatch):
        # db explodes on the subscription lookup
        def _boom(*a, **kw):
            raise RuntimeError("db exploded (fake)")
        monkeypatch.setattr(fdb.webhook_subscriptions, "find", _boom)
        assert run(dev_lib.emit_event(T1, "invoice.created", {})) == []
        # unknown events are ignored, not raised
        assert run(dev_lib.emit_event(T1, "bogus.event", {})) == []

    def test_emit_never_raises_when_poster_explodes(self, fdb, monkeypatch):
        monkeypatch.setattr(dev_lib, "_post", Poster("boom"))
        create_webhook(fdb, events=("customer.created",))
        out = run(dev_lib.emit_event(T1, "customer.created", {"name": "X"}))
        assert len(out) == 1 and out[0]["status"] == "pending"


# ---------------- event hooks (invoices / customers) ----------------
class TestEventHooks:
    def test_create_issue_pay_emit_events(self, fdb, monkeypatch):
        poster = Poster("ok")
        monkeypatch.setattr(dev_lib, "_post", poster)
        create_webhook(fdb, events=tuple(dev_lib.EVENT_TYPES))
        owner = make_user(T1)

        async def _customer():
            return await customers_r.create_customer(
                customers_r.CustomerBody(
                    name="Acme Co", email="a@acme.example", phone="555-0100"),
                user=owner, request=req({}, "/api/customers"))
        cust = run(_customer())
        assert cust["name"] == "Acme Co"

        async def _invoice():
            return await invoices_r.issue_invoice(
                invoices_r.InvoiceBody(
                    client_name="Acme Co", customer_id=cust["id"],
                    lines=[invoices_r.InvLine(name="Service", amount=100.0)],
                    tax=0),
                user=owner, request=req({}, "/api/invoices"))
        inv = run(_invoice())
        assert inv["total_cents"] == 10000

        async def _pay():
            return await invoices_r.pay_invoice(
                inv["id"], invoices_r.PayBody(payment_method="card"),
                user=owner, request=req({}, f"/api/invoices/{inv['id']}/pay"))
        paid = run(_pay())
        assert paid["status"] == "paid"

        rows = coll_docs(fdb, "webhook_deliveries", T1)
        by_event = {}
        for r in rows:
            by_event.setdefault(r["event"], []).append(r)
        assert set(by_event) == {"customer.created", "invoice.created",
                                 "payment.received", "invoice.paid"}
        cc = by_event["customer.created"][0]["payload"]["data"]
        assert cc == {"customer_id": cust["id"], "name": "Acme Co"}
        ic = by_event["invoice.created"][0]["payload"]["data"]
        assert ic["invoice_id"] == inv["id"]
        assert ic["total_cents"] == 10000 and ic["status"] == "issued"
        pr = by_event["payment.received"][0]["payload"]["data"]
        assert pr["invoice_id"] == inv["id"]
        assert pr["amount_cents"] == 10000
        assert pr["paid_cents"] == 10000 and pr["balance_cents"] == 0
        assert pr["status"] == "paid"
        ip = by_event["invoice.paid"][0]["payload"]["data"]
        assert ip == {"invoice_id": inv["id"], "total_cents": 10000,
                      "paid_cents": 10000}
        assert all(r["status"] == "delivered" for r in rows)
