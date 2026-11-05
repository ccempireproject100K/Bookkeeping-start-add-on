"""DataClean family charging: the stamped product ladder's sellable
flags, Stripe product provisioning (idempotent), Checkout session
creation behind the key-gate + sellable gate, the webhook as the
only writer of the tenant's dataclean_billing record, and the
entitlement transitions enforced at key minting and at the API door.

Run from the backend directory with:
    python -m pytest tests/test_dataclean_billing.py

No live Stripe or MongoDB needed — a fake `stripe` module is
injected into sys.modules (lib/dataclean_billing.py imports it
lazily, AFTER the key-gate) and the db is faked in-memory, following
tests/test_books_checkout.py conventions.
"""
import asyncio
import os
import sys
import types
from copy import deepcopy

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_dataclean_billing")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from lib import dataclean_api as DCA  # noqa: E402
from lib import dataclean_billing as DCB  # noqa: E402
from routers import dataclean_billing as R  # noqa: E402
from routers import dataclean_admin as ADM  # noqa: E402
import lib.audit as AUD  # noqa: E402


# ---------------- fake stripe SDK ----------------
class SignatureVerificationError(Exception):
    pass


class InvalidRequestError(Exception):
    pass


def _fake_stripe(event=None, sig_ok=True):
    fake = types.SimpleNamespace()
    fake.api_key = None
    fake.error = types.SimpleNamespace(
        SignatureVerificationError=SignatureVerificationError)
    state = {"sessions": [], "products": {}, "prices": {},
             "product_creates": 0, "price_creates": 0}

    def _checkout_create(**kwargs):
        state["sessions"].append(kwargs)
        return types.SimpleNamespace(
            id="cs_dc_123", url="https://checkout.stripe.com/c/cs_dc_123",
            client_secret=("cs_dc_123_secret_x"
                           if kwargs.get("ui_mode") == "elements" else None))

    fake.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_checkout_create))

    def _product_retrieve(pid):
        if pid not in state["products"]:
            raise InvalidRequestError(f"No such product: {pid}")
        return state["products"][pid]

    def _product_create(id=None, **kw):
        state["product_creates"] += 1
        obj = types.SimpleNamespace(id=id, **kw)
        state["products"][id] = obj
        return obj

    def _price_retrieve(pid):
        if pid not in state["prices"]:
            raise InvalidRequestError(f"No such price: {pid}")
        return state["prices"][pid]

    def _price_create(id=None, **kw):
        state["price_creates"] += 1
        obj = types.SimpleNamespace(id=id, **kw)
        state["prices"][id] = obj
        return obj

    fake.Product = types.SimpleNamespace(
        retrieve=_product_retrieve, create=_product_create)
    fake.Price = types.SimpleNamespace(
        retrieve=_price_retrieve, create=_price_create)

    def _construct(payload, sig, secret):
        if not sig_ok:
            raise SignatureVerificationError("bad signature")
        assert secret, "construct_event must receive the webhook secret"
        return event

    fake.Webhook = types.SimpleNamespace(construct_event=_construct)
    fake.state = state
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
                return _UpdateResult(1)
        if upsert:
            doc = {k: v for k, v in (q or {}).items()
                   if not isinstance(v, dict)}
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
        self.dataclean_stripe_products = _FakeCollection()
        self.dataclean_api_keys = _FakeCollection()
        self.dataclean_api_usage = _FakeCollection()


class _Req:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}
        self.client = None

    async def body(self):
        return self._body


USER = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.example",
        "name": "Owner", "role": "owner", "permissions": ["*"]}
EPOCH_END = 1793577600


def _fx(monkeypatch, stripe_fake=None):
    fdb = _FakeDB()
    monkeypatch.setattr(R, "db", fdb)
    monkeypatch.setattr(DCB, "db", fdb)
    monkeypatch.setattr(DCA, "db", fdb)
    monkeypatch.setattr(ADM, "db", fdb)
    monkeypatch.setattr(AUD, "db", fdb)
    if stripe_fake is not None:
        monkeypatch.setitem(sys.modules, "stripe", stripe_fake)
    return fdb


def _set_keys(monkeypatch, with_key=True, webhook=True):
    if with_key:
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    else:
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    if webhook:
        monkeypatch.setenv("STRIPE_DATACLEAN_WEBHOOK_SECRET", "whsec_dc_fake")
    else:
        monkeypatch.delenv("STRIPE_DATACLEAN_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    for env in ("STRIPE_PRICE_DATACLEAN", "STRIPE_PRICE_SECURITY",
                "STRIPE_PRICE_BUNDLE", "SECURITY_PRODUCT_BUILT",
                "DATACLEAN_BILLING_ENFORCED"):
        monkeypatch.delenv(env, raising=False)


