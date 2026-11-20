"""InvoiceEase fold-in test suite (2026-11-14).

Direct-call tests: no mongod, no network, no live Stripe/Resend. Every
endpoint function is awaited directly with a fake user dict and an
in-memory FakeDB monkeypatched into the `db` attribute of each module
under test. Stripe is faked at the SDK surface; Resend is faked at
lib.resend_mailer.send_email (plus two tests against the real mailer's
graceful no-op / capture-file behavior with the key unset).
"""
import asyncio
import copy
import io
import json
import os
import sys
import uuid
import zipfile
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
import stripe
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from lib.base import Scoped, today_str
from lib import invoice_pay as IP
from lib import resend_mailer
from lib import audit as audit_lib
from routers import invoice_delivery as ID
from routers import invoices as INV
from routers import portal as POR
from routers import collections as COL
from routers import recurring_invoices as REC
from routers import copilot as COP

TID = "tenant-test"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}
REAL_SEND_EMAIL = resend_mailer.send_email

ACCOUNT_SEED = [  # code, name, type, normal_balance
    ("1010", "Cash — Register", "asset", "debit"),
    ("1020", "Cash — Bank Checking", "asset", "debit"),
    ("1050", "Stripe Clearing", "asset", "debit"),
    ("1200", "Accounts Receivable", "asset", "debit"),
    ("2100", "Sales Tax Payable", "liability", "credit"),
    ("2210", "Customer Credits", "liability", "credit"),
    ("4020", "Services Revenue", "revenue", "credit"),
    ("4090", "Late Fee Income", "revenue", "credit"),
]


def run(coro):
    return asyncio.run(coro)


# ============================ FakeDB ============================
_MISSING = object()


def _matches(doc, query):
    for key, cond in (query or {}).items():
        val = doc.get(key, _MISSING)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if op == "$in":
                    if val is _MISSING or val not in operand:
                        return False
                elif op == "$ne":
                    if val is not _MISSING and val == operand:
                        return False
                elif op == "$lte":
                    if val is _MISSING or not val <= operand:
                        return False
                elif op == "$gte":
                    if val is _MISSING or not val >= operand:
                        return False
                elif op == "$lt":
                    if val is _MISSING or not val < operand:
                        return False
                elif op == "$gt":
                    if val is _MISSING or not val > operand:
                        return False
                elif op == "$exists":
                    if (val is not _MISSING) != bool(operand):
                        return False
                else:
                    raise NotImplementedError(f"FakeDB: operator {op}")
        else:
            if val is _MISSING or val != cond:
                return False
    return True


def _apply_update(doc, update):
    for op, fields in (update or {}).items():
        if op == "$set":
            for k, v in fields.items():
                doc[k] = copy.deepcopy(v)
        elif op == "$push":
            for k, v in fields.items():
                doc.setdefault(k, []).append(copy.deepcopy(v))
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = (doc.get(k) or 0) + v
        elif op == "$unset":
            for k in fields:
                doc.pop(k, None)
        elif not op.startswith("$"):
            doc[op] = copy.deepcopy(fields)
        else:
            raise NotImplementedError(f"FakeDB: update operator {op}")


def _project(doc, projection):
    if not projection:
        return copy.deepcopy(doc)
    inc = [k for k, v in projection.items() if v]
    exc = [k for k, v in projection.items() if not v]
    if inc:
        out = {k: copy.deepcopy(doc[k]) for k in inc if k in doc}
        if "_id" in doc and "_id" not in exc:
            out["_id"] = doc["_id"]
        return out
    out = copy.deepcopy(doc)
    for k in exc:
        out.pop(k, None)
    return out


def _sort_key(v):
    return (v is None, v if isinstance(v, (str, int, float)) else str(v))


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        if isinstance(spec, dict):
            items = list(spec.items())
        elif isinstance(spec, tuple):
            items = [spec]
        else:
            items = list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: _sort_key(d.get(field)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    # Merge-train completion (2026-11-14): main's multi-currency ledger
    # scans entries with `async for` over find() — support iteration.
    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield d
        return _gen()

    async def to_list(self, n=None):
        return list(self._docs) if n is None else self._docs[:n]

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)


