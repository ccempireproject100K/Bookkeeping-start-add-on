"""Invoice payments via Stripe Connect: fee math to the cent, key
gating, Connect onboarding state, direct-charge pay links, and
webhook reconciliation that is idempotent by event id (and, as a
second layer, claim-guarded per session).

Run from the backend directory with:
    python -m pytest tests/test_invoice_payments.py -n 0

No live Stripe or MongoDB needed — a fake `stripe` module is injected
into sys.modules (lib/stripe_connect.py imports it lazily, AFTER the
key-gate) and the db is faked in-memory, following
tests/test_stripe_integration.py conventions.
"""
import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_invoice_payments")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402

import lib.audit as audit_mod  # noqa: E402
import lib.stripe_connect as SC  # noqa: E402
import routers.invoice_payments as IP  # noqa: E402


# ---------------- fake stripe SDK ----------------
class _SigError(Exception):
    pass


def _fake_stripe(event=None, sig_ok=True, charges_enabled=True):
    fake = types.SimpleNamespace()
    fake.api_key = None
    fake.error = types.SimpleNamespace(SignatureVerificationError=_SigError)
    fake.created_sessions = []
    fake.created_accounts = []
    fake.created_links = []
    fake.account = {"id": "acct_1234567890", "charges_enabled": charges_enabled,
                    "payouts_enabled": charges_enabled,
                    "details_submitted": charges_enabled}

    def _acct_create(**kwargs):
        fake.created_accounts.append(kwargs)
        return {**fake.account, "charges_enabled": False,
                "payouts_enabled": False, "details_submitted": False}

    def _acct_retrieve(account_id):
        assert account_id == fake.account["id"]
        return dict(fake.account)

    def _link_create(**kwargs):
        fake.created_links.append(kwargs)
        return {"url": "https://connect.stripe.com/setup/test-link",
                "expires_at": 9999999999}

    def _session_create(**kwargs):
        fake.created_sessions.append(kwargs)
        return {"id": "cs_test_999",
                "url": "https://checkout.stripe.com/c/cs_test_999"}

    fake.Account = types.SimpleNamespace(create=_acct_create,
                                         retrieve=_acct_retrieve)
    fake.AccountLink = types.SimpleNamespace(create=_link_create)
    fake.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_session_create))

    def _construct(payload, sig, secret):
        if not sig_ok:
            raise _SigError("bad signature")
        assert secret, "construct_event must receive the webhook secret"
        return event

    fake.Webhook = types.SimpleNamespace(construct_event=_construct)
    return fake


# ---------------- fake in-memory db ----------------
def _get(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict) and "$in" in v:
            if _get(doc, k) not in v["$in"]:
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if _get(doc, k) == v["$ne"]:
                return False
        elif _get(doc, k) != v:
            return False
    return True


def _apply_set(doc, changes):
    for k, v in changes.items():
        parts = k.split(".")
        cur = doc
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = v


class _UpdateResult:
    def __init__(self, matched):
        self.matched_count = matched


