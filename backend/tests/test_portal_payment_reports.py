"""Portal payment reports (security audit 2026-10-26, N-1; owner
ruling: a customer-reported payment says PENDING until the shop
confirms it).

Run from the backend directory with:
    python -m pytest tests/test_portal_payment_reports.py

A customer reporting a payment posts NOTHING — no journal entry, no
invoice movement. Only the shop's confirm posts, and it posts exactly
once (atomic claim, the F-1 pattern): sequential and concurrent
double-confirms are pinned. Rejection leaves no journal trace and
frees the reported amount.

No live MongoDB needed — the db is faked in-memory following
tests/test_portal_completion.py conventions, the ledger poster is a
recording stub (ledger internals have their own coverage), and
realized-FX is mocked out.
"""
import asyncio
import os
from unittest.mock import AsyncMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_portal_reports")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from routers import portal as P  # noqa: E402


# ---------------- fake in-memory db ----------------
def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        # Accepts motor-style sort("field", dir) and Scoped's
        # sort([("field", dir)]) alike.
        if a and isinstance(a[0], list) and a[0]:
            key, direction = a[0][0]
        elif a:
            key, direction = a[0], (a[1] if len(a) > 1 else 1)
        else:
            return self
        self._docs = sorted(self._docs, key=lambda d: d.get(key) or "",
                            reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return [dict(d) for d in self._docs[:length] if length
                ] if length else [dict(d) for d in self._docs]


class _UpdateResult:
    def __init__(self, matched):
        self.matched_count = matched


class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q or {}):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        docs = [dict(d) for d in self.docs if _matches(d, q or {})]
        if projection and any(v == 0 for v in projection.values()):
            excluded = {k for k, v in projection.items() if v == 0}
            docs = [{k: v for k, v in d.items() if k not in excluded}
                    for d in docs]
        elif projection:
            keep = set(projection) | {"id"}
            docs = [{k: v for k, v in d.items() if k in keep}
                    for d in docs]
        return _Cursor(docs)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update):
        matched = 0
        for d in self.docs:
            if _matches(d, q):
                matched += 1
                for op, changes in update.items():
                    if op == "$set":
                        d.update(changes)
                    elif op == "$push":
                        for f, v in changes.items():
                            d.setdefault(f, []).append(v)
        return _UpdateResult(matched)


class _FakeDB:
    def __init__(self):
        self.portal_links = _FakeCollection()
        self.portal_payment_reports = _FakeCollection()
        self.customers = _FakeCollection()
        self.invoices = _FakeCollection()
        self.tenants = _FakeCollection()
        self.audit_log = _FakeCollection()


OWNER = {"id": "u1", "tenant_id": "t1", "name": "Test Owner",
         "email": "owner@example.com", "role": "owner"}
INV_ID = "inv-abcdef123456"


def _wire(monkeypatch):
    """Fake db + recording ledger stub; returns (fdb, posts)."""
    fdb = _FakeDB()
    monkeypatch.setattr(P, "db", fdb)
    posts = []

    async def fake_post(db_, tenant_id, actor, entry, status=None):
        posts.append(entry)
        return {"number": f"JE-{len(posts):04d}"}

    monkeypatch.setattr(P.L, "post_journal_entry", fake_post)
    monkeypatch.setattr(P.FXP, "post_realized_fx", AsyncMock())
    fdb.tenants.docs.append({"id": "t1", "tenant_id": "t1",
                             "name": "Acme Plumbing Co."})
    fdb.invoices.docs.append({
        "id": INV_ID, "tenant_id": "t1", "customer_id": "c1",
        "client_name": "Jane Client", "total_cents": 50000,
        "paid_cents": 0, "status": "issued", "issue_entry": "INV-0007",
        "created_at": "2026-10-01T00:00:00", "payments": []})
    return fdb, posts


def run(coro):
    return asyncio.run(coro)


async def _token(fdb):
    out = await P.create_link(
        P.LinkIn(invoice_id=INV_ID, send_email=False), OWNER)
    return out["token"]


async def _report(token, amount, method="bank"):
    return await P.portal_pay(token, P.PortalPayIn(
        amount=amount, payment_method=method, reference=""))


def _invoice(fdb):
    return next(d for d in fdb.invoices.docs if d["id"] == INV_ID)


