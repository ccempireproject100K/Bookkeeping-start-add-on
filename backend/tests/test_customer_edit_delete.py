"""Customer edit + soft delete.

Covers the fast-follow to PR #120: customers previously had create/list/get/
statement only — no way to fix a typo or retire a customer.

  - PATCH /customers/{id}: happy path, self-edit is not a "duplicate",
    rename onto another active customer's name + email/phone -> 409,
    missing/deleted customer -> 404.
  - DELETE /customers/{id}: SOFT delete (deleted_at + active=False, row kept)
    so historical invoices/statements keep resolving; hidden from list;
    second delete -> 404; name/contact freed for reuse.
  - Permission wiring: writes require ops:manage (owner only), like create.
  - Cross-tenant isolation: tenant B cannot see/edit/delete tenant A's rows.

No live MongoDB needed — db collections are faked in-memory (tenant-strict).

Run from the backend directory with:
    python -m pytest tests/test_customer_edit_delete.py
"""
import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_custedit")

from routers import customers as C  # noqa: E402

T1, T2 = "tenant-1", "tenant-2"
OWNER1 = {"id": "u1", "email": "owner@a.test", "tenant_id": T1, "role": "owner"}
OWNER2 = {"id": "u9", "email": "owner@b.test", "tenant_id": T2, "role": "owner"}
BOOKKEEPER1 = {"id": "u2", "email": "books@a.test", "tenant_id": T1, "role": "bookkeeper"}
STAFF1 = {"id": "u3", "email": "staff@a.test", "tenant_id": T1, "role": "barber"}


# ---------------------------------------------------------------- fakes

def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict):
            raise AssertionError(f"unexpected operator filter in fake: {k}={v}")
        if doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec):
            self._docs.sort(key=lambda d: (d.get(key) is None, str(d.get(key) or "")),
                            reverse=(direction == -1))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return [dict(d) for d in self._docs]


class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([d for d in self.docs if _matches(d, q or {})])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                d.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q or {}))


class _FakeDB:
    def __init__(self):
        self.customers = _FakeCollection()
        self.invoices = _FakeCollection()


def _fx(monkeypatch):
    fdb = _FakeDB()
    monkeypatch.setattr(C, "db", fdb)
    return fdb


def _cust(cid, tenant=T1, name="Acme Co", email="a@x.com", phone="555-0001", **kw):
    d = {"id": cid, "tenant_id": tenant, "name": name, "email": email,
         "phone": phone, "locations": [{"label": "Primary", "address": "1 Main St"}],
         "created_at": "2026-01-01T00:00:00+00:00", "active": True, "deleted_at": None}
    d.update(kw)
    return d


def _body(name="Acme Co", email="a@x.com", phone="555-0001", address="1 Main St"):
    return C.CustomerBody(name=name, email=email, phone=phone,
                          locations=[C.Location(label="Primary", address=address)])


def run(coro):
    # Dedicated loop, NOT asyncio.run(): asyncio.run() unsets the thread's
    # current event loop on exit, which breaks the legacy
    # asyncio.get_event_loop() pattern in test_currency_phase4.py when test
    # order shifts (same fix PR #123 applied to its own tests).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ------------------------------------------------------------- edit (PATCH)

def test_edit_happy_path(monkeypatch):
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1"))

    async def go():
        out = await C.update_customer(
            "c1", _body(name="Acme Corp", email="new@x.com", phone="555-2222",
                        address="9 Oak Ave"), user=OWNER1)
        assert out["name"] == "Acme Corp"
        assert out["email"] == "new@x.com"
        assert out["phone"] == "555-2222"
        assert out["locations"][0]["address"] == "9 Oak Ave"
        assert out["updated_at"]
        stored = await C.get_customer("c1", user=OWNER1)
        assert stored["name"] == "Acme Corp"
    run(go())


def test_edit_unchanged_identity_is_not_a_duplicate(monkeypatch):
    """Saving an edit without changing name/email must not 409 against itself."""
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1"))

    async def go():
        out = await C.update_customer("c1", _body(address="2 New Rd"), user=OWNER1)
        assert out["locations"][0]["address"] == "2 New Rd"
    run(go())


def test_edit_rename_onto_other_customer_email_409(monkeypatch):
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1"))
    fdb.customers.docs.append(_cust("c2", name="Beta LLC", email="b@x.com", phone="555-0002"))

    async def go():
        with pytest.raises(HTTPException) as exc:
            await C.update_customer(
                "c2", _body(name="acme co", email="A@X.COM", phone="555-9999"),
                user=OWNER1)
        assert exc.value.status_code == 409
        assert exc.value.detail["existing_id"] == "c1"
        # and the failed edit changed nothing
        stored = await C.get_customer("c2", user=OWNER1)
        assert stored["name"] == "Beta LLC"
    run(go())


def test_edit_rename_onto_other_customer_phone_409(monkeypatch):
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1"))
    fdb.customers.docs.append(_cust("c2", name="Beta LLC", email="b@x.com", phone="555-0002"))

    async def go():
        with pytest.raises(HTTPException) as exc:
            await C.update_customer(
                "c2", _body(name="Acme Co", email="other@y.com", phone="555 0001"),
                user=OWNER1)
        assert exc.value.status_code == 409
        assert exc.value.detail["existing_id"] == "c1"
    run(go())


def test_edit_same_name_different_contact_ok(monkeypatch):
    """Two real customers may share a name — edit is fine if contact differs."""
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1", name="John Smith", email="j1@x.com", phone="555-1001"))
    fdb.customers.docs.append(_cust("c2", name="Jane Roe", email="j2@x.com", phone="555-1002"))

    async def go():
        out = await C.update_customer(
            "c2", _body(name="John Smith", email="j2@x.com", phone="555-1002"), user=OWNER1)
        assert out["name"] == "John Smith"
    run(go())


