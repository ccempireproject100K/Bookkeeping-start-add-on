"""Books subscription checkout: plan catalog, Stripe Checkout session
creation, Customer Portal sessions, the billing webhook as the ONLY
writer of subscription state, and the referral hooks (signup ref
capture + payment-event emission).

Run from the backend directory with:
    python -m pytest tests/test_books_checkout.py

No live Stripe or MongoDB needed — a fake `stripe` module is injected
into sys.modules (lib.billing imports it lazily, AFTER the key-gate)
and the db is faked in-memory, following tests/test_stripe_integration.py
conventions.
"""
import asyncio
import os
import sys
import types
from copy import deepcopy
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_books_checkout")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from lib import billing as B  # noqa: E402
from routers import billing as R  # noqa: E402
import routers.auth as auth_router  # noqa: E402


# ---------------- fake stripe SDK ----------------
class SignatureVerificationError(Exception):
    pass


def _fake_stripe(event=None, sig_ok=True):
    fake = types.SimpleNamespace()
    fake.api_key = None
    fake.error = types.SimpleNamespace(
        SignatureVerificationError=SignatureVerificationError)
    created = {}
    portal_created = {}

    def _checkout_create(**kwargs):
        created.update(kwargs)
        return types.SimpleNamespace(
            id="cs_sub_123", url="https://checkout.stripe.com/c/cs_sub_123")

    def _portal_create(**kwargs):
        portal_created.update(kwargs)
        return types.SimpleNamespace(
            id="bps_123", url="https://billing.stripe.com/p/session/bps_123")

    fake.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_checkout_create))
    fake.billing_portal = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_portal_create))

    def _construct(payload, sig, secret):
        if not sig_ok:
            raise SignatureVerificationError("bad signature")
        assert secret, "construct_event must receive the webhook secret"
        return event

    fake.Webhook = types.SimpleNamespace(construct_event=_construct)
    fake.created = created
    fake.portal_created = portal_created
    return fake


# ---------------- fake in-memory db ----------------
def _get_path(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict) and "$in" in v:
            if _get_path(doc, k) not in v["$in"]:
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if _get_path(doc, k) == v["$ne"]:
                return False
        elif _get_path(doc, k) != v:
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
                return deepcopy(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(deepcopy(doc))

    async def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if _matches(d, q):
                for op, changes in update.items():
                    if op == "$set":
                        for k, v in changes.items():
                            if "." in k:
                                parts = k.split(".")
                                cur = d
                                for p in parts[:-1]:
                                    cur = cur.setdefault(p, {})
                                cur[parts[-1]] = v
                            else:
                                d[k] = v
                    elif op == "$push":
                        for f, v in changes.items():
                            d.setdefault(f, []).append(v)
                return _UpdateResult(1)
        if upsert:
            doc = {k: v for k, v in (q or {}).items() if not isinstance(v, dict)}
            for op, changes in (update or {}).items():
                if op == "$set":
                    doc.update(changes)
            self.docs.append(doc)
            return _UpdateResult(1)
        return _UpdateResult(0)


class _FakeDB:
    def __init__(self):
        self.tenants = _FakeCollection()
        self.users = _FakeCollection()
        self.audit_log = _FakeCollection()
        self.stripe_webhook_events = _FakeCollection()
        self.referral_payment_events = _FakeCollection()


class _Req:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}
        self.client = None

    async def body(self):
        return self._body


USER = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.com",
        "name": "Owner", "role": "owner"}
EPOCH_END = 1793577600


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _fx(monkeypatch, stripe_fake=None):
    fdb = _FakeDB()
    monkeypatch.setattr(R, "db", fdb)
    if stripe_fake is not None:
        monkeypatch.setitem(sys.modules, "stripe", stripe_fake)
    return fdb


