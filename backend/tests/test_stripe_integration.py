"""Stripe portal integration: key-gating, Checkout Sessions, webhook
signature verification, and exactly-once fulfillment.

Run from the backend directory with:
    python -m pytest tests/test_stripe_integration.py

No live Stripe or MongoDB needed — a fake `stripe` module is injected into
sys.modules (the router imports it lazily, AFTER the key-gate) and the db is
faked in-memory, following tests/test_portal_completion.py conventions.
"""
import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_stripe")

from fastapi import HTTPException  # noqa: E402

from routers import portal as P  # noqa: E402


# ---------------- fake stripe SDK ----------------
class _SigError(Exception):
    pass


def _fake_stripe(event=None, sig_ok=True):
    fake = types.SimpleNamespace()
    fake.api_key = None
    fake.error = types.SimpleNamespace(SignatureVerificationError=_SigError)
    created = {}

    def _create(**kwargs):
        created.update(kwargs)
        return types.SimpleNamespace(
            id="cs_test_123", url="https://checkout.stripe.com/c/cs_test_123")

    fake.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_create))

    def _construct(payload, sig, secret):
        if not sig_ok:
            raise _SigError("bad signature")
        assert secret, "construct_event must receive the webhook secret"
        return event

    fake.Webhook = types.SimpleNamespace(construct_event=_construct)
    fake.created = created
    return fake


# ---------------- fake in-memory db ----------------
def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _UpdateResult:
    def __init__(self, matched):
        self.matched_count = matched


class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                return dict(d)
        return None

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

    async def find_one_and_update(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                before = dict(d)
                for op, changes in update.items():
                    if op == "$set":
                        d.update(changes)
                return before
        return None


class _FakeDB:
    def __init__(self):
        self.portal_links = _FakeCollection()
        self.customers = _FakeCollection()
        self.invoices = _FakeCollection()
        self.tenants = _FakeCollection()
        self.audit_log = _FakeCollection()
        self.payment_transactions = _FakeCollection()


class _Req:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    async def body(self):
        return self._body


TOKEN = "tok_test_abc123"
INV_ID = "inv-abcdef123456"


def _fx(monkeypatch, stripe_fake=None):
    fdb = _FakeDB()
    monkeypatch.setattr(P, "db", fdb)
    if stripe_fake is not None:
        monkeypatch.setitem(sys.modules, "stripe", stripe_fake)
    return fdb


def _seed(fdb, currency="USD", total=50000, paid=0):
    fdb.tenants.docs.append({"id": "t1", "tenant_id": "t1",
                             "name": "Acme Plumbing Co."})
    inv = {"id": INV_ID, "tenant_id": "t1", "customer_id": "c1",
           "client_name": "Jane Client", "total_cents": total,
           "paid_cents": paid, "status": "issued", "issue_entry": "INV-0007",
           "currency": currency, "created_at": "2026-10-01T00:00:00"}
    fdb.invoices.docs.append(inv)
    fdb.portal_links.docs.append({
        "id": "link1", "tenant_id": "t1", "invoice_id": INV_ID,
        "token_hash": P._digest(TOKEN), "revoked": False, "used": False,
        "expires_at": "2099-01-01T00:00:00+00:00"})
    return inv


def _seed_paid_session(fdb):
    fdb.payment_transactions.docs.append({
        "session_id": "cs_test_123", "tenant_id": "t1",
        "kind": "portal_invoice", "invoice_id": INV_ID,
        "token_hash": P._digest(TOKEN), "amount_cents": 50000,
        "currency": "usd", "status": "initiated",
        "payment_status": "pending", "fulfilled": False})


def _completed_event(payment_status="paid"):
    return {"id": "evt_1", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_123", "payment_status": payment_status,
                "amount_total": 50000, "currency": "usd",
                "metadata": {"kind": "portal_invoice", "tenant_id": "t1",
                             "invoice_id": INV_ID,
                             "token_hash": P._digest(TOKEN)}}}}


def _set_keys(monkeypatch, secret=True, webhook=True):
    if secret:
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    else:
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_fake")
    if webhook:
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake")
    else:
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)


# ---------------- key-gating ----------------
def test_requirements_pin_stripe():
    """Regression guard for the original bug: the SDK must be a declared
    dependency, or every checkout call 500s on import."""
    req = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
    with open(req) as f:
        assert any(line.strip().startswith("stripe==") for line in f)


def test_checkout_503_when_unconfigured(monkeypatch):
    async def _run():
        _fx(monkeypatch)
        _set_keys(monkeypatch, secret=False)
        # no `stripe` module available at all — the gate must fire BEFORE
        # any import is attempted (the old code 500'd here)
        monkeypatch.delitem(sys.modules, "stripe", raising=False)
        try:
            await P.portal_checkout(TOKEN, P.PortalCheckoutIn(
                origin_url="https://books.example.com"), _Req())
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 503
            assert "STRIPE_SECRET_KEY" in e.detail
    asyncio.run(_run())


def test_webhook_503_when_secret_unset(monkeypatch):
    async def _run():
        _fx(monkeypatch, _fake_stripe(event=_completed_event()))
        _set_keys(monkeypatch, secret=True, webhook=False)
        try:
            await P.stripe_webhook(_Req(headers={"stripe-signature": "t=1,v1=x"}))
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 503
    asyncio.run(_run())