class _FakeCollection:
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

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update, upsert=False):
        matched = 0
        for d in self.docs:
            if _matches(d, q):
                matched += 1
                for op, changes in update.items():
                    if op == "$set":
                        _apply_set(d, changes)
                    elif op == "$push":
                        for f, v in changes.items():
                            d.setdefault(f, []).append(v)
                    elif op == "$unset":
                        for f in changes:
                            d.pop(f, None)
        if not matched and upsert:
            doc = {}
            for k, v in (q or {}).items():
                if not isinstance(v, dict):
                    _apply_set(doc, {k: v})
            for op, changes in update.items():
                if op == "$set":
                    _apply_set(doc, changes)
            self.docs.append(doc)
            matched = 1
        return _UpdateResult(matched)

    async def find_one_and_update(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                before = dict(d)
                for op, changes in update.items():
                    if op == "$set":
                        _apply_set(d, changes)
                return before
        return None


class _FakeDB:
    def __init__(self):
        self.tenants = _FakeCollection()
        self.invoices = _FakeCollection()
        self.payment_transactions = _FakeCollection()
        self.stripe_webhook_events = _FakeCollection()
        self.audit_log = _FakeCollection()


class _Req:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    async def body(self):
        return self._body


TENANT = "t1"
ACCT = "acct_1234567890"
INV_ID = "inv-abcdef123456"
ADMIN = {"id": "u1", "email": "owner@acme.test", "name": "Owner",
         "role": "owner", "tenant_id": TENANT}


def _fx(monkeypatch, stripe_fake=None):
    fdb = _FakeDB()
    monkeypatch.setattr(IP, "db", fdb)
    monkeypatch.setattr(audit_mod, "db", fdb)
    if stripe_fake is not None:
        monkeypatch.setitem(sys.modules, "stripe", stripe_fake)
    return fdb


def _set_keys(monkeypatch, secret=True, connect_secret=True):
    if secret:
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    else:
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    if connect_secret:
        monkeypatch.setenv("STRIPE_CONNECT_WEBHOOK_SECRET", "whsec_connect_fake")
    else:
        monkeypatch.delenv("STRIPE_CONNECT_WEBHOOK_SECRET", raising=False)
    for name in ("INVOICE_CARD_APP_FEE_BPS", "INVOICE_ACH_TOTAL_BPS",
                 "INVOICE_ACH_TOTAL_CAP_CENTS", "INVOICE_ACH_STRIPE_BPS",
                 "INVOICE_ACH_STRIPE_CAP_CENTS"):
        monkeypatch.delenv(name, raising=False)


def _seed_tenant(fdb, connect=None):
    tenant = {"id": TENANT, "name": "Acme Plumbing Co."}
    if connect is not None:
        tenant["stripe_connect"] = connect
    fdb.tenants.docs.append(tenant)
    return tenant


def _seed_invoice(fdb, total=185000, paid=0, currency="USD"):
    inv = {"id": INV_ID, "tenant_id": TENANT, "customer_id": "c1",
           "client_name": "Jane Client", "total_cents": total,
           "paid_cents": paid, "status": "issued", "payments": [],
           "issue_entry": "INV-0007", "currency": currency,
           "created_at": "2026-10-01T00:00:00"}
    fdb.invoices.docs.append(inv)
    return inv


def _connected(charges=True):
    return {"account_id": ACCT, "charges_enabled": charges,
            "payouts_enabled": charges, "details_submitted": charges,
            "onboarded_at": "2026-10-20T00:00:00", "updated_at": "2026-10-20T00:00:00"}


def _mock_ledger(monkeypatch):
    drafts = []

    async def _post(db, tenant_id, user, draft, status="posted",
                    idempotency_key=None):
        drafts.append(draft)
        return {"number": "JE-2001", "id": "je9"}

    monkeypatch.setattr(IP.L, "post_journal_entry", _post)
    monkeypatch.setattr(IP.FXP, "post_realized_fx",
                        AsyncMock(return_value=None))
    return drafts


# ---------------- fee math (pure) ----------------
def test_fees_card_to_the_cent():
    fees = SC.fees_for("card", 185000)  # $1,850 invoice
    assert fees["application_fee_cents"] == 370          # 0.2%
    assert fees["stripe_fee_estimate_cents"] == 5395     # 2.9% + 30c
    assert fees["member_total_fee_cents"] == 5765        # 3.1% + 30c
    small = SC.fees_for("card", 100)                     # $1.00
    assert small["application_fee_cents"] == 0
    assert small["stripe_fee_estimate_cents"] == 33


def test_fees_ach_spread_and_cap():
    assert SC.fees_for("ach", 10000) == {                # $100
        "application_fee_cents": 20, "stripe_fee_estimate_cents": 80,
        "member_total_fee_cents": 100}
    assert SC.fees_for("ach", 5000)["application_fee_cents"] == 10     # $50
    assert SC.fees_for("ach", 62500)["application_fee_cents"] == 125   # $625: stripe hits its $5 cap
    big = SC.fees_for("ach", 250000)                                   # $2,500
    assert big["member_total_fee_cents"] == 1000                       # all-in cap $10
    assert big["application_fee_cents"] == 500
    assert big["stripe_fee_estimate_cents"] == 500


def test_fees_env_override(monkeypatch):
    monkeypatch.setenv("INVOICE_CARD_APP_FEE_BPS", "40")
    assert SC.fees_for("card", 185000)["application_fee_cents"] == 740


# ---------------- key gating ----------------
def test_connect_start_503_when_unconfigured(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch)  # no stripe module at all
        _set_keys(monkeypatch, secret=False)
        monkeypatch.delitem(sys.modules, "stripe", raising=False)
        _seed_tenant(fdb)
        try:
            await IP.connect_start(IP.OriginIn(
                origin_url="https://books.example.com"), _Req(), ADMIN)
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 503
            assert "STRIPE_SECRET_KEY" in e.detail
    asyncio.run(_run())


def test_pay_link_503_when_unconfigured(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch)
        _set_keys(monkeypatch, secret=False)
        monkeypatch.delitem(sys.modules, "stripe", raising=False)
        _seed_tenant(fdb, _connected())
        _seed_invoice(fdb)
        try:
            await IP.create_pay_link(INV_ID, IP.PayLinkIn(
                origin_url="https://books.example.com"), _Req(), ADMIN)
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 503
    asyncio.run(_run())


def test_webhook_503_when_connect_secret_unset(monkeypatch):
    async def _run():
        _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch, secret=True, connect_secret=False)
        try:
            await IP.connect_webhook(_Req(headers={"stripe-signature": "t=1,v1=x"}))
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 503
    asyncio.run(_run())