def _set_keys(monkeypatch, secret=True, webhook=True):
    if secret:
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    else:
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    if webhook:
        monkeypatch.setenv("STRIPE_BILLING_WEBHOOK_SECRET", "whsec_billing_fake")
    else:
        monkeypatch.delenv("STRIPE_BILLING_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("STRIPE_PRICE_LIGHT", raising=False)
    monkeypatch.delenv("STRIPE_PRICE_MEDIUM", raising=False)


def _seed_tenant(fdb, **over):
    t = {"id": "t1", "name": "Acme Plumbing Co.", "plan": "light",
         "trial_ends_at": "2020-01-01T00:00:00+00:00"}
    t.update(over)
    fdb.tenants.docs.append(t)
    return t


def _tenant(fdb):
    return fdb.tenants.docs[0]


# ---------------- catalog ----------------
def test_catalog_prices_match_plan_info():
    from lib.plans import PLAN_INFO
    assert B.CATALOG["light"]["amount_cents"] == 1899
    assert B.CATALOG["medium"]["amount_cents"] == 3999
    assert PLAN_INFO["light"]["standalone_price"] == "$18.99/mo"
    assert PLAN_INFO["medium"]["standalone_price"] == "$39.99/mo"
    assert B.CATALOG["heavy"]["self_serve"] is False
    assert set(B.SELF_SERVE_PLANS) == {"light", "medium"}


# ---------------- key-gating ----------------
def test_checkout_503_when_unconfigured(monkeypatch):
    async def _run():
        _fx(monkeypatch)
        _set_keys(monkeypatch, secret=False)
        # no `stripe` module at all — the gate must fire BEFORE import
        monkeypatch.delitem(sys.modules, "stripe", raising=False)
        with pytest.raises(HTTPException) as e:
            await R.subscribe_checkout(
                R.CheckoutIn(plan="light", origin_url="https://books.example.com"),
                _Req(), USER)
        assert e.value.status_code == 503
        assert "STRIPE_SECRET_KEY" in e.value.detail
    asyncio.run(_run())


def test_webhook_503_when_secret_unset(monkeypatch):
    async def _run():
        _fx(monkeypatch, _fake_stripe(event={}))
        _set_keys(monkeypatch, secret=True, webhook=False)
        with pytest.raises(HTTPException) as e:
            await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=x"}))
        assert e.value.status_code == 503
    asyncio.run(_run())


def test_portal_session_503_when_unconfigured(monkeypatch):
    async def _run():
        _fx(monkeypatch)
        _set_keys(monkeypatch, secret=False)
        with pytest.raises(HTTPException) as e:
            await R.billing_portal_session(
                R.PortalSessionIn(origin_url="https://books.example.com"),
                _Req(), USER)
        assert e.value.status_code == 503
    asyncio.run(_run())


# ---------------- checkout creation ----------------
def test_checkout_heavy_is_not_self_serve(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        with pytest.raises(HTTPException) as e:
            await R.subscribe_checkout(
                R.CheckoutIn(plan="heavy", origin_url="https://books.example.com"),
                _Req(), USER)
        assert e.value.status_code == 422
        assert "volume" in e.value.detail.lower()
    asyncio.run(_run())


def test_checkout_unknown_plan_422(monkeypatch):
    async def _run():
        _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        with pytest.raises(HTTPException) as e:
            await R.subscribe_checkout(
                R.CheckoutIn(plan="platinum", origin_url="https://books.example.com"),
                _Req(), USER)
        assert e.value.status_code == 422
    asyncio.run(_run())


def test_checkout_creates_session_inline_price_and_changes_nothing(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)  # no STRIPE_PRICE_* -> inline price_data
        _seed_tenant(fdb)

        res = await R.subscribe_checkout(
            R.CheckoutIn(plan="medium", origin_url="https://books.example.com"),
            _Req(), USER)

        assert res["checkout_url"].startswith("https://checkout.stripe.com/")
        assert res["session_id"] == "cs_sub_123"
        kw = fake.created
        assert kw["mode"] == "subscription"
        item = kw["line_items"][0]
        assert item["price_data"]["unit_amount"] == 3999
        assert item["price_data"]["currency"] == "usd"
        assert item["price_data"]["recurring"] == {"interval": "month"}
        assert kw["metadata"] == {"kind": "books_subscription",
                                   "tenant_id": "t1", "plan": "medium"}
        assert kw["subscription_data"]["metadata"] == kw["metadata"]
        assert kw["customer_email"] == "owner@shop.com"
        assert kw["success_url"].startswith("https://books.example.com/settings?billing=success")
        assert kw["cancel_url"] == "https://books.example.com/settings?billing=cancelled"
        # Creating a session is NOT a payment: no state may move.
        t = _tenant(fdb)
        assert t["plan"] == "light"
        assert "billing" not in t
    asyncio.run(_run())


def test_checkout_uses_env_price_id(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        monkeypatch.setenv("STRIPE_PRICE_LIGHT", "price_light_123")
        _seed_tenant(fdb)

        await R.subscribe_checkout(
            R.CheckoutIn(plan="light", origin_url="https://books.example.com"),
            _Req(), USER)

        assert fake.created["line_items"][0] == {"price": "price_light_123",
                                                 "quantity": 1}
    asyncio.run(_run())


def test_checkout_reuses_existing_stripe_customer(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, billing={"stripe_customer_id": "cus_123",
                                    "status": "canceled", "plan": "light"})

        await R.subscribe_checkout(
            R.CheckoutIn(plan="medium", origin_url="https://books.example.com"),
            _Req(), USER)

        assert fake.created["customer"] == "cus_123"
        assert "customer_email" not in fake.created
    asyncio.run(_run())


# ---------------- customer portal ----------------
def test_portal_session_409_without_billing_account(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        with pytest.raises(HTTPException) as e:
            await R.billing_portal_session(
                R.PortalSessionIn(origin_url="https://books.example.com"),
                _Req(), USER)
        assert e.value.status_code == 409
    asyncio.run(_run())


def test_portal_session_creates(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, billing={"stripe_customer_id": "cus_123",
                                    "status": "active", "plan": "medium"})
        res = await R.billing_portal_session(
            R.PortalSessionIn(origin_url="https://books.example.com"),
            _Req(), USER)
        assert res["url"].startswith("https://billing.stripe.com/")
        assert fake.portal_created["customer"] == "cus_123"
        assert fake.portal_created["return_url"] == \
            "https://books.example.com/settings"
    asyncio.run(_run())


# ---------------- webhook: signature + claim ----------------
def test_webhook_bad_signature_400(monkeypatch):
    async def _run():
        _fx(monkeypatch, _fake_stripe(sig_ok=False))
        _set_keys(monkeypatch)
        with pytest.raises(HTTPException) as e:
            await R.billing_webhook(_Req(
                body=b"{}", headers={"stripe-signature": "t=1,v1=forged"}))
        assert e.value.status_code == 400
    asyncio.run(_run())


def _session_completed_event(payment_status="paid", plan="medium",
                             kind="books_subscription", event_id="evt_co_1"):
    return {"id": event_id, "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_sub_123", "payment_status": payment_status,
                "customer": "cus_123", "subscription": "sub_123",
                "metadata": {"kind": kind, "tenant_id": "t1", "plan": plan}}}}


def test_webhook_checkout_completed_activates_plan(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe(event=_session_completed_event()))
        _set_keys(monkeypatch)
        _seed_tenant(fdb)

        res = await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))
        assert res == {"received": True}

        t = _tenant(fdb)
        assert t["plan"] == "medium"  # verified webhook moved the plan
        b = t["billing"]
        assert b["stripe_customer_id"] == "cus_123"
        assert b["subscription_id"] == "sub_123"
        assert b["plan"] == "medium"
        assert b["status"] == "active"
    asyncio.run(_run())