# ---------------- checkout session creation ----------------
def test_checkout_creates_session_usd(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed(fdb)

        res = await P.portal_checkout(TOKEN, P.PortalCheckoutIn(
            origin_url="https://books.example.com"), _Req())

        assert res["checkout_url"].startswith("https://checkout.stripe.com/")
        assert res["session_id"] == "cs_test_123"
        kw = fake.created
        assert kw["mode"] == "payment"
        item = kw["line_items"][0]
        assert item["price_data"]["currency"] == "usd"
        assert item["price_data"]["unit_amount"] == 50000
        assert kw["success_url"].startswith("https://books.example.com/p/" + TOKEN)
        assert "paid=1" in kw["success_url"]
        assert kw["cancel_url"] == "https://books.example.com/p/" + TOKEN
        assert kw["metadata"]["invoice_id"] == INV_ID
        assert kw["metadata"]["tenant_id"] == "t1"
        tx = fdb.payment_transactions.docs[0]
        assert tx["session_id"] == "cs_test_123"
        assert tx["amount_cents"] == 50000
        assert tx["payment_status"] == "pending"
        assert tx["fulfilled"] is False
    asyncio.run(_run())


def test_checkout_jpy_zero_decimal_amount(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed(fdb, currency="JPY")  # stored x100 -> ¥500 due; Stripe wants 500

        await P.portal_checkout(TOKEN, P.PortalCheckoutIn(
            origin_url="https://books.example.com"), _Req())

        item = fake.created["line_items"][0]
        assert item["price_data"]["currency"] == "jpy"
        assert item["price_data"]["unit_amount"] == 500
    asyncio.run(_run())


def test_checkout_rejects_paid_invoice(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed(fdb, paid=50000)
        try:
            await P.portal_checkout(TOKEN, P.PortalCheckoutIn(
                origin_url="https://books.example.com"), _Req())
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 409
    asyncio.run(_run())


# ---------------- webhook ----------------
def test_webhook_bad_signature_400(monkeypatch):
    async def _run():
        _fx(monkeypatch, _fake_stripe(sig_ok=False))
        _set_keys(monkeypatch)
        try:
            await P.stripe_webhook(_Req(
                body=b"{}", headers={"stripe-signature": "t=1,v1=forged"}))
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 400
    asyncio.run(_run())


def _mock_ledger(monkeypatch):
    drafts = []

    async def _post(db, tenant_id, user, draft, status="posted",
                    idempotency_key=None):
        drafts.append(draft)
        return {"number": "JE-1001", "id": "je1"}

    monkeypatch.setattr(P.L, "post_journal_entry", _post)
    monkeypatch.setattr(P.FXP, "post_realized_fx",
                        AsyncMock(return_value=None))
    return drafts


def test_webhook_fulfills_exactly_once(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe(event=_completed_event()))
        _set_keys(monkeypatch)
        _seed(fdb)
        _seed_paid_session(fdb)
        drafts = _mock_ledger(monkeypatch)
        req = _Req(body=b"{}", headers={"stripe-signature": "t=1,v1=ok"})

        res = await P.stripe_webhook(req)
        assert res == {"received": True}

        # exactly one journal entry: Dr 1050 Stripe Clearing / Cr 1200 AR
        assert len(drafts) == 1
        draft = drafts[0]
        assert draft["source"] == "stripe_portal"
        assert draft["lines"][0] == {"account_code": "1050", "debit": 50000,
                                     "credit": 0, "memo": "Stripe card payment"}
        assert draft["lines"][1] == {"account_code": "1200", "debit": 0,
                                     "credit": 50000, "memo": "AR cleared"}

        inv = fdb.invoices.docs[0]
        assert inv["paid_cents"] == 50000
        assert inv["status"] == "paid"
        assert len(inv["payments"]) == 1
        pmt = inv["payments"][0]
        assert pmt["reference"] == "cs_test_123"  # session ref = idempotency key
        assert pmt["method"] == "card" and pmt["processor"] == "stripe"
        assert fdb.portal_links.docs[0]["used"] is True
        tx = fdb.payment_transactions.docs[0]
        assert tx["fulfilled"] is True and tx["payment_status"] == "paid"

        # duplicate delivery (Stripe retries webhooks): no double-post
        res2 = await P.stripe_webhook(req)
        assert res2 == {"received": True}
        assert len(drafts) == 1
        assert len(fdb.invoices.docs[0]["payments"]) == 1
        assert fdb.invoices.docs[0]["paid_cents"] == 50000
    asyncio.run(_run())


def test_webhook_unpaid_session_waits(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe(
            event=_completed_event(payment_status="unpaid")))
        _set_keys(monkeypatch)
        _seed(fdb)
        _seed_paid_session(fdb)
        drafts = _mock_ledger(monkeypatch)

        res = await P.stripe_webhook(_Req(
            body=b"{}", headers={"stripe-signature": "t=1,v1=ok"}))
        assert res == {"received": True}
        assert drafts == []
        assert fdb.payment_transactions.docs[0]["payment_status"] == "pending"
    asyncio.run(_run())


def test_webhook_ignores_unrelated_events(monkeypatch):
    async def _run():
        ev = {"id": "evt_9", "type": "payment_intent.succeeded",
              "data": {"object": {"id": "pi_123"}}}
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed(fdb)
        drafts = _mock_ledger(monkeypatch)

        res = await P.stripe_webhook(_Req(
            body=b"{}", headers={"stripe-signature": "t=1,v1=ok"}))
        assert res == {"received": True}
        assert drafts == []
    asyncio.run(_run())