def test_webhook_bad_signature_400(monkeypatch):
    async def _run():
        _fx(monkeypatch, _fake_stripe(sig_ok=False))
        _set_keys(monkeypatch)
        try:
            await IP.connect_webhook(_Req(
                body=b"{}", headers={"stripe-signature": "t=1,v1=forged"}))
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 400
    asyncio.run(_run())


# ---------------- Connect onboarding ----------------
def test_connect_start_creates_account_and_link(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb)

        res = await IP.connect_start(IP.OriginIn(
            origin_url="https://books.example.com"), _Req(), ADMIN)

        assert res["url"].startswith("https://connect.stripe.com/")
        assert res["account_tail"] == "7890"
        assert len(fake.created_accounts) == 1
        kw = fake.created_accounts[0]
        assert kw["type"] == "express" and kw["country"] == "US"
        assert kw["metadata"]["tenant_id"] == TENANT
        assert len(fake.created_links) == 1
        assert fake.created_links[0]["account"] == ACCT
        tenant = fdb.tenants.docs[0]
        assert tenant["stripe_connect"]["account_id"] == ACCT

        # second start reuses the account — no duplicate creation
        await IP.connect_start(IP.OriginIn(
            origin_url="https://books.example.com"), _Req(), ADMIN)
        assert len(fake.created_accounts) == 1
    asyncio.run(_run())


def test_connect_status_without_account_is_honest(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed_tenant(fdb)
        res = await IP.connect_status(ADMIN)
        assert res["configured"] is True
        assert res["connected"] is False
        assert res["charges_enabled"] is False
        assert res["account_tail"] is None
    asyncio.run(_run())


def test_connect_status_refreshes_from_stripe(monkeypatch):
    async def _run():
        fake = _fake_stripe(charges_enabled=True)
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected(charges=False))  # stored says not ready
        res = await IP.connect_status(ADMIN)
        assert res["connected"] is True and res["charges_enabled"] is True
        assert res["state_source"] == "stripe"
        # and the fresh state was persisted
        assert fdb.tenants.docs[0]["stripe_connect"]["charges_enabled"] is True
    asyncio.run(_run())


# ---------------- pay links ----------------
def test_pay_link_refused_without_connect(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed_tenant(fdb)  # no Stripe connected
        _seed_invoice(fdb)
        try:
            await IP.create_pay_link(INV_ID, IP.PayLinkIn(
                origin_url="https://books.example.com"), _Req(), ADMIN)
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 409
            assert e.detail["connect"]["connected"] is False
    asyncio.run(_run())


def test_pay_link_refused_until_charges_enabled(monkeypatch):
    async def _run():
        fake = _fake_stripe(charges_enabled=False)
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected(charges=False))
        _seed_invoice(fdb)
        try:
            await IP.create_pay_link(INV_ID, IP.PayLinkIn(
                origin_url="https://books.example.com"), _Req(), ADMIN)
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 409
            assert "onboarding isn't finished" in e.detail["message"]
        assert fake.created_sessions == []
    asyncio.run(_run())


def test_pay_link_card_session_math(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected())
        _seed_invoice(fdb)  # $1,850.00 balance

        res = await IP.create_pay_link(INV_ID, IP.PayLinkIn(
            rail="card", origin_url="https://books.example.com"),
            _Req(), ADMIN)

        assert res["pay_url"].startswith("https://checkout.stripe.com/")
        assert res["payment_state"] == "link_created"
        kw = fake.created_sessions[0]
        assert kw["stripe_account"] == ACCT          # direct charge
        assert kw["payment_method_types"] == ["card"]
        assert kw["line_items"][0]["price_data"]["unit_amount"] == 185000
        assert kw["payment_intent_data"]["application_fee_amount"] == 370
        assert kw["metadata"]["kind"] == "connect_invoice"
        tx = fdb.payment_transactions.docs[0]
        assert tx["kind"] == "connect_invoice" and tx["rail"] == "card"
        assert tx["application_fee_cents"] == 370
        inv = fdb.invoices.docs[0]
        assert inv["payment"]["state"] == "link_created"
        assert inv["payment"]["application_fee_cents"] == 370
    asyncio.run(_run())