def test_webhook_checkout_completed_unpaid_keeps_entitlement(monkeypatch):
    async def _run():
        ev = _session_completed_event(payment_status="unpaid")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb)

        await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))

        t = _tenant(fdb)
        assert t["billing"]["status"] == "incomplete"
        assert t["billing"]["plan"] == "medium"  # intended plan recorded
        assert t["plan"] == "light"  # entitlement waits for money
    asyncio.run(_run())


def test_webhook_ignores_foreign_checkout_sessions(monkeypatch):
    async def _run():
        ev = _session_completed_event(kind="portal_invoice")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb)

        res = await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))
        assert res == {"received": True}
        t = _tenant(fdb)
        assert t["plan"] == "light"
        assert "billing" not in t
    asyncio.run(_run())


# ---------------- webhook: subscription lifecycle ----------------
def _subscription_event(etype, status, event_id="evt_sub_1", **over):
    obj = {"id": "sub_123", "customer": "cus_123", "status": status,
           "current_period_end": EPOCH_END, "cancel_at_period_end": False,
           "metadata": {"kind": "books_subscription", "tenant_id": "t1",
                        "plan": "medium"},
           "items": {"data": []}}
    obj.update(over)
    return {"id": event_id, "type": etype, "data": {"object": obj}}


def test_webhook_subscription_updated_maps_status_and_period(monkeypatch):
    async def _run():
        ev = _subscription_event("customer.subscription.updated", "past_due")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb, plan="medium")

        await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))

        b = _tenant(fdb)["billing"]
        assert b["status"] == "past_due"
        assert b["current_period_end"] == _iso(EPOCH_END)
        assert b["subscription_id"] == "sub_123"
        assert b["plan"] == "medium"
    asyncio.run(_run())