class FakeCollection:
    def __init__(self, name):
        self._name = name
        self.docs = []

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return _project(d, projection)
        return None

    def find(self, query=None, projection=None):
        return FakeCursor([_project(d, projection) for d in self.docs
                           if _matches(d, query)])

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        if self._name == "stripe_events" and doc.get("id") is not None and \
                any(d.get("id") == doc["id"] for d in self.docs):
            raise DuplicateKeyError(f"duplicate stripe event {doc['id']}")
        doc.setdefault("_id", uuid.uuid4().hex)
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def insert_many(self, docs):
        ids = []
        for d in docs:
            r = await self.insert_one(d)
            ids.append(r.inserted_id)
        return SimpleNamespace(inserted_ids=ids)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1,
                                       upserted_id=None)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return SimpleNamespace(matched_count=0, modified_count=0,
                                   upserted_id=new["_id"])
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)

    async def update_many(self, query, update):
        n = 0
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                n += 1
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def find_one_and_update(self, query, update, upsert=False,
                                  return_document=False, projection=None):
        for d in self.docs:
            if _matches(d, query):
                before = _project(d, projection)
                _apply_update(d, update)
                return _project(d, projection) if return_document else before
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return _project(new, projection) if return_document else None
        return None

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def aggregate(self, pipeline):
        docs = [copy.deepcopy(d) for d in self.docs]
        for stage in pipeline or []:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                id_expr = spec.get("_id")
                groups = {}
                for d in docs:
                    key = d.get(id_expr[1:]) if isinstance(id_expr, str) \
                        and id_expr.startswith("$") else None
                    g = groups.setdefault(key, {"_id": key})
                    for out_f, agg in spec.items():
                        if out_f == "_id":
                            continue
                        if isinstance(agg, dict) and "$sum" in agg:
                            src = agg["$sum"]
                            if isinstance(src, str) and src.startswith("$"):
                                g[out_f] = g.get(out_f, 0) + (d.get(src[1:]) or 0)
                            else:
                                g[out_f] = g.get(out_f, 0) + (src or 0)
                docs = list(groups.values())
            elif "$sort" in stage:
                cur = FakeCursor(docs).sort(stage["$sort"])
                docs = cur._docs
            elif "$limit" in stage:
                docs = docs[:stage["$limit"]]
        return FakeCursor(docs)