def test_edit_missing_customer_404(monkeypatch):
    _fx(monkeypatch)

    async def go():
        with pytest.raises(HTTPException) as exc:
            await C.update_customer("nope", _body(), user=OWNER1)
        assert exc.value.status_code == 404
    run(go())


def test_edit_deleted_customer_404(monkeypatch):
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1", deleted_at="2026-02-01T00:00:00+00:00",
                                    active=False))

    async def go():
        with pytest.raises(HTTPException) as exc:
            await C.update_customer("c1", _body(name="Zombie"), user=OWNER1)
        assert exc.value.status_code == 404
    run(go())


def test_edit_onto_deleted_customers_contact_ok(monkeypatch):
    """A deleted customer's name + contact no longer blocks edits."""
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1", deleted_at="2026-02-01T00:00:00+00:00",
                                    active=False))
    fdb.customers.docs.append(_cust("c2", name="Beta LLC", email="b@x.com", phone="555-0002"))

    async def go():
        out = await C.update_customer(
            "c2", _body(name="Acme Co", email="a@x.com", phone="555-0001"), user=OWNER1)
        assert out["email"] == "a@x.com"
    run(go())


# ----------------------------------------------------------- delete (soft)

def test_delete_is_soft_and_statement_still_resolves(monkeypatch):
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1"))
    fdb.invoices.docs.append({
        "id": "inv1", "tenant_id": T1, "customer_id": "c1",
        "client_name": "Acme Co", "total_cents": 10000, "status": "issued",
        "created_at": "2026-03-01T10:00:00+00:00", "payments": [],
    })

    async def go():
        out = await C.delete_customer("c1", user=OWNER1)
        assert out["deleted"] is True and out["deleted_at"]

        # row kept, stamped — not removed
        raw = fdb.customers.docs[0]
        assert raw["deleted_at"] and raw["active"] is False

        # hidden from list
        rows = await C.list_customers(user=OWNER1)
        assert rows == []

        # still resolvable by id and statement (history must not orphan)
        got = await C.get_customer("c1", user=OWNER1)
        assert got["name"] == "Acme Co"
        stmt = await C.statement("c1", user=OWNER1)
        assert stmt["customer"]["name"] == "Acme Co"
        assert len(stmt["events"]) == 1
        assert stmt["outstanding_cents"] == 10000
    run(go())


def test_delete_twice_404(monkeypatch):
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1"))

    async def go():
        await C.delete_customer("c1", user=OWNER1)
        with pytest.raises(HTTPException) as exc:
            await C.delete_customer("c1", user=OWNER1)
        assert exc.value.status_code == 404
    run(go())


def test_delete_missing_404(monkeypatch):
    _fx(monkeypatch)

    async def go():
        with pytest.raises(HTTPException) as exc:
            await C.delete_customer("nope", user=OWNER1)
        assert exc.value.status_code == 404
    run(go())


def test_create_reusing_deleted_customers_contact_ok(monkeypatch):
    """After a soft delete, the same name + email can be created fresh."""
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1"))

    async def go():
        await C.delete_customer("c1", user=OWNER1)
        out = await C.create_customer(_body(), user=OWNER1)
        assert out["id"] != "c1"
        rows = await C.list_customers(user=OWNER1)
        assert [r["id"] for r in rows] == [out["id"]]
    run(go())


# ------------------------------------------------------------- permissions

def _route_dep(action_methods):
    """Find the customers route for (method, path) and return its gate callable."""
    for route in C.router.routes:
        if route.path == "/customers/{customer_id}" and action_methods & route.methods:
            deps = route.dependant.dependencies
            assert deps, "write route must declare a permission dependency"
            return deps[0].call
    raise AssertionError(f"route not found for {action_methods}")


def test_write_routes_require_ops_manage():
    for methods in ({"PATCH"}, {"DELETE"}):
        gate = _route_dep(methods)

        async def go():
            assert await gate(OWNER1) is OWNER1
            for denied in (BOOKKEEPER1, STAFF1):
                with pytest.raises(HTTPException) as exc:
                    await gate(denied)
                assert exc.value.status_code == 403
        run(go())


def test_soft_delete_helpers():
    assert C._is_deleted({"deleted_at": "2026-01-01"}) is True
    assert C._is_deleted({"deleted_at": None}) is False
    assert C._is_deleted({}) is False
    rows = [{"id": "1"}, {"id": "2", "deleted_at": "2026-01-01"},
            {"id": "3", "deleted_at": None}]
    assert [r["id"] for r in C._active_only(rows)] == ["1", "3"]


# -------------------------------------------------------- cross-tenant

def test_cross_tenant_isolation(monkeypatch):
    fdb = _fx(monkeypatch)
    fdb.customers.docs.append(_cust("c1", tenant=T1))

    async def go():
        # tenant 2's list is empty; tenant 1's customer is invisible
        assert await C.list_customers(user=OWNER2) == []
        with pytest.raises(HTTPException) as exc:
            await C.get_customer("c1", user=OWNER2)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await C.update_customer("c1", _body(name="Hijacked"), user=OWNER2)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await C.delete_customer("c1", user=OWNER2)
        assert exc.value.status_code == 404
        # untouched in tenant 1
        rows = await C.list_customers(user=OWNER1)
        assert [r["name"] for r in rows] == ["Acme Co"]
    run(go())