def test_webhook_subscription_deleted_cancels_to_light(monkeypatch):
    async def _run():
        ev = _subscription_event("customer.subscription.deleted", "canceled")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb, plan="medium",
                     billing={"stripe_customer_id": "cus_123",
                              "subscription_id": "sub_123", "plan": "medium",
                              "status": "active"})

        await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))

        t = _tenant(fdb)
        assert t["billing"]["status"] == "canceled"
        assert t["plan"] == "light"
    asyncio.run(_run())


# ---------------- webhook: invoices + referral emission ----------------
def _invoice_event(etype, event_id, amount_paid=3999, invoice_id="in_123"):
    return {"id": event_id, "type": etype,
            "data": {"object": {
                "id": invoice_id, "subscription": "sub_123",
                "customer": "cus_123", "amount_paid": amount_paid,
                "currency": "usd", "created": EPOCH_END - 86400,
                "status_transitions": {"paid_at": EPOCH_END - 86000}}}}


def _seed_subscribed(fdb, referred_by_code="ABC123XY"):
    t = _seed_tenant(fdb, plan="medium", referred_by_code=referred_by_code,
                     billing={"stripe_customer_id": "cus_123",
                              "subscription_id": "sub_123", "plan": "medium",
                              "status": "past_due"})
    fdb.users.docs.append({"id": "u1", "tenant_id": "t1", "role": "owner",
                           "email": "owner@shop.com"})
    return t


def test_webhook_invoice_paid_emits_referral_event_exactly_once(monkeypatch):
    async def _run():
        ev = _invoice_event("invoice.payment_succeeded", "evt_inv_1")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_subscribed(fdb)
        req = _Req(headers={"stripe-signature": "t=1,v1=ok"})

        res = await R.billing_webhook(req)
        assert res == {"received": True}

        # billing state driven by the verified event
        assert _tenant(fdb)["billing"]["status"] == "active"

        events = fdb.referral_payment_events.docs
        assert len(events) == 1
        e0 = events[0]
        assert e0["type"] == "referral.payment"
        assert e0["product"] == "books"
        assert e0["tenant_id"] == "t1"
        assert e0["referred_account_id"] == "t1"
        assert e0["referred_email"] == "owner@shop.com"
        assert e0["referral_code"] == "ABC123XY"
        assert e0["payment_id"] == "in_123"
        assert e0["stripe_event_id"] == "evt_inv_1"
        assert e0["amount_cents"] == 3999
        assert e0["currency"] == "usd"
        assert e0["paid_at"] == _iso(EPOCH_END - 86000)

        # redelivery of the SAME event: claimed already -> no reprocessing
        res2 = await R.billing_webhook(req)
        assert res2 == {"received": True, "duplicate": True}
        assert len(fdb.referral_payment_events.docs) == 1

        # a DIFFERENT event for the same invoice (dashboard resend):
        # processed, but the (product, payment_id) guard emits no second copy
        ev2 = _invoice_event("invoice.payment_succeeded", "evt_inv_2")
        monkeypatch.setitem(sys.modules, "stripe", _fake_stripe(event=ev2))
        res3 = await R.billing_webhook(req)
        assert res3 == {"received": True}
        assert len(fdb.referral_payment_events.docs) == 1
    asyncio.run(_run())