# ---------------- the report itself ----------------
def test_report_posts_nothing(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        return await _report(token, 500.00)
    out = run(go())
    assert out["status"] == "pending_confirmation"
    assert out["paid_cents"] == 0 and out["balance_cents"] == 50000
    assert posts == []  # NO journal entry
    inv = _invoice(fdb)
    assert inv["paid_cents"] == 0 and inv["status"] == "issued"
    assert inv["payments"] == []
    reports = fdb.portal_payment_reports.docs
    assert len(reports) == 1 and reports[0]["status"] == "pending"
    assert reports[0]["amount_cents"] == 50000
    actions = [a["action"] for a in fdb.audit_log.docs]
    assert "portal.payment.reported" in actions


def test_view_shows_pending_honestly(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        await _report(token, 200.00, method="cash")
        return await P.portal_view(token)
    view = run(go())
    assert view["pending_cents"] == 20000
    assert len(view["pending_reports"]) == 1
    assert view["pending_reports"][0]["method"] == "cash"
    # The invoice itself still shows the money as NOT paid.
    assert view["invoice"]["paid_cents"] == 0
    assert view["invoice"]["balance_cents"] == 50000


def test_over_report_refused(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        await _report(token, 500.00)
        with pytest.raises(HTTPException) as e1:
            await _report(token, 1.00)
        assert e1.value.status_code == 422
        token2 = await _token(fdb)
        with pytest.raises(HTTPException) as e2:
            await P.portal_pay(token2, P.PortalPayIn(
                amount=600.00, payment_method="bank", reference=""))
        assert e2.value.status_code == 422
    run(go())
    assert len(fdb.portal_payment_reports.docs) == 1


# ---------------- confirm: exactly once ----------------
def test_confirm_posts_exactly_once(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        rep = await _report(token, 500.00)
        first = await P.confirm_payment_report(rep["report_id"], OWNER)
        second = await P.confirm_payment_report(rep["report_id"], OWNER)
        return first, second
    first, second = run(go())
    assert first["already_confirmed"] is False
    assert first["invoice_status"] == "paid"
    assert second["already_confirmed"] is True
    assert second["entry_number"] == first["entry_number"]
    assert len(posts) == 1
    inv = _invoice(fdb)
    assert inv["paid_cents"] == 50000 and inv["status"] == "paid"
    assert len(inv["payments"]) == 1
    assert inv["payments"][0]["report_id"]
    link = fdb.portal_links.docs[0]
    assert link["used"] is True  # burned on full payment, as before


def test_concurrent_confirms_post_once(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        rep = await _report(token, 500.00)
        return await asyncio.gather(
            P.confirm_payment_report(rep["report_id"], OWNER),
            P.confirm_payment_report(rep["report_id"], OWNER))
    results = run(go())
    assert len(posts) == 1
    assert _invoice(fdb)["paid_cents"] == 50000
    assert sorted(r["already_confirmed"] for r in results) == [False, True]


def test_partial_reports_confirm_to_paid(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        r1 = await _report(token, 200.00)
        r2 = await _report(token, 300.00)
        await P.confirm_payment_report(r1["report_id"], OWNER)
        return await P.confirm_payment_report(r2["report_id"], OWNER)
    out = run(go())
    assert out["invoice_status"] == "paid"
    assert len(posts) == 2
    inv = _invoice(fdb)
    assert inv["paid_cents"] == 50000 and len(inv["payments"]) == 2


def test_confirm_beyond_remaining_balance_refused(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        rep = await _report(token, 300.00)
        # Another payment lands first (e.g. in-shop), shrinking the
        # balance below the reported amount.
        _invoice(fdb)["paid_cents"] = 40000
        with pytest.raises(HTTPException) as e:
            await P.confirm_payment_report(rep["report_id"], OWNER)
        assert e.value.status_code == 409
    run(go())
    assert posts == []
    # The report stays pending so the shop can reject it.
    assert fdb.portal_payment_reports.docs[0]["status"] == "pending"


# ---------------- reject ----------------
def test_reject_leaves_no_trace_and_frees_amount(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        rep = await _report(token, 500.00)
        out = await P.reject_payment_report(
            rep["report_id"], P.ReportDecisionIn(note="no record of it"),
            OWNER)
        again = await P.reject_payment_report(rep["report_id"], None,
                                              OWNER)
        # The freed amount can be reported again.
        rep2 = await _report(token, 500.00)
        return out, again, rep2
    out, again, rep2 = run(go())
    assert out["status"] == "rejected" and out["already_rejected"] is False
    assert again["already_rejected"] is True
    assert rep2["status"] == "pending_confirmation"
    assert posts == []
    inv = _invoice(fdb)
    assert inv["paid_cents"] == 0 and inv["payments"] == []


def test_confirm_rejected_and_reject_confirmed_conflict(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        rep = await _report(token, 100.00)
        await P.reject_payment_report(rep["report_id"], None, OWNER)
        with pytest.raises(HTTPException) as e1:
            await P.confirm_payment_report(rep["report_id"], OWNER)
        assert e1.value.status_code == 409
        rep2 = await _report(token, 100.00)
        await P.confirm_payment_report(rep2["report_id"], OWNER)
        with pytest.raises(HTTPException) as e2:
            await P.reject_payment_report(rep2["report_id"], None, OWNER)
        assert e2.value.status_code == 409
    run(go())
    assert len(posts) == 1


# ---------------- shop list ----------------
def test_list_payment_reports_for_shop(monkeypatch):
    fdb, posts = _wire(monkeypatch)

    async def go():
        token = await _token(fdb)
        r1 = await _report(token, 100.00)
        await _report(token, 150.00)
        pending = await P.list_payment_reports("pending", OWNER)
        await P.confirm_payment_report(r1["report_id"], OWNER)
        pending2 = await P.list_payment_reports("pending", OWNER)
        all_ = await P.list_payment_reports("all", OWNER)
        return pending, pending2, all_
    pending, pending2, all_ = run(go())
    assert len(pending) == 2
    assert pending[0]["invoice_ref"] == "INV-0007"
    assert pending[0]["client_name"] == "Jane Client"
    assert "token_hash" not in pending[0]
    assert len(pending2) == 1 and len(all_) == 2
