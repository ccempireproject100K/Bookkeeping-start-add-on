"""Books Heavy self-serve checkout (owner stamp 2026-11-19).

Heavy pricing: $99/mo covering up to 3 shops, + $21/mo per additional
shop; a "shop" is a workspace (tenant with its own books) the login
owns. All transactions included — no volume caps anywhere in the math.

Covered here:
  * the pure price computation (1 / 3 / 5 / 10 shops),
  * checkout line items in inline-price and env Price-id modes,
  * the router checkout sizing items from the account's entity count,
  * the webhook stamping plan + billed shop count,
  * the shop-added adjustment (POST /billing/heavy/resync ->
    Subscription.modify with prorations), grow and shrink.

Run from the backend directory with:
    python -m pytest tests/test_books_heavy_checkout.py

Same conventions as tests/test_books_checkout.py: a fake `stripe`
module in sys.modules and a fake in-memory db.
"""
import asyncio
import os
import sys
import types
from copy import deepcopy

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_books_heavy")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from lib import billing as B  # noqa: E402
from routers import billing as R  # noqa: E402


# ---------------- fake stripe SDK ----------------
class SignatureVerificationError(Exception):
    pass


def _fake_stripe(event=None, sub_items=None):
    fake = types.SimpleNamespace()
    fake.api_key = None
    fake.error = types.SimpleNamespace(
        SignatureVerificationError=SignatureVerificationError)
    created = {}
    modified = {}

    def _checkout_create(**kwargs):
        created.update(kwargs)
        return types.SimpleNamespace(
            id="cs_heavy_1", url="https://checkout.stripe.com/c/cs_heavy_1")

    fake.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_checkout_create))

    def _sub_retrieve(sub_id):
        return {"id": sub_id, "items": {"data": deepcopy(sub_items or [])}}

    def _sub_modify(sub_id, **kwargs):
        modified.update({"id": sub_id, **kwargs})
        return {"id": sub_id}

    fake.Subscription = types.SimpleNamespace(
        retrieve=_sub_retrieve, modify=_sub_modify)

    def _construct(payload, sig, secret):
        assert secret, "construct_event must receive the webhook secret"
        return event

    fake.Webhook = types.SimpleNamespace(construct_event=_construct)
    fake.created = created
    fake.modified = modified
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


def _user_with_shops(n):
    """A login owning n shops (workspaces), the first being active."""
    return {
        "id": "u1", "tenant_id": "t1", "email": "owner@shops.com",
        "name": "Owner", "role": "owner",
        "memberships": [{"tenant_id": f"t{i}", "role": "owner"}
                        for i in range(1, n + 1)],
    }


def _fx(monkeypatch, stripe_fake=None):
    fdb = _FakeDB()
    monkeypatch.setattr(R, "db", fdb)
    if stripe_fake is not None:
        monkeypatch.setitem(sys.modules, "stripe", stripe_fake)
    return fdb


def _set_keys(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_BILLING_WEBHOOK_SECRET", "whsec_billing_fake")
    for env in ("STRIPE_PRICE_LIGHT", "STRIPE_PRICE_MEDIUM",
                "STRIPE_PRICE_HEAVY", "STRIPE_PRICE_HEAVY_EXTRA_SHOP"):
        monkeypatch.delenv(env, raising=False)


def _seed_tenant(fdb, tid="t1", **over):
    t = {"id": tid, "name": f"Shop {tid}", "plan": "light",
         "trial_ends_at": "2020-01-01T00:00:00+00:00"}
    t.update(over)
    fdb.tenants.docs.append(t)
    return t


# ---------------- pure price computation ----------------
def test_heavy_price_computation():
    assert B.heavy_amount_cents(1) == 9900
    assert B.heavy_amount_cents(2) == 9900
    assert B.heavy_amount_cents(3) == 9900
    assert B.heavy_amount_cents(4) == 12000
    assert B.heavy_amount_cents(5) == 14100
    assert B.heavy_amount_cents(10) == 24600
    assert B.heavy_extra_shops(1) == 0
    assert B.heavy_extra_shops(3) == 0
    assert B.heavy_extra_shops(5) == 2
    assert B.heavy_extra_shops(10) == 7


def test_heavy_line_items_inline_prices():
    one = B.line_items("heavy", 1)
    assert len(one) == 1
    assert one[0]["price_data"]["unit_amount"] == 9900
    assert one[0]["quantity"] == 1

    three = B.line_items("heavy", 3)
    assert len(three) == 1  # included shops ride on the base alone

    five = B.line_items("heavy", 5)
    assert len(five) == 2
    assert five[1]["price_data"]["unit_amount"] == 2100
    assert five[1]["quantity"] == 2

    ten = B.line_items("heavy", 10)
    assert len(ten) == 2
    assert ten[1]["quantity"] == 7
    total = sum(i["price_data"]["unit_amount"] * i["quantity"] for i in ten)
    assert total == 24600


def test_heavy_line_items_env_price_ids(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_HEAVY", "price_heavy_base")
    monkeypatch.setenv("STRIPE_PRICE_HEAVY_EXTRA_SHOP", "price_heavy_extra")
    items = B.line_items("heavy", 5)
    assert items == [{"price": "price_heavy_base", "quantity": 1},
                     {"price": "price_heavy_extra", "quantity": 2}]
    assert B.plan_for_price_id("price_heavy_base") == "heavy"
    assert B.plan_for_price_id("price_heavy_extra") == "heavy"


def test_light_medium_line_items_unchanged():
    assert B.line_items("light")[0]["price_data"]["unit_amount"] == 1899
    assert B.line_items("medium")[0]["price_data"]["unit_amount"] == 3999
    assert len(B.line_items("light")) == 1
    assert len(B.line_items("medium")) == 1


# ---------------- router checkout sizes from the entity count ----------------
@pytest.mark.parametrize("shops,amount,items_n,extra_qty", [
    (1, 9900, 1, 0), (3, 9900, 1, 0), (5, 14100, 2, 2), (10, 24600, 2, 7),
])
def test_checkout_heavy_sizes_from_entity_count(monkeypatch, shops, amount,
                                                items_n, extra_qty):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        res = await R.subscribe_checkout(
            R.CheckoutIn(plan="heavy", origin_url="https://books.example.com"),
            _Req(), _user_with_shops(shops))
        assert res["shops"] == shops
        assert res["amount_cents"] == amount
        items = fake.created["line_items"]
        assert len(items) == items_n
        if extra_qty:
            assert items[1]["quantity"] == extra_qty
            assert items[1]["price_data"]["unit_amount"] == 2100
        assert fake.created["metadata"]["plan"] == "heavy"
        assert fake.created["metadata"]["shops"] == str(shops)
        assert fake.created["subscription_data"]["metadata"]["shops"] == str(shops)
        # Checkout creation alone changes no tenant state.
        t = fdb.tenants.docs[0]
        assert t["plan"] == "light"
        assert "billing" not in t
    asyncio.run(_run())


# ---------------- webhook stamps plan + billed shops ----------------
def _session_completed_event(shops="5"):
    return {
        "id": "evt_heavy_1", "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_heavy_1", "mode": "subscription",
            "payment_status": "paid", "customer": "cus_9",
            "subscription": "sub_9",
            "metadata": {"kind": "books_subscription", "tenant_id": "t1",
                         "plan": "heavy", "shops": shops},
        }},
    }