def test_webhook_invoice_paid_without_referral_code_still_emits(monkeypatch):
    async def _run():
        ev = _invoice_event("invoice.payment_succeeded", "evt_inv_9")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        t = _seed_subscribed(fdb)
        del t["referred_by_code"]
        # the seeded doc in the fake is a shared dict; mutate the stored copy
        fdb.tenants.docs[0].pop("referred_by_code", None)

        await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))
        events = fdb.referral_payment_events.docs
        assert len(events) == 1
        assert events[0]["referral_code"] is None
    asyncio.run(_run())


def test_webhook_invoice_failed_sets_past_due(monkeypatch):
    async def _run():
        ev = _invoice_event("invoice.payment_failed", "evt_inv_f1", amount_paid=0)
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_subscribed(fdb)
        fdb.tenants.docs[0]["billing"]["status"] = "active"

        await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))

        assert _tenant(fdb)["billing"]["status"] == "past_due"
        assert fdb.referral_payment_events.docs == []
    asyncio.run(_run())


def test_webhook_replay_of_state_event_is_idempotent(monkeypatch):
    async def _run():
        ev = _subscription_event("customer.subscription.updated", "active")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        req = _Req(headers={"stripe-signature": "t=1,v1=ok"})

        await R.billing_webhook(req)
        first = deepcopy(_tenant(fdb))
        res2 = await R.billing_webhook(req)
        assert res2 == {"received": True, "duplicate": True}
        second = _tenant(fdb)
        assert second["billing"]["status"] == first["billing"]["status"] == "active"
        assert second["plan"] == first["plan"] == "medium"
    asyncio.run(_run())


# ---------------- status endpoint ----------------
def test_billing_status_shape(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch)
        _set_keys(monkeypatch, secret=False, webhook=False)
        _seed_tenant(fdb)
        out = await R.billing_status(USER)
        assert out["configured"] is False
        assert out["plan"] == "light"
        assert out["billing"] is None
        cat = {c["plan"]: c for c in out["catalog"]}
        assert cat["light"]["amount_cents"] == 1899
        assert cat["medium"]["amount_cents"] == 3999
        assert cat["heavy"]["self_serve"] is False
    asyncio.run(_run())


# ---------------- signup referral capture (B4) ----------------
def _auth_fx(monkeypatch):
    captured = {}

    async def _insert_tenant(doc):
        captured.update(doc)

    fake = types.SimpleNamespace(
        users=types.SimpleNamespace(
            find_one=AsyncMock(return_value=None), insert_one=AsyncMock()),
        tenants=types.SimpleNamespace(insert_one=AsyncMock(side_effect=_insert_tenant)),
        accounts=types.SimpleNamespace(),
    )
    monkeypatch.setattr(auth_router, "db", fake)
    monkeypatch.setattr(auth_router, "seed_coa", AsyncMock())
    monkeypatch.setattr("lib.niches.get_pack", lambda nid: {"id": "hvac"})
    return captured


def test_ref_captured_at_signup_normalized(monkeypatch):
    captured = _auth_fx(monkeypatch)
    asyncio.run(auth_router.create_workspace(
        "new@shop.com", "StrongPass1!", "New Owner", "New Shop",
        ref=" abc123xy "))
    assert captured["referred_by_code"] == "ABC123XY"


def test_no_ref_no_field(monkeypatch):
    captured = _auth_fx(monkeypatch)
    asyncio.run(auth_router.create_workspace(
        "new@shop.com", "StrongPass1!", "New Owner", "New Shop"))
    assert "referred_by_code" not in captured


def test_ref_first_touch_never_overwritten(monkeypatch):
    captured = _auth_fx(monkeypatch)
    asyncio.run(auth_router.create_workspace(
        "new@shop.com", "StrongPass1!", "New Owner", "New Shop", ref="FIRST001"))
    tenant_id = captured["id"]
    assert captured["referred_by_code"] == "FIRST001"
    # A later account joining the SAME tenant with a different code must
    # not move attribution: no second tenant insert happens at all.
    fake_db = auth_router.db
    calls_before = fake_db.tenants.insert_one.call_count
    asyncio.run(auth_router.create_workspace(
        "second@shop.com", "StrongPass1!", "Second", "New Shop",
        tenant_id=tenant_id, ref="SECOND02"))
    assert fake_db.tenants.insert_one.call_count == calls_before
    assert captured["referred_by_code"] == "FIRST001"