class FakeDB:
    def __init__(self):
        object.__setattr__(self, "_colls", {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        colls = object.__getattribute__(self, "_colls")
        if name not in colls:
            colls[name] = FakeCollection(name)
        return colls[name]

    def __getitem__(self, name):
        return getattr(self, name)


# ============================ Fakes / fixtures ============================
class MailCapture:
    def __init__(self):
        self.calls = []

    async def send(self, to, subject, html, text="", **kw):
        self.calls.append({"to": to, "subject": subject, "html": html, "text": text})
        return ("sent", None)


class StripeFakes:
    def __init__(self):
        self.sessions = []
        self.intents = []
        self.pi_status = "succeeded"
        self.pi_exc = None
        self._n = 0

    def session_create(self, **kwargs):
        self._n += 1
        self.sessions.append(kwargs)
        return SimpleNamespace(
            id=f"cs_test_{self._n}",
            url=f"https://checkout.stripe.com/pay/cs_test_{self._n}")

    def pi_create(self, **kwargs):
        self.intents.append(kwargs)
        if self.pi_exc:
            raise self.pi_exc
        return {"id": f"pi_{len(self.intents)}", "status": self.pi_status}

    def construct_event(self, payload, sig, secret):
        if sig != "goodsig":
            raise ValueError("signature mismatch")
        return json.loads(payload)


class FakeRequest:
    def __init__(self, payload: bytes, sig="goodsig"):
        self._payload = payload
        self.headers = {"stripe-signature": sig}

    async def body(self):
        return self._payload


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    for mod in (ID, INV, POR, COL, REC, COP, IP, audit_lib):
        monkeypatch.setattr(mod, "db", fake)
    mailer = MailCapture()
    monkeypatch.setattr(resend_mailer, "send_email", mailer.send)
    sf = StripeFakes()
    monkeypatch.setattr(stripe.checkout, "Session",
                        SimpleNamespace(create=sf.session_create))
    monkeypatch.setattr(stripe, "PaymentIntent",
                        SimpleNamespace(create=sf.pi_create))
    monkeypatch.setattr(stripe, "Webhook",
                        SimpleNamespace(construct_event=sf.construct_event))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_unit")
    monkeypatch.setenv("STRIPE_INVOICE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_CAPTURE_FILE", raising=False)
    monkeypatch.delenv("SMS_PROVIDER_URL", raising=False)
    monkeypatch.delenv("SMS_PROVIDER_KEY", raising=False)
    run(fake.tenants.insert_one(
        {"id": TID, "name": "Test Biz", "settings": {"currency": "USD"}}))
    for code, name, typ, nb in ACCOUNT_SEED:
        run(Scoped(fake.accounts, TID).insert_one(
            {"id": f"acct-{code}", "code": code, "name": name, "type": typ,
             "normal_balance": nb, "archived": False}))
    return SimpleNamespace(db=fake, mailer=mailer, stripe=sf, user=USER)


# ============================ Helpers ============================
def make_customer(env, cid="cust-1", name="Acme Co",
                  email="acme@example.com", phone="+15551234567", **extra):
    doc = {"id": cid, "name": name, "email": email, "phone": phone, **extra}
    run(Scoped(env.db.customers, TID).insert_one(doc))
    return doc


def make_invoice(env, amount=100.0, client="Acme Co", customer_id=None,
                 email=None, due=None):
    body = INV.InvoiceBody(
        client_name=client, customer_id=customer_id,
        lines=[INV.InvLine(name="Service", amount=amount)], due_date=due,
        # Fixture helper: suites issue several identical invoices on
        # purpose — deliberate duplicates, exempt from the guard
        # (lib/duplicates.py; the guard itself is tested in
        # test_duplicate_guards.py).
        allow_duplicate=True)
    inv = run(INV.issue_invoice(body, env.user))
    if email:
        run(Scoped(env.db.invoices, TID).update_one(
            {"id": inv["id"]}, {"$set": {"client_email": email}}))
        inv["client_email"] = email
    return inv


def stored_invoice(env, invoice_id):
    return run(Scoped(env.db.invoices, TID).find_one({"id": invoice_id}))


def journal_totals(env):
    deb = sum(l.get("debit", 0) for l in env.db.journal_lines.docs
              if l.get("tenant_id") == TID)
    cred = sum(l.get("credit", 0) for l in env.db.journal_lines.docs
               if l.get("tenant_id") == TID)
    return deb, cred


def event_bytes(eid, etype, obj):
    return json.dumps({"id": eid, "type": etype, "created": 1700000000,
                       "data": {"object": obj}}).encode()


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


# ============================ (1) Fee config ============================
def test_fee_config_defaults_zero(env):
    cfg = run(IP.get_pay_config(TID))
    assert cfg["fee_percent"] == 0.0
    assert cfg["fee_fixed_cents"] == 0
    assert cfg["fee_methods"] == ["ach"]
    assert cfg["surcharge_enabled"] is False
    assert cfg["surcharge_percent"] == 0.0
    assert cfg["surcharge_fixed_cents"] == 0
    assert cfg["sms_enabled"] is False
    assert cfg["nudges_paused"] is False


def test_compute_fee_math_and_method_gating(env):
    cfg = {"fee_percent": 2.5, "fee_fixed_cents": 25, "fee_methods": ["ach"]}
    assert IP.compute_fee_cents(cfg, 10000, "ach") == 275
    assert IP.compute_fee_cents(cfg, 10000, "card") == 0  # method gating
    both = {**cfg, "fee_methods": ["ach", "card"]}
    assert IP.compute_fee_cents(both, 10000, "card") == 275
    assert IP.compute_fee_cents(IP.FEE_DEFAULTS, 10000, "ach") == 0
    capped = {"fee_percent": 100.0, "fee_fixed_cents": 500, "fee_methods": ["ach"]}
    assert IP.compute_fee_cents(capped, 100, "ach") == 100  # never > amount


def test_compute_surcharge_card_only(env):
    cfg = {"surcharge_enabled": True, "surcharge_percent": 2.9,
           "surcharge_fixed_cents": 30}
    assert IP.compute_surcharge_cents(cfg, 10000, "card") == 320
    assert IP.compute_surcharge_cents(cfg, 10000, "ach") == 0  # card only
    assert IP.compute_surcharge_cents({**cfg, "surcharge_enabled": False},
                                      10000, "card") == 0
    assert IP.compute_surcharge_cents(IP.FEE_DEFAULTS, 10000, "card") == 0


def test_pay_config_write_read_roundtrip(env):
    # Exercises the fixed Scoped.update_one(upsert=True) path.
    out = run(ID.write_pay_config(
        ID.PayConfigBody(fee_percent=2.5, fee_fixed_cents=25,
                         fee_methods=["ach"]), env.user))
    assert out["fee_percent"] == 2.5 and out["fee_fixed_cents"] == 25
    cfg = run(ID.read_pay_config(env.user))
    assert cfg["fee_percent"] == 2.5 and cfg["fee_methods"] == ["ach"]


# ============================ (2) Send ============================
def test_send_records_send_and_timeline(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    res = run(ID.send_invoice(inv["id"], ID.SendBody(channel="email"), env.user))
    assert res["sends"][0]["status"] == "sent"
    assert res["sends"][0]["to"] == "acme@example.com"
    assert len(env.mailer.calls) == 1
    assert env.mailer.calls[0]["to"] == "acme@example.com"
    stored = stored_invoice(env, inv["id"])
    assert len(stored.get("sends", [])) == 1
    assert any(e["event"] == "sent" for e in stored.get("timeline", []))
    rows = [d for d in env.db.invoice_sends.docs if d.get("invoice_id") == inv["id"]]
    assert len(rows) == 1 and rows[0]["status"] == "sent"
    tl = run(ID.invoice_timeline(inv["id"], env.user))
    assert any(e["event"] == "sent" for e in tl["events"])
    assert tl["events"][0]["event"] == "issued"


def test_send_skipped_when_no_client_email(env):
    inv = make_invoice(env)  # no customer record, no client_email
    res = run(ID.send_invoice(inv["id"], ID.SendBody(channel="email"), env.user))
    assert res["sends"][0]["status"] == "skipped"
    assert "No client email" in (res["sends"][0]["error"] or "")
    assert env.mailer.calls == []
    stored = stored_invoice(env, inv["id"])
    assert stored["sends"][0]["status"] == "skipped"  # skip is still recorded


def test_send_skipped_when_resend_unconfigured(env, monkeypatch):
    monkeypatch.setattr(resend_mailer, "send_email", REAL_SEND_EMAIL)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_CAPTURE_FILE", raising=False)
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    res = run(ID.send_invoice(inv["id"], ID.SendBody(channel="email"), env.user))
    assert res["sends"][0]["status"] == "skipped"
    assert res["sends"][0]["error"] == "Resend not configured"


def test_resend_capture_file_records_attempt(env, monkeypatch, tmp_path):
    monkeypatch.setattr(resend_mailer, "send_email", REAL_SEND_EMAIL)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    cap = tmp_path / "capture.jsonl"
    monkeypatch.setenv("RESEND_CAPTURE_FILE", str(cap))
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    res = run(ID.send_invoice(inv["id"], ID.SendBody(channel="email"), env.user))
    # No key: the mailer stays a graceful no-op, but capture logs the attempt.
    assert res["sends"][0]["status"] == "skipped"
    lines = cap.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["to"] == "acme@example.com" and "Invoice" in rec["subject"]


# ============================ (3) Pay link ============================
def test_pay_link_card_no_fee_no_surcharge_by_default(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    out = run(ID.pay_link(inv["id"], ID.PayLinkBody(
        method="card", origin_url="https://app.example.com"), env.user))
    assert out["checkout_url"].startswith("https://checkout.stripe.com/")
    assert out["amount_cents"] == 10000
    assert out["platform_fee_cents"] == 0
    assert out["surcharge_cents"] == 0
    assert out["charge_cents"] == 10000
    txn = run(env.db.payment_transactions.find_one({"session_id": out["session_id"]}))
    assert txn["kind"] == "invoice_delivery"
    assert txn["invoice_id"] == inv["id"]
    assert txn["amount_cents"] == 10000 and txn["platform_fee_cents"] == 0
    assert txn["status"] == "initiated" and txn["fulfilled"] is False


def test_pay_link_ach_fee_from_config(env):
    run(IP.set_pay_config(TID, {"fee_percent": 2.5, "fee_fixed_cents": 25,
                                "fee_methods": ["ach"]}))
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    out = run(ID.pay_link(inv["id"], ID.PayLinkBody(
        method="ach", origin_url="https://app.example.com"), env.user))
    assert out["platform_fee_cents"] == 275
    assert out["charge_cents"] == 10000  # fee is ours, not added to payer
    kwargs = env.stripe.sessions[-1]
    assert kwargs["metadata"]["platform_fee_cents"] == "275"
    assert kwargs["payment_method_types"] == ["card", "us_bank_account"]
    txn = run(env.db.payment_transactions.find_one({"session_id": out["session_id"]}))
    assert txn["platform_fee_cents"] == 275 and txn["method"] == "ach"


def test_pay_link_surcharge_card_only(env):
    run(IP.set_pay_config(TID, {"surcharge_enabled": True,
                                "surcharge_percent": 2.9, "surcharge_fixed_cents": 30}))
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    out = run(ID.pay_link(inv["id"], ID.PayLinkBody(
        method="card", origin_url="https://app.example.com"), env.user))
    assert out["surcharge_cents"] == 320
    assert out["charge_cents"] == 10320
    assert env.stripe.sessions[-1]["line_items"][0]["price_data"]["unit_amount"] == 10320
    out2 = run(ID.pay_link(inv["id"], ID.PayLinkBody(
        method="ach", origin_url="https://app.example.com"), env.user))
    assert out2["surcharge_cents"] == 0 and out2["charge_cents"] == 10000


# ============================ (4) Webhook ============================
def _pay_link(env, inv, method="card", amount_cents=None):
    body = ID.PayLinkBody(method=method, origin_url="https://app.example.com",
                          amount_cents=amount_cents)
    return run(ID.pay_link(inv["id"], body, env.user))


def _completed_event(eid, session_id, kind="invoice_delivery", inv_id=None):
    return event_bytes(eid, "checkout.session.completed", {
        "id": session_id, "mode": "payment",
        "metadata": {"kind": kind, "tenant_id": TID, "invoice_id": inv_id or ""}})


def test_webhook_bad_signature_400(env):
    with pytest.raises(HTTPException) as ei:
        run(ID.invoice_stripe_webhook(FakeRequest(b"{}", sig="badsig")))
    assert ei.value.status_code == 400


def test_webhook_unconfigured_secret_503(env, monkeypatch):
    monkeypatch.delenv("STRIPE_INVOICE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    with pytest.raises(HTTPException) as ei:
        run(ID.invoice_stripe_webhook(FakeRequest(b"{}")))
    assert ei.value.status_code == 503


def test_webhook_checkout_completed_records_payment(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    out = _pay_link(env, inv)
    payload = _completed_event("evt_1", out["session_id"], inv_id=inv["id"])
    res = run(ID.invoice_stripe_webhook(FakeRequest(payload)))
    assert res == {"status": "ok"}
    stored = stored_invoice(env, inv["id"])
    assert stored["status"] == "paid" and stored["paid_cents"] == 10000
    assert stored["payments"][0]["processor"] == "stripe"
    assert stored["payments"][0]["method"] == "card"
    assert any(e["event"] == "paid" for e in stored.get("timeline", []))
    txn = run(env.db.payment_transactions.find_one({"session_id": out["session_id"]}))
    assert txn["fulfilled"] is True and txn["payment_status"] == "paid"
    lines = [l for l in env.db.journal_lines.docs if l.get("tenant_id") == TID]
    assert any(l["account_code"] == "1050" and l["debit"] == 10000 for l in lines)
    assert any(l["account_code"] == "1200" and l["credit"] == 10000 for l in lines)


def test_webhook_duplicate_event_returns_duplicate(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    out = _pay_link(env, inv)
    payload = _completed_event("evt_dup", out["session_id"], inv_id=inv["id"])
    assert run(ID.invoice_stripe_webhook(FakeRequest(payload))) == {"status": "ok"}
    n_entries = len(env.db.journal_entries.docs)
    res = run(ID.invoice_stripe_webhook(FakeRequest(payload)))
    assert res == {"status": "duplicate"}
    stored = stored_invoice(env, inv["id"])
    assert len(stored["payments"]) == 1  # exactly once
    assert len(env.db.journal_entries.docs) == n_entries


def test_webhook_partial_payment_marks_partial(env):
    make_customer(env)
    inv = make_invoice(env, amount=200.0, customer_id="cust-1")
    out = _pay_link(env, inv, amount_cents=5000)
    payload = _completed_event("evt_part", out["session_id"], inv_id=inv["id"])
    run(ID.invoice_stripe_webhook(FakeRequest(payload)))
    stored = stored_invoice(env, inv["id"])
    assert stored["status"] == "partial" and stored["paid_cents"] == 5000


def test_webhook_ach_async_succeeded_records_payment(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    out = _pay_link(env, inv, method="ach")
    payload = event_bytes("evt_ach", "checkout.session.async_payment_succeeded", {
        "id": out["session_id"], "mode": "payment",
        "metadata": {"kind": "invoice_delivery", "tenant_id": TID,
                     "invoice_id": inv["id"]}})
    run(ID.invoice_stripe_webhook(FakeRequest(payload)))
    stored = stored_invoice(env, inv["id"])
    assert stored["status"] == "paid"
    lines = [l for l in env.db.journal_lines.docs if l.get("tenant_id") == TID]
    assert any(l["account_code"] == "1020" and l["debit"] == 10000 for l in lines)


def test_webhook_portal_invoice_kind_routes_to_portal(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    run(env.db.payment_transactions.insert_one({
        "session_id": "cs_portal_1", "tenant_id": TID, "kind": "portal_invoice",
        "invoice_id": inv["id"], "token_hash": "deadbeef",
        "amount_cents": 10000, "currency": "usd", "status": "initiated",
        "payment_status": "pending", "fulfilled": False}))
    payload = _completed_event("evt_portal", "cs_portal_1",
                               kind="portal_invoice", inv_id=inv["id"])
    res = run(ID.invoice_stripe_webhook(FakeRequest(payload)))
    assert res == {"status": "ok"}
    stored = stored_invoice(env, inv["id"])
    assert stored["status"] == "paid"
    assert stored["payments"][0]["source"] == "stripe_portal"
    txn = run(env.db.payment_transactions.find_one({"session_id": "cs_portal_1"}))
    assert txn["fulfilled"] is True and txn["payment_status"] == "paid"


# ============================ (5) Client page ============================
def _token_for(env, cid="cust-1", name="Acme Co"):
    return run(IP.ensure_client_token(TID, cid, name))


def test_client_page_lists_open_and_history(env):
    make_customer(env)
    open_inv = make_invoice(env, amount=100.0, customer_id="cust-1")
    paid_inv = make_invoice(env, amount=40.0, customer_id="cust-1")
    run(INV.pay_invoice(paid_inv["id"], INV.PayBody(payment_method="bank"), env.user))
    token = _token_for(env)
    page = run(ID.client_page(token))
    assert page["business"] == "Test Biz"
    assert [i["id"] for i in page["open"]] == [open_inv["id"]]
    assert [i["id"] for i in page["history"]] == [paid_inv["id"]]
    assert page["total_open_cents"] == 10000
    assert page["autopay"]["enrolled"] is False


def test_client_view_records_viewed_and_first_viewed_at(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    token = _token_for(env)
    out = run(ID.client_view_invoice(token, inv["id"]))
    assert out["invoice"]["id"] == inv["id"]
    stored = stored_invoice(env, inv["id"])
    assert stored.get("first_viewed_at")
    assert any(e["event"] == "viewed" and e["detail"].get("via") == "client_page"
               for e in stored.get("timeline", []))


def test_client_pay_rejects_other_customers_invoice(env):
    make_customer(env)
    make_customer(env, cid="cust-2", name="Other Co", email="other@example.com")
    inv = make_invoice(env, customer_id="cust-1")
    token2 = _token_for(env, cid="cust-2", name="Other Co")
    with pytest.raises(HTTPException) as ei:
        run(ID.client_pay(token2, inv["id"], ID.ClientPayBody(
            method="card", origin_url="https://app.example.com")))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei2:
        run(ID.client_view_invoice(token2, inv["id"]))
    assert ei2.value.status_code == 404


def test_client_page_revoked_and_unknown_token_404(env):
    make_customer(env)
    make_invoice(env, customer_id="cust-1")
    token = _token_for(env)
    assert run(ID.client_page(token))["client_name"] == "Acme Co"
    out = run(ID.revoke_client_token(token, env.user))
    assert out == {"ok": True}
    with pytest.raises(HTTPException) as ei:
        run(ID.client_page(token))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei2:
        run(ID.client_page("no-such-token"))
    assert ei2.value.status_code == 404


# ============================ (6) Open pixel ============================
def test_open_pixel_returns_gif_and_marks_viewed(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    token = _token_for(env)
    resp = run(ID.client_open_pixel(token))
    assert resp.media_type == "image/gif"
    assert resp.body[:6] == b"GIF89a"
    stored = stored_invoice(env, inv["id"])
    assert stored.get("first_viewed_at")
    assert any(e["event"] == "viewed" and e["detail"].get("via") == "email_pixel"
               for e in stored.get("timeline", []))
    # Unknown token still serves the pixel (never breaks an email render).
    resp2 = run(ID.client_open_pixel("bogus-token"))
    assert resp2.media_type == "image/gif" and resp2.body[:6] == b"GIF89a"


# ============================ (7) Split ============================
def test_split_creates_children_and_marks_parent(env):
    make_customer(env)
    inv = make_invoice(env, amount=300.0, customer_id="cust-1")
    out = run(ID.split_invoice(inv["id"], ID.SplitBody(parts=[
        ID.SplitPart(label="Deposit", amount=100.0),
        ID.SplitPart(label="Balance", amount=200.0)]), env.user))
    assert out["parent_invoice_id"] == inv["id"]
    assert len(out["parts"]) == 2
    assert sum(p["total_cents"] for p in out["parts"]) == 30000
    parent = stored_invoice(env, inv["id"])
    assert parent["status"] == "split"
    assert parent["split_children"] == [p["invoice_id"] for p in out["parts"]]
    child = stored_invoice(env, out["parts"][0]["invoice_id"])
    assert child["split_parent_id"] == inv["id"]
    assert child["split_label"] == "Deposit"
    assert child["total_cents"] == 10000 and child["issue_entry"]


def test_split_parts_must_sum_to_total(env):
    inv = make_invoice(env, amount=300.0)
    with pytest.raises(HTTPException) as ei:
        run(ID.split_invoice(inv["id"], ID.SplitBody(parts=[
            ID.SplitPart(label="A", amount=100.0),
            ID.SplitPart(label="B", amount=100.0)]), env.user))
    assert ei.value.status_code == 422


def test_split_with_payments_conflict_409(env):
    inv = make_invoice(env, amount=300.0)
    run(INV.pay_invoice(inv["id"], INV.PayBody(
        payment_method="bank", amount=50.0), env.user))
    with pytest.raises(HTTPException) as ei:
        run(ID.split_invoice(inv["id"], ID.SplitBody(parts=[
            ID.SplitPart(label="A", amount=150.0),
            ID.SplitPart(label="B", amount=150.0)]), env.user))
    assert ei.value.status_code == 409


def test_split_twice_conflict_409(env):
    inv = make_invoice(env, amount=300.0)
    parts = [ID.SplitPart(label="A", amount=150.0),
             ID.SplitPart(label="B", amount=150.0)]
    run(ID.split_invoice(inv["id"], ID.SplitBody(parts=parts), env.user))
    with pytest.raises(HTTPException) as ei:
        run(ID.split_invoice(inv["id"], ID.SplitBody(parts=parts), env.user))
    assert ei.value.status_code == 409


# ============================ (8) Autopay ============================
def _enroll(env, cid="cust-1"):
    return run(ID.autopay_enroll(ID.AutopayEnrollBody(
        customer_id=cid, payment_method_id="pm_123",
        pm_brand="visa", pm_last4="4242"), env.user))


def test_autopay_enroll_and_charge_success(env):
    make_customer(env)
    enr = _enroll(env)
    assert enr["active"] is True and enr["pm_last4"] == "4242"
    inv = make_invoice(env, customer_id="cust-1")
    out = run(ID.autopay_charge(inv["id"], env.user))
    assert out["status"] == "paid" and out["paid_cents"] == 10000
    assert env.stripe.intents[-1]["payment_method"] == "pm_123"
    assert env.stripe.intents[-1]["off_session"] is True
    stored = stored_invoice(env, inv["id"])
    assert stored["payments"][0]["source"] == "stripe_autopay"
    assert stored.get("autopay_last_error") is None


def test_autopay_charge_failure_lands_in_collections(env):
    make_customer(env)
    _enroll(env)
    inv = make_invoice(env, customer_id="cust-1")
    env.stripe.pi_exc = Exception("Your card was declined.")
    with pytest.raises(HTTPException) as ei:
        run(ID.autopay_charge(inv["id"], env.user))
    assert ei.value.status_code == 402
    stored = stored_invoice(env, inv["id"])
    assert "declined" in (stored.get("autopay_last_error") or "")
    assert stored.get("autopay_failed_at")
    assert any(e["event"] == "autopay_failed" for e in stored.get("timeline", []))
    rems = [d for d in env.db.payment_reminders.docs
            if d.get("invoice_id") == inv["id"]]
    assert len(rems) == 1 and rems[0]["stage"] == "autopay_failed"
    assert rems[0]["via"] == "autopay"


def test_autopay_charge_non_success_status_fails(env):
    make_customer(env)
    _enroll(env)
    inv = make_invoice(env, customer_id="cust-1")
    env.stripe.pi_status = "requires_payment_method"
    with pytest.raises(HTTPException) as ei:
        run(ID.autopay_charge(inv["id"], env.user))
    assert ei.value.status_code == 402
    rems = [d for d in env.db.payment_reminders.docs
            if d.get("invoice_id") == inv["id"]]
    assert len(rems) == 1 and rems[0]["stage"] == "autopay_failed"


def test_autopay_charge_not_enrolled_409(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    with pytest.raises(HTTPException) as ei:
        run(ID.autopay_charge(inv["id"], env.user))
    assert ei.value.status_code == 409


def test_recurring_template_autopay_generates_and_charges(env):
    make_customer(env)
    _enroll(env)
    tpl = run(REC.create_template(REC.RecurringInvoiceBody(
        name="Monthly retainer", client_name="Acme Co", customer_id="cust-1",
        lines=[INV.InvLine(name="Retainer", amount=50.0)], frequency="monthly",
        next_run=today_str(), autopay=True), env.user))
    assert tpl["autopay"] is True
    out = run(REC.run_due(env.user))
    assert out["generated"] == 1
    gen = stored_invoice(env, out["invoices"][0]["invoice_id"])
    assert gen["status"] == "paid" and gen["paid_cents"] == 5000
    assert gen["payments"][0]["source"] == "stripe_autopay"


# ============================ (9) Collections autopilot ============================
def _enable_autopilot(env):
    return run(COL.write_config(COL.ConfigBody(
        enabled=True, steps=COL.DEFAULT_STEPS, late_fee_days=21,
        late_fee_percent=1.5, late_fee_min_cents=2500,
        late_fee_max_cents=25000, pause_service_days=35), env.user))


def test_collections_autopilot_run_sends_reminder(env):
    _enable_autopilot(env)
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1", due=days_ago(5))
    res = run(COL.autopilot_run(env.user))
    assert res["errors"] == []
    mine = [a for a in res["actions"] if a["invoice_id"] == inv["id"]]
    assert [a["action"] for a in mine] == ["nudge"]
    assert mine[0]["delivery"]["status"] == "sent"
    assert len(env.mailer.calls) == 1
    assert env.mailer.calls[0]["to"] == "acme@example.com"
    rems = [d for d in env.db.payment_reminders.docs
            if d.get("invoice_id") == inv["id"]]
    assert len(rems) == 1 and rems[0]["via"] == "autopilot"
    stored = stored_invoice(env, inv["id"])
    assert any(s["kind"] == "reminder" and s["status"] == "sent"
               for s in stored.get("sends", []))


def test_collections_respects_per_invoice_pause(env):
    _enable_autopilot(env)
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1", due=days_ago(5))
    run(Scoped(env.db.invoices, TID).update_one(
        {"id": inv["id"]}, {"$set": {"nudges_paused": True}}))
    res = run(COL.autopilot_run(env.user))
    mine = [a for a in res["actions"] if a["invoice_id"] == inv["id"]]
    assert mine and mine[0]["delivery"]["status"] == "skipped"
    assert env.mailer.calls == []


def test_collections_respects_tenant_kill_switch(env):
    _enable_autopilot(env)
    run(IP.set_pay_config(TID, {"nudges_paused": True}))
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1", due=days_ago(5))
    res = run(COL.autopilot_run(env.user))
    mine = [a for a in res["actions"] if a["invoice_id"] == inv["id"]]
    assert mine and mine[0]["delivery"]["status"] == "skipped"
    assert "paused" in (mine[0]["delivery"]["error"] or "").lower()
    assert env.mailer.calls == []


def test_collections_late_fee_still_applies(env):
    _enable_autopilot(env)
    make_customer(env)
    inv = make_invoice(env, amount=100.0, customer_id="cust-1", due=days_ago(25))
    res = run(COL.autopilot_run(env.user))
    fees = [a for a in res["actions"]
            if a["invoice_id"] == inv["id"] and a["action"] == "late_fee"]
    assert fees and fees[0]["fee_cents"] == 2500  # $25 minimum fee
    stored = stored_invoice(env, inv["id"])
    assert stored["total_cents"] == 12500
    assert len(stored.get("late_fees", [])) == 1
    lines = [l for l in env.db.journal_lines.docs if l.get("tenant_id") == TID]
    assert any(l["account_code"] == "4090" and l["credit"] == 2500 for l in lines)
    assert any(l["account_code"] == "1200" and l["debit"] == 2500 for l in lines)


# ============================ (10) SMS seam ============================
def test_sms_seam_default_skipped_not_yet_enabled(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    rec = run(IP.send_invoice_sms(TID, inv, "invoice"))
    assert rec["status"] == "skipped"
    assert "not yet enabled" in (rec["error"] or "")
    stored = stored_invoice(env, inv["id"])
    assert any(s["channel"] == "sms" and s["status"] == "skipped"
               for s in stored.get("sends", []))


def test_sms_via_send_endpoint_both_channels(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    res = run(ID.send_invoice(inv["id"], ID.SendBody(channel="both"), env.user))
    by_channel = {r["channel"]: r for r in res["sends"]}
    assert by_channel["email"]["status"] == "sent"
    assert by_channel["sms"]["status"] == "skipped"
    assert "not yet enabled" in (by_channel["sms"]["error"] or "")


def test_sms_enabled_but_provider_unconfigured(env):
    run(IP.set_pay_config(TID, {"sms_enabled": True}))
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    rec = run(IP.send_invoice_sms(TID, inv, "invoice"))
    assert rec["status"] == "skipped"
    assert "provider not configured" in (rec["error"] or "")


# ============================ (11) Dispute packet ============================
def test_dispute_packet_zip_contains_pdf_and_json(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    run(ID.send_invoice(inv["id"], ID.SendBody(channel="email"), env.user))
    run(INV.pay_invoice(inv["id"], INV.PayBody(payment_method="bank"), env.user))
    resp = run(ID.dispute_packet(inv["id"], env.user))
    assert resp.media_type == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(resp.body))
    names = z.namelist()
    assert any(n.endswith(".pdf") for n in names)
    assert any(n.endswith(".json") for n in names)
    pdf_name = next(n for n in names if n.endswith(".pdf"))
    json_name = next(n for n in names if n.endswith(".json"))
    assert z.read(pdf_name)[:5] == b"%PDF-"
    packet = json.loads(z.read(json_name))
    assert packet["invoice"]["id"] == inv["id"]
    assert len(packet["sends"]) == 1


# ============================ (12) Regression ============================
def test_manual_issue_and_pay_still_works_balanced(env):
    inv = make_invoice(env, amount=120.0)
    assert inv["status"] == "issued" and inv["total_cents"] == 12000
    out = run(INV.pay_invoice(inv["id"], INV.PayBody(
        payment_method="bank"), env.user))
    assert out["status"] == "paid" and out["balance_cents"] == 0
    deb, cred = journal_totals(env)
    assert deb == cred and deb > 0
    from lib import ledger as L
    tb = run(L.trial_balance(env.db, TID, today_str()))
    assert tb["balanced"] is True and tb["total_debit"] == tb["total_credit"]


def test_online_payment_journal_balanced(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1")
    out = _pay_link(env, inv)
    payload = _completed_event("evt_bal", out["session_id"], inv_id=inv["id"])
    run(ID.invoice_stripe_webhook(FakeRequest(payload)))
    deb, cred = journal_totals(env)
    assert deb == cred
    lines = [l for l in env.db.journal_lines.docs if l.get("tenant_id") == TID]
    ar_debit = sum(l["debit"] for l in lines if l["account_code"] == "1200")
    ar_credit = sum(l["credit"] for l in lines if l["account_code"] == "1200")
    assert ar_debit == ar_credit == 10000  # AR fully cleared by the payment


# ============================ Extra: copilot chase ============================
def test_copilot_chase_now_sends(env):
    make_customer(env)
    inv = make_invoice(env, customer_id="cust-1", due=days_ago(3))
    result = run(COP._exec_chase_invoice(env.user, {"invoice_id": inv["id"]}))
    assert "Email sent" in result
    assert len(env.mailer.calls) == 1
    rems = [d for d in env.db.reminders.docs if d.get("invoice_id") == inv["id"]]
    assert len(rems) == 1 and rems[0]["type"] == "invoice_chase"