def _subscription_event(items, event_id="evt_heavy_2"):
    return {
        "id": event_id, "type": "customer.subscription.updated",
        "data": {"object": {
            "id": "sub_9", "customer": "cus_9", "status": "active",
            "current_period_end": 1793577600, "cancel_at_period_end": False,
            "metadata": {"kind": "books_subscription", "tenant_id": "t1",
                         "plan": "heavy", "shops": "5"},
            "items": {"data": items},
        }},
    }


_BASE_ITEM = {"id": "si_base", "quantity": 1,
              "price": {"id": "price_x", "unit_amount": 9900}}


def _extra_item(qty):
    return {"id": "si_extra", "quantity": qty,
            "price": {"id": "price_y", "unit_amount": 2100}}


def test_webhook_heavy_checkout_then_subscription_shops(monkeypatch):
    async def _run():
        fake = _fake_stripe(event=_session_completed_event("5"))
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        res = await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))
        assert res == {"received": True}
        t = fdb.tenants.docs[0]
        assert t["plan"] == "heavy"
        assert t["billing"]["plan"] == "heavy"
        assert t["billing"]["shops"] == 5
        assert t["billing"]["status"] == "active"

        # A later subscription event re-derives shops from the items:
        # extra quantity 7 -> 10 billed shops.
        fake2 = _fake_stripe(event=_subscription_event([_BASE_ITEM, _extra_item(7)]))
        monkeypatch.setitem(sys.modules, "stripe", fake2)
        res = await R.billing_webhook(_Req(headers={"stripe-signature": "t=1,v1=ok"}))
        assert res == {"received": True}
        t = fdb.tenants.docs[0]
        assert t["billing"]["shops"] == 10
        assert t["billing"]["plan"] == "heavy"
    asyncio.run(_run())


# ---------------- shop-added adjustment ----------------
def test_resync_grows_extra_shop_quantity(monkeypatch):
    async def _run():
        fake = _fake_stripe(sub_items=[_BASE_ITEM])
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, billing={
            "provider": "stripe", "plan": "heavy", "status": "active",
            "subscription_id": "sub_9", "stripe_customer_id": "cus_9",
            "shops": 3})
        res = await R.heavy_resync(_user_with_shops(5))
        assert res["shops"] == 5
        assert res["amount_cents"] == 14100
        mod = fake.modified
        assert mod["id"] == "sub_9"
        assert mod["proration_behavior"] == "create_prorations"
        items = mod["items"]
        assert items[0] == {"id": "si_base", "quantity": 1}
        assert items[1]["price_data"]["unit_amount"] == 2100
        assert items[1]["quantity"] == 2
        assert fdb.tenants.docs[0]["billing"]["shops"] == 5
    asyncio.run(_run())


def test_resync_shrinks_and_removes_extra_item(monkeypatch):
    async def _run():
        fake = _fake_stripe(sub_items=[_BASE_ITEM, _extra_item(7)])
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, billing={
            "provider": "stripe", "plan": "heavy", "status": "active",
            "subscription_id": "sub_9", "stripe_customer_id": "cus_9",
            "shops": 10})
        res = await R.heavy_resync(_user_with_shops(3))
        assert res["shops"] == 3
        assert res["amount_cents"] == 9900
        items = fake.modified["items"]
        assert items[0] == {"id": "si_base", "quantity": 1}
        assert items[1] == {"id": "si_extra", "deleted": True}
        assert fdb.tenants.docs[0]["billing"]["shops"] == 3
    asyncio.run(_run())


def test_resync_without_heavy_subscription_409(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        with pytest.raises(HTTPException) as e:
            await R.heavy_resync(_user_with_shops(2))
        assert e.value.status_code == 409
    asyncio.run(_run())