def test_pay_link_ach_uses_capped_spread(monkeypatch):
    async def _run():
        fake = _fake_stripe()
        fdb = _fx(monkeypatch, fake)
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected())
        _seed_invoice(fdb, total=250000)  # $2,500 — past both caps

        res = await IP.create_pay_link(INV_ID, IP.PayLinkIn(
            rail="ach", origin_url="https://books.example.com"),
            _Req(), ADMIN)

        kw = fake.created_sessions[0]
        assert kw["payment_method_types"] == ["us_bank_account"]
        assert kw["payment_intent_data"]["application_fee_amount"] == 500
        assert res["fees"]["member_total_fee_cents"] == 1000  # $10 all-in cap
    asyncio.run(_run())


def test_pay_link_paid_invoice_and_non_usd(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe())
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected())
        _seed_invoice(fdb, paid=185000)
        try:
            await IP.create_pay_link(INV_ID, IP.PayLinkIn(
                origin_url="https://books.example.com"), _Req(), ADMIN)
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 409

        fdb.invoices.docs.clear()
        _seed_invoice(fdb, currency="EUR")
        try:
            await IP.create_pay_link(INV_ID, IP.PayLinkIn(
                origin_url="https://books.example.com"), _Req(), ADMIN)
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 422
            assert "USD-only" in e.detail
    asyncio.run(_run())


# ---------------- webhook reconciliation ----------------
def _session_event(event_id, etype="checkout.session.completed",
                   payment_status="paid", session_id="cs_test_999",
                   kind="connect_invoice"):
    return {"id": event_id, "type": etype, "account": ACCT,
            "data": {"object": {
                "id": session_id, "payment_status": payment_status,
                "payment_intent": "pi_test_1", "amount_total": 185000,
                "metadata": {"kind": kind, "tenant_id": TENANT,
                             "invoice_id": INV_ID, "rail": "card"}}}}


def _seed_open_session(fdb):
    fdb.payment_transactions.docs.append({
        "session_id": "cs_test_999", "tenant_id": TENANT,
        "kind": "connect_invoice", "invoice_id": INV_ID, "account_id": ACCT,
        "rail": "card", "amount_cents": 185000, "currency": "usd",
        "application_fee_cents": 370, "stripe_fee_estimate_cents": 5395,
        "status": "initiated", "payment_status": "pending", "fulfilled": False})


def _wh_req():
    return _Req(body=b"{}", headers={"stripe-signature": "t=1,v1=ok"})


def test_webhook_fulfills_exactly_once(monkeypatch):
    async def _run():
        ev = _session_event("evt_1")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected())
        _seed_invoice(fdb)
        _seed_open_session(fdb)
        drafts = _mock_ledger(monkeypatch)

        res = await IP.connect_webhook(_wh_req())
        assert res == {"received": True}
        assert len(drafts) == 1
        draft = drafts[0]
        assert draft["source"] == "stripe_connect"
        assert draft["lines"][0]["account_code"] == "1050"
        assert draft["lines"][0]["debit"] == 185000
        assert draft["lines"][1]["account_code"] == "1200"
        inv = fdb.invoices.docs[0]
        assert inv["status"] == "paid" and inv["paid_cents"] == 185000
        assert inv["payment"]["state"] == "paid"
        assert len(inv["payments"]) == 1
        assert inv["payments"][0]["reference"] == "cs_test_999"
        assert inv["payments"][0]["source"] == "stripe_connect"

        # duplicate delivery of the SAME event id: no reprocessing
        res2 = await IP.connect_webhook(_wh_req())
        assert res2 == {"received": True, "duplicate": True}
        assert len(drafts) == 1

        # a DIFFERENT event for the same session: layer-2 claim still
        # prevents a second posting
        ev2 = _session_event("evt_2")
        monkeypatch.setitem(sys.modules, "stripe", _fake_stripe(event=ev2))
        res3 = await IP.connect_webhook(_wh_req())
        assert res3 == {"received": True}
        assert len(drafts) == 1
        assert len(fdb.invoices.docs[0]["payments"]) == 1
    asyncio.run(_run())


def test_webhook_ach_pending_then_succeeds(monkeypatch):
    async def _run():
        fdb = _fx(monkeypatch, _fake_stripe(
            event=_session_event("evt_1", payment_status="unpaid")))
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected())
        _seed_invoice(fdb)
        _seed_open_session(fdb)
        drafts = _mock_ledger(monkeypatch)

        res = await IP.connect_webhook(_wh_req())
        assert res == {"received": True}
        assert drafts == []  # funds in flight — no ledger entry yet
        assert fdb.invoices.docs[0]["payment"]["state"] == "pending"

        ev2 = _session_event("evt_2", etype="checkout.session.async_payment_succeeded")
        monkeypatch.setitem(sys.modules, "stripe", _fake_stripe(event=ev2))
        res2 = await IP.connect_webhook(_wh_req())
        assert res2 == {"received": True}
        assert len(drafts) == 1
        assert fdb.invoices.docs[0]["status"] == "paid"
        assert fdb.invoices.docs[0]["payment"]["state"] == "paid"
    asyncio.run(_run())