def _seed_tenant(fdb, **over):
    t = {"id": "t1", "name": "Acme Plumbing Co.", "plan": "light"}
    t.update(over)
    fdb.tenants.docs.append(t)
    return t


def _tenant(fdb):
    return fdb.tenants.docs[0]


def _event(etype, obj, eid="evt_1"):
    return {"id": eid, "type": etype, "data": {"object": obj}}


def _checkout_event(eid="evt_co"):
    return _event("checkout.session.completed", {
        "id": "cs_dc_123", "mode": "subscription",
        "payment_status": "paid", "customer": "cus_1",
        "subscription": "sub_1",
        "metadata": {"kind": "dataclean_subscription",
                     "tenant_id": "t1", "product": "dataclean"},
    }, eid)


def _sub_obj(**over):
    obj = {
        "id": "sub_1", "customer": "cus_1", "status": "active",
        "current_period_end": EPOCH_END, "cancel_at_period_end": False,
        "metadata": {"kind": "dataclean_subscription",
                     "tenant_id": "t1", "product": "dataclean"},
        "items": {"data": [{"price": {"id": "mae_dataclean_monthly"}}]},
    }
    obj.update(over)
    return obj


# ---------------- the ladder + sellable flags ----------------
def test_catalog_is_the_stamped_ladder(monkeypatch):
    _set_keys(monkeypatch)
    cat = DCB.product_catalog()
    assert cat["dataclean"]["amount_cents"] == 1299
    assert cat["security"]["amount_cents"] == 1899
    assert cat["bundle"]["amount_cents"] == 2799
    assert cat["bundle"]["parts_total_cents"] == 3198
    assert cat["bundle"]["savings_cents"] == 399
    # DataClean sells today; Security + the bundle do not (flag off).
    assert DCB.sellable("dataclean") is True
    assert DCB.sellable("security") is False
    assert DCB.sellable("bundle") is False
    assert DCB.sellable("nope") is False


def test_sellable_flags_flip_with_security_built(monkeypatch):
    _set_keys(monkeypatch)
    monkeypatch.setenv("SECURITY_PRODUCT_BUILT", "1")
    assert DCB.sellable("security") is True
    assert DCB.sellable("bundle") is True
    pub = {p["id"]: p for p in DCB.catalog_public()}
    assert pub["bundle"]["savings_cents"] == 399
    assert pub["security"]["status"] == "live"


# ---------------- provisioning (idempotent) ----------------
def test_ensure_products_is_idempotent(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        fake = sys.modules["stripe"]
        first = await DCB.ensure_products()
        second = await DCB.ensure_products()
        assert first == second
        # retrieve-first: the second run created nothing new
        assert fake.state["product_creates"] == 3
        assert fake.state["price_creates"] == 3
        assert first["dataclean"]["stripe_price_id"] == "mae_dataclean_monthly"
        assert first["security"]["amount_cents"] == 1899
        assert first["bundle"]["amount_cents"] == 2799
        rows = {d["product"]: d for d in fdb.dataclean_stripe_products.docs}
        assert set(rows) == {"dataclean", "security", "bundle"}
    asyncio.run(_run())


def test_ensure_products_requires_key(monkeypatch):
    async def _run():
        _fx(monkeypatch)
        _set_keys(monkeypatch, with_key=False)
        monkeypatch.delitem(sys.modules, "stripe", raising=False)
        with pytest.raises(DCB.BillingNotConfigured):
            await DCB.ensure_products()
    asyncio.run(_run())


# ---------------- checkout ----------------
def test_checkout_503_when_unconfigured(monkeypatch):
    async def _run():
        _fx(monkeypatch)
        _set_keys(monkeypatch, with_key=False)
        monkeypatch.delitem(sys.modules, "stripe", raising=False)
        with pytest.raises(HTTPException) as e:
            await R.dataclean_checkout(
                R.CheckoutIn(product="dataclean",
                             origin_url="https://books.example.com"),
                _Req(), USER)
        assert e.value.status_code == 503
    asyncio.run(_run())


def test_checkout_refuses_unsellable_products(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        for product in ("security", "bundle"):
            with pytest.raises(HTTPException) as e:
                await R.dataclean_checkout(
                    R.CheckoutIn(product=product,
                                 origin_url="https://books.example.com"),
                    _Req(), USER)
            assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e:
            await R.dataclean_checkout(
                R.CheckoutIn(product="gold",
                             origin_url="https://books.example.com"),
                _Req(), USER)
        assert e.value.status_code == 422
    asyncio.run(_run())


def test_checkout_creates_subscription_session(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        out = await R.dataclean_checkout(
            R.CheckoutIn(product="dataclean",
                         origin_url="https://books.example.com"),
            _Req(), USER)
        assert out["checkout_url"].startswith("https://checkout.stripe.com/")
        kw = sys.modules["stripe"].state["sessions"][0]
        assert kw["mode"] == "subscription"
        assert kw["metadata"] == {"kind": "dataclean_subscription",
                                  "tenant_id": "t1", "product": "dataclean"}
        assert kw["subscription_data"]["metadata"] == kw["metadata"]
        item = kw["line_items"][0]
        # no env price + no ensured catalog -> inline price_data at the
        # stamped amount
        assert item["price_data"]["unit_amount"] == 1299
        assert kw["success_url"].endswith(
            "/dataclean-checkout?checkout=success&session_id={CHECKOUT_SESSION_ID}")
        # creating a session changed NOTHING on the tenant
        assert "dataclean_billing" not in _tenant(fdb)
    asyncio.run(_run())


def test_checkout_elements_mode_returns_client_secret(monkeypatch):
    """The embedded checkout (ui_mode='elements'): the session is
    created for in-page rendering — client_secret out, return_url
    instead of success/cancel URLs, same metadata + honesty gates."""
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        out = await R.dataclean_checkout(
            R.CheckoutIn(product="dataclean",
                         origin_url="https://books.example.com",
                         ui_mode="elements"),
            _Req(), USER)
        assert out["client_secret"] == "cs_dc_123_secret_x"
        assert out["ui_mode"] == "elements"
        kw = sys.modules["stripe"].state["sessions"][0]
        assert kw["ui_mode"] == "elements"
        assert kw["return_url"].endswith(
            "/dataclean-checkout?checkout=success&session_id={CHECKOUT_SESSION_ID}")
        assert "success_url" not in kw and "cancel_url" not in kw
        assert kw["metadata"]["product"] == "dataclean"
        assert "dataclean_billing" not in _tenant(fdb)
    asyncio.run(_run())


def test_checkout_uses_env_price_id_when_set(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        monkeypatch.setenv("STRIPE_PRICE_DATACLEAN", "price_dashboard_1")
        _seed_tenant(fdb)
        out = await R.dataclean_checkout(
            R.CheckoutIn(product="dataclean",
                         origin_url="https://books.example.com"),
            _Req(), USER)
        kw = sys.modules["stripe"].state["sessions"][0]
        assert kw["line_items"][0] == {"price": "price_dashboard_1",
                                       "quantity": 1}
        assert out["session_id"] == "cs_dc_123"
    asyncio.run(_run())


def test_status_reports_unconfigured_and_ladder(monkeypatch):
    async def _run():
        _fx(monkeypatch)
        _set_keys(monkeypatch, with_key=False)
        out = await R.dataclean_billing_status(USER)
        assert out["configured"] is False
        assert out["billing"]["status"] is None
        amounts = {p["id"]: p["amount_cents"] for p in out["catalog"]}
        assert amounts == {"dataclean": 1299, "security": 1899,
                           "bundle": 2799}
    asyncio.run(_run())


# ---------------- webhook ----------------
def test_webhook_503_without_secret_and_400_on_bad_signature(monkeypatch):
    async def _run():
        _fx(monkeypatch, _fake_stripe(event=_checkout_event()))
        _set_keys(monkeypatch, webhook=False)
        with pytest.raises(HTTPException) as e:
            await R.dataclean_webhook(_Req(headers={"stripe-signature": "x"}))
        assert e.value.status_code == 503

        _fx(monkeypatch, _fake_stripe(event=_checkout_event(), sig_ok=False))
        _set_keys(monkeypatch)
        with pytest.raises(HTTPException) as e:
            await R.dataclean_webhook(_Req(headers={"stripe-signature": "x"}))
        assert e.value.status_code == 400
    asyncio.run(_run())


def test_webhook_checkout_completed_writes_record(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe(event=_checkout_event()))
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        out = await R.dataclean_webhook(_Req(headers={"stripe-signature": "s"}))
        assert out == {"received": True}
        rec = _tenant(fdb)["dataclean_billing"]
        assert rec["product"] == "dataclean"
        assert rec["status"] == "active"
        assert rec["subscription_id"] == "sub_1"
        assert rec["stripe_customer_id"] == "cus_1"
        assert await DCB.tenant_dataclean_entitled("t1") is True
        # the books subdoc was never touched
        assert "billing" not in _tenant(fdb)
        # redelivery is claimed, not reprocessed
        out2 = await R.dataclean_webhook(_Req(headers={"stripe-signature": "s"}))
        assert out2 == {"received": True, "duplicate": True}
    asyncio.run(_run())


def test_webhook_ignores_other_flows(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe(event=_event(
            "checkout.session.completed", {
                "id": "cs_books", "payment_status": "paid",
                "customer": "cus_9", "subscription": "sub_9",
                "metadata": {"kind": "books_subscription",
                             "tenant_id": "t1", "plan": "light"},
            })))
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        out = await R.dataclean_webhook(_Req(headers={"stripe-signature": "s"}))
        assert out == {"received": True}
        assert "dataclean_billing" not in _tenant(fdb)
    asyncio.run(_run())


def test_webhook_subscription_lifecycle_drives_entitlement(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe(event=_checkout_event()))
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        await R.dataclean_webhook(_Req(headers={"stripe-signature": "s"}))

        async def _send(event):
            monkeypatch.setitem(sys.modules, "stripe",
                                _fake_stripe(event=event))
            return await R.dataclean_webhook(
                _Req(headers={"stripe-signature": "s"}))

        # past_due: dunning grace — access continues
        await _send(_event("customer.subscription.updated",
                           _sub_obj(status="past_due"), "evt_pd"))
        rec = _tenant(fdb)["dataclean_billing"]
        assert rec["status"] == "past_due"
        assert rec["current_period_end"]
        assert await DCB.tenant_dataclean_entitled("t1") is True

        # invoice failure keeps past_due + stamps the failure
        await _send(_event("invoice.payment_failed",
                           {"id": "in_1", "subscription": "sub_1",
                            "customer": "cus_1"}, "evt_if"))
        rec = _tenant(fdb)["dataclean_billing"]
        assert rec["status"] == "past_due"
        assert rec["last_failed_invoice_id"] == "in_1"

        # a successful retry reactivates
        await _send(_event("invoice.payment_succeeded", {
            "id": "in_2", "subscription": "sub_1", "customer": "cus_1",
            "created": EPOCH_END,
            "status_transitions": {"paid_at": EPOCH_END}}, "evt_ip"))
        rec = _tenant(fdb)["dataclean_billing"]
        assert rec["status"] == "active"
        assert rec["last_paid_invoice_id"] == "in_2"

        # deletion cancels — entitlement ends
        await _send(_event("customer.subscription.deleted",
                           _sub_obj(status="canceled"), "evt_del"))
        rec = _tenant(fdb)["dataclean_billing"]
        assert rec["status"] == "canceled"
        assert await DCB.tenant_dataclean_entitled("t1") is False
    asyncio.run(_run())


# ---------------- entitlement gates ----------------
def test_entitlement_record_rules(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch)
        _set_keys(monkeypatch)
        # no tenant at all -> grandfathered (pre-charging workspace)
        assert await DCB.tenant_dataclean_entitled("t1") is True
        _seed_tenant(fdb)
        assert await DCB.tenant_dataclean_entitled("t1") is True
        # bundle subscription grants DataClean
        _tenant(fdb)["dataclean_billing"] = {
            "product": "bundle", "status": "active"}
        assert await DCB.tenant_dataclean_entitled("t1") is True
        # a security-only record does NOT grant DataClean
        _tenant(fdb)["dataclean_billing"] = {
            "product": "security", "status": "active"}
        assert await DCB.tenant_dataclean_entitled("t1") is False
        # incomplete (no money landed) does not entitle
        _tenant(fdb)["dataclean_billing"] = {
            "product": "dataclean", "status": "incomplete"}
        assert await DCB.tenant_dataclean_entitled("t1") is False
        # strict mode: no record -> not entitled
        del _tenant(fdb)["dataclean_billing"]
        monkeypatch.setenv("DATACLEAN_BILLING_ENFORCED", "1")
        assert await DCB.tenant_dataclean_entitled("t1") is False
    asyncio.run(_run())


def test_gates_block_canceled_workspace(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, dataclean_billing={
            "product": "dataclean", "status": "canceled"})
        # key minting refuses
        with pytest.raises(HTTPException) as e:
            await ADM.create_key(ADM.CreateKeyBody(), USER)
        assert e.value.status_code == 403
        # an already-issued key stops at the API door
        doc, full = DCA.new_key_doc(tenant_id="t1", created_by="o@x",
                                    label="k")
        await fdb.dataclean_api_keys.insert_one(doc)
        with pytest.raises(HTTPException) as e:
            await DCA.get_dataclean_actor(
                _HeaderReq({"X-API-Key": full}))
        assert e.value.status_code == 403
        assert e.value.detail["code"] == "subscription_inactive"
        # reactivating the record opens both doors again
        _tenant(fdb)["dataclean_billing"]["status"] = "active"
        out = await ADM.create_key(ADM.CreateKeyBody(), USER)
        assert out["key"].startswith("mdck_")
        actor = await DCA.get_dataclean_actor(
            _HeaderReq({"X-API-Key": full}))
        assert actor["tenant_id"] == "t1"
    asyncio.run(_run())


class _HeaderReq:
    def __init__(self, headers):
        self.headers = headers
        self.client = types.SimpleNamespace(host="203.0.113.9")