def test_webhook_async_failed_records_state_only(monkeypatch):
    async def _run():
        ev = _session_event("evt_1", etype="checkout.session.async_payment_failed",
                            payment_status="unpaid")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected())
        _seed_invoice(fdb)
        _seed_open_session(fdb)
        drafts = _mock_ledger(monkeypatch)

        res = await IP.connect_webhook(_wh_req())
        assert res == {"received": True}
        assert drafts == []
        inv = fdb.invoices.docs[0]
        assert inv["payment"]["state"] == "failed"
        assert inv["status"] == "issued" and inv["paid_cents"] == 0
    asyncio.run(_run())


def _paid_setup(monkeypatch):
    fdb = _fx(monkeypatch, _fake_stripe(event=_session_event("evt_1")))
    _set_keys(monkeypatch)
    _seed_tenant(fdb, _connected())
    _seed_invoice(fdb)
    _seed_open_session(fdb)
    drafts = _mock_ledger(monkeypatch)
    return fdb, drafts


def test_webhook_refund_records_state_only(monkeypatch):
    async def _run():
        fdb, drafts = _paid_setup(monkeypatch)
        await IP.connect_webhook(_wh_req())
        assert fdb.invoices.docs[0]["status"] == "paid"
        assert len(drafts) == 1

        ev = {"id": "evt_9", "type": "charge.refunded", "account": ACCT,
              "data": {"object": {"id": "ch_1", "payment_intent": "pi_test_1",
                                  "amount_refunded": 185000}}}
        monkeypatch.setitem(sys.modules, "stripe", _fake_stripe(event=ev))
        res = await IP.connect_webhook(_wh_req())
        assert res == {"received": True}
        inv = fdb.invoices.docs[0]
        assert inv["payment"]["state"] == "refunded"
        assert inv["payment"]["refunded_cents"] == 185000
        # state only: no refund journal, invoice totals untouched — the
        # member reflects it via /invoices/{id}/refund
        assert len(drafts) == 1
        assert inv["status"] == "paid" and inv["paid_cents"] == 185000
    asyncio.run(_run())


def test_webhook_dispute_records_state_only(monkeypatch):
    async def _run():
        fdb, drafts = _paid_setup(monkeypatch)
        await IP.connect_webhook(_wh_req())
        ev = {"id": "evt_10", "type": "charge.dispute.created", "account": ACCT,
              "data": {"object": {"id": "dp_1", "payment_intent": "pi_test_1",
                                  "amount": 185000, "charge": "ch_1"}}}
        monkeypatch.setitem(sys.modules, "stripe", _fake_stripe(event=ev))
        res = await IP.connect_webhook(_wh_req())
        assert res == {"received": True}
        inv = fdb.invoices.docs[0]
        assert inv["payment"]["state"] == "disputed"
        assert len(drafts) == 1  # no money logic of ours
    asyncio.run(_run())


def test_webhook_account_updated_syncs_tenant(monkeypatch):
    async def _run():
        ev = {"id": "evt_11", "type": "account.updated", "account": ACCT,
              "data": {"object": {"id": ACCT, "charges_enabled": True,
                                  "payouts_enabled": True,
                                  "details_submitted": True}}}
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected(charges=False))
        res = await IP.connect_webhook(_wh_req())
        assert res == {"received": True}
        conn = fdb.tenants.docs[0]["stripe_connect"]
        assert conn["charges_enabled"] is True
        assert conn["onboarded_at"] == "2026-10-20T00:00:00"  # preserved
    asyncio.run(_run())


def test_webhook_ignores_other_flows_sessions(monkeypatch):
    async def _run():
        ev = _session_event("evt_12", kind="portal_invoice")
        fdb = _fx(monkeypatch, _fake_stripe(event=ev))
        _set_keys(monkeypatch)
        _seed_tenant(fdb, _connected())
        _seed_invoice(fdb)
        drafts = _mock_ledger(monkeypatch)
        res = await IP.connect_webhook(_wh_req())
        assert res == {"received": True}
        assert drafts == []
        assert fdb.invoices.docs[0].get("payment") is None
    asyncio.run(_run())
