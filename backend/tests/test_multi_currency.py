"""Multi-currency lane (Top-20 #16, owner-approved bounded shape 2026-11-14).

Direct-call tests: no mongod, no network. Feeds are stubbed through the one
HTTP seam in lib.fx (`_http_get`); endpoint functions are awaited directly
with a fake user dict and an in-memory FakeDB monkeypatched into the `db`
attribute of the modules under test. The real ledger posts every journal
entry, so balance / chart / stamp math is exercised end to end.
"""
import asyncio
import copy
import json
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib import fx as FX
from lib.coa import full_coa
from routers import invoices, bills, fx as fx_router

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}

TODAY = "2026-11-14"


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
                elif op == "$gte":
                    if val is _MISSING or not val >= operand:
                        return False
                elif op == "$lte":
                    if val is _MISSING or not val <= operand:
                        return False
                elif op == "$lt":
                    if val is _MISSING or not val < operand:
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
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = (doc.get(k) or 0) + v
        elif op == "$unset":
            for k in fields:
                doc.pop(k, None)
        elif op == "$push":
            for k, v in fields.items():
                doc.setdefault(k, []).append(copy.deepcopy(v))
        else:
            raise NotImplementedError(f"FakeDB: update operator {op}")


def _project(doc, projection):
    if not projection:
        return copy.deepcopy(doc)
    exc = [k for k, v in projection.items() if not v]
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
        items = [spec] if isinstance(spec, tuple) else list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: _sort_key(d.get(field)),
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
    def __init__(self, name):
        self.name = name
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
        doc.setdefault("_id", uuid.uuid4().hex)
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)
        return SimpleNamespace(inserted_ids=[])

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return SimpleNamespace(matched_count=0, modified_count=0)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, query, update):
        n = 0
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                n += 1
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def find_one_and_update(self, query, update, upsert=False,
                                  return_document=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return copy.deepcopy(d)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return copy.deepcopy(new)
        return None

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


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


# ============================ fixtures ============================
def _seed_accounts(fake, tid):
    for code, name, typ, normal in full_coa("barbershop"):
        fake.accounts.docs.append(
            {"tenant_id": tid, "code": code, "name": name, "type": typ,
             "normal_balance": normal, "archived": False})


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    fake.tenants.docs.append({"id": TID, "settings": {"currency": "USD"}})
    fake.tenants.docs.append({"id": TID2, "settings": {"currency": "USD"}})
    _seed_accounts(fake, TID)
    _seed_accounts(fake, TID2)
    monkeypatch.setattr(invoices, "db", fake)
    monkeypatch.setattr(bills, "db", fake)
    monkeypatch.setattr(fx_router, "db", fake)
    return fake


def frank(usd_per_eur="1.10", date=TODAY):
    """Frankfurter answer for base=EUR&symbols=USD (USD per 1 EUR)."""
    return json.dumps({"amount": 1, "base": "EUR", "date": date,
                       "rates": {"USD": float(usd_per_eur)}})


ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
 <Cube><Cube time="2026-11-14">
  <Cube currency="USD" rate="1.08"/><Cube currency="GBP" rate="0.85"/>
 </Cube></Cube>
</gesmes:Envelope>"""


def feed(monkeypatch, frank_text=None, ecb_text=None):
    def fake_get(url):
        if "frankfurter" in url:
            if frank_text is None:
                raise OSError("frankfurter down")
            return frank_text
        if "ecb.europa" in url:
            if ecb_text is None:
                raise OSError("ecb down")
            return ecb_text
        raise AssertionError(f"unexpected URL {url}")
    monkeypatch.setattr(FX, "_http_get", fake_get)


def lines_for(env, entry_number):
    return [l for l in env.journal_lines.docs if True]  # filtered by caller


def entry_lines(env, source_ref):
    entries = [e for e in env.journal_entries.docs
               if e.get("source_ref") == source_ref]
    out = []
    for e in entries:
        out += [l for l in env.journal_lines.docs
                if l.get("entry_id") == e["id"]]
    return entries, out


def by_code(lines):
    return {l["account_code"]: (l.get("debit", 0), l.get("credit", 0))
            for l in lines}


# ============================ feed parsing / refresh ============================
def test_frankfurter_refresh_stores_home_per_txn_rate(env, monkeypatch):
    feed(monkeypatch, frank_text=frank("1.10"))
    doc = run(FX.refresh_pair(env, TID, "USD", "EUR"))
    assert doc["rate"] == "1.1" and doc["date"] == TODAY
    assert doc["source"] == FX.SOURCE_FRANKFURTER
    stored = run(FX.get_stored_rate(env, TID, "USD", "EUR"))
    assert stored["rate"] == "1.1"


def test_ecb_fallback_cross_rate(env, monkeypatch):
    feed(monkeypatch, frank_text=None, ecb_text=ECB_XML)
    doc = run(FX.refresh_pair(env, TID, "USD", "EUR"))
    # ECB quotes per EUR: USD 1.08 -> USD per 1 EUR = 1.08 / 1 = 1.08.
    assert doc["rate"] == "1.08" and doc["source"] == FX.SOURCE_ECB
    doc2 = run(FX.refresh_pair(env, TID, "USD", "GBP"))
    # USD per 1 GBP = 1.08 / 0.85 = 1.27058824 (8dp HALF_UP).
    assert doc2["rate"] == "1.27058824"


def test_total_failure_keeps_stored_rate(env, monkeypatch):
    feed(monkeypatch, frank_text=frank("1.10"))
    run(FX.refresh_pair(env, TID, "USD", "EUR"))
    feed(monkeypatch)  # both feeds down now
    assert run(FX.refresh_pair(env, TID, "USD", "EUR")) is None
    stored = run(FX.get_stored_rate(env, TID, "USD", "EUR"))
    assert stored["rate"] == "1.1"  # untouched


# ============================ stamps ============================
def test_home_stamp_is_rate_one(env):
    s = run(FX.resolve_stamp(env, TID, "USD", "USD", on_date=TODAY))
    assert s == {"txn_currency": "USD", "fx_rate": "1", "fx_rate_date": TODAY,
                 "fx_source": FX.SOURCE_HOME, "fx_stale": False}


def test_manual_rate_stamp(env):
    s = run(FX.resolve_stamp(env, TID, "USD", "EUR", on_date=TODAY,
                             manual_rate=1.25))
    assert s["fx_rate"] == "1.25" and s["fx_source"] == FX.SOURCE_MANUAL
    assert s["fx_stale"] is False


def test_stale_rate_is_honestly_labeled(env, monkeypatch):
    run(FX.store_rate(env, TID, "USD", "EUR", "1.05", "2026-11-01",
                      FX.SOURCE_FRANKFURTER))
    feed(monkeypatch)  # feeds down: refresh cannot help
    s = run(FX.resolve_stamp(env, TID, "USD", "EUR", on_date=TODAY))
    assert s["fx_rate"] == "1.05" and s["fx_rate_date"] == "2026-11-01"
    assert s["fx_stale"] is True


def test_no_rate_anywhere_is_422_not_invented(env, monkeypatch):
    feed(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        run(FX.resolve_stamp(env, TID, "USD", "EUR", on_date=TODAY))
    assert ei.value.status_code == 422


def test_rates_are_tenant_isolated(env, monkeypatch):
    feed(monkeypatch, frank_text=frank("1.10"))
    run(FX.refresh_pair(env, TID, "USD", "EUR"))
    assert run(FX.get_stored_rate(env, TID2, "USD", "EUR")) is None
    feed(monkeypatch)
    with pytest.raises(HTTPException):
        run(FX.resolve_stamp(env, TID2, "USD", "EUR", on_date=TODAY))


# ============================ invoices ============================
def eur_invoice(env, amount=100.0, tax=10.0, rate="1.10"):
    run(FX.store_rate(env, TID, "USD", "EUR", rate, TODAY, FX.SOURCE_MANUAL))
    body = invoices.InvoiceBody(
        client_name="Berlin GmbH", currency="EUR", tax=tax,
        lines=[invoices.InvLine(name="Job", amount=amount)])
    return run(invoices.issue_invoice(body, USER))


def test_foreign_invoice_converts_and_stamps(env):
    inv = eur_invoice(env)
    assert inv["fx_rate"] == "1.1" and inv["fx_source"] == FX.SOURCE_MANUAL
    assert inv["txn_currency"] == "EUR" and inv["is_foreign"] is True
    assert inv["txn_total_cents"] == 11000          # document currency
    assert inv["total_cents"] == 12100              # home: 110.00 EUR x 1.10
    entries, lines = entry_lines(env, inv["id"])
    assert len(entries) == 1
    m = by_code(lines)
    assert m["1200"] == (12100, 0)                  # Dr AR (home)
    assert m["4020"] == (0, 11000)                  # Cr revenue (home)
    assert m["2100"] == (0, 1100)                   # Cr sales tax payable


def test_home_invoice_is_byte_for_byte_legacy(env):
    body = invoices.InvoiceBody(
        client_name="Local LLC", tax=10.0,
        lines=[invoices.InvLine(name="Job", amount=100.0)])
    inv = run(invoices.issue_invoice(body, USER))
    assert inv["fx_rate"] == "1" and inv["fx_source"] == FX.SOURCE_HOME
    assert inv["txn_currency"] == "USD" and inv["is_foreign"] is False
    assert inv["total_cents"] == 11000 and inv["txn_total_cents"] == 11000
    entries, lines = entry_lines(env, inv["id"])
    m = by_code(lines)
    assert m["1200"] == (11000, 0) and m["4020"] == (0, 10000)
    assert m["2100"] == (0, 1000)
    res = run(invoices.pay_invoice(
        inv["id"], invoices.PayBody(payment_method="bank"), USER))
    assert res["fx_gain_loss_cents"] == 0 and res["status"] == "paid"
    entries, lines = entry_lines(env, inv["id"])
    pay_lines = [l for e in entries if e["source"] == "invoice_paid"
                 for l in env.journal_lines.docs if l["entry_id"] == e["id"]]
    m = by_code(pay_lines)
    assert m == {"1020": (11000, 0), "1200": (0, 11000)}


def test_stamp_immutability(env):
    inv = eur_invoice(env)
    # A newer rate lands AFTER the invoice was stamped.
    run(FX.store_rate(env, TID, "USD", "EUR", "9.99", TODAY, FX.SOURCE_MANUAL))
    stored = run(invoices.coll(USER).find_one({"id": inv["id"]}))
    assert stored["fx_rate"] == "1.1" and stored["total_cents"] == 12100
    entries, lines = entry_lines(env, inv["id"])
    assert by_code(lines)["1200"] == (12100, 0)     # JE never moved


def test_invoice_payment_gain(env):
    inv = eur_invoice(env)
    # Pay the full EUR 110 on a day when EUR buys 1.20 USD.
    run(FX.store_rate(env, TID, "USD", "EUR", "1.20", TODAY, FX.SOURCE_MANUAL))
    res = run(invoices.pay_invoice(
        inv["id"], invoices.PayBody(payment_method="bank",
                                    pay_date=TODAY), USER))
    assert res["status"] == "paid"
    assert res["fx_gain_loss_cents"] == 1100        # 13200 - 12100
    assert res["fx_rate_invoice"] == "1.1" and res["fx_rate_payment"] == "1.2"
    entries = [e for e in env.journal_entries.docs
               if e.get("source_ref") == inv["id"] and e["source"] == "invoice_paid"]
    lines = [l for l in env.journal_lines.docs
             if l["entry_id"] == entries[0]["id"]]
    m = by_code(lines)
    assert m["1020"] == (13200, 0)                  # cash at payment rate
    assert m["1200"] == (0, 12100)                  # AR clears at stamp rate
    assert m["4950"] == (0, 1100)                   # realized FX gain
    assert sum(d for d, _ in m.values()) == sum(c for _, c in m.values())
    doc = run(invoices.coll(USER).find_one({"id": inv["id"]}))
    pmt = doc["payments"][-1]
    assert pmt["fx_gain_loss_cents"] == 1100
    assert pmt["fx_rate_invoice"] == "1.1" and pmt["fx_rate_payment"] == "1.2"


def test_invoice_payment_loss(env):
    inv = eur_invoice(env)
    run(FX.store_rate(env, TID, "USD", "EUR", "1.00", TODAY, FX.SOURCE_MANUAL))
    res = run(invoices.pay_invoice(
        inv["id"], invoices.PayBody(payment_method="bank",
                                    pay_date=TODAY), USER))
    assert res["fx_gain_loss_cents"] == -1100       # 11000 - 12100
    entries = [e for e in env.journal_entries.docs
               if e.get("source_ref") == inv["id"] and e["source"] == "invoice_paid"]
    lines = [l for l in env.journal_lines.docs
             if l["entry_id"] == entries[0]["id"]]
    m = by_code(lines)
    assert m["1020"] == (11000, 0)
    assert m["6990"] == (1100, 0)                   # realized FX loss (debit)
    assert m["1200"] == (0, 12100)
    assert sum(d for d, _ in m.values()) == sum(c for _, c in m.values())


def test_invoice_partial_payment_clears_proportionally(env):
    inv = eur_invoice(env)
    run(FX.store_rate(env, TID, "USD", "EUR", "1.20", TODAY, FX.SOURCE_MANUAL))
    res = run(invoices.pay_invoice(
        inv["id"], invoices.PayBody(amount=55.0, payment_method="bank",
                                    pay_date=TODAY), USER))
    assert res["status"] == "partial"
    # Half the invoice: AR clears 55 x 1.10 = 6050; cash 55 x 1.20 = 6600.
    assert res["fx_gain_loss_cents"] == 550
    doc = run(invoices.coll(USER).find_one({"id": inv["id"]}))
    assert doc["txn_paid_cents"] == 5500 and doc["paid_cents"] == 6050


def test_foreign_invoice_rejects_credit_application(env):
    inv = eur_invoice(env)
    env.customer_credits.docs.append(
        {"id": "cred-1", "tenant_id": TID, "client_name": "Berlin GmbH",
         "remaining_cents": 5000})
    with pytest.raises(HTTPException) as ei:
        run(invoices.pay_invoice(
            inv["id"], invoices.PayBody(credit_id="cred-1"), USER))
    assert ei.value.status_code == 422


# ============================ bills ============================
def eur_bill(env, amount=200.0, rate="1.10"):
    run(FX.store_rate(env, TID, "USD", "EUR", rate, TODAY, FX.SOURCE_MANUAL))
    body = bills.BillBody(vendor="Munich Supply", amount=amount,
                          expense_code="5700", currency="EUR",
                          bill_date=TODAY)
    return run(bills.create_bill(body, USER))


def test_foreign_bill_converts_and_stamps(env):
    b = eur_bill(env)
    assert b["fx_rate"] == "1.1" and b["txn_currency"] == "EUR"
    assert b["txn_amount_cents"] == 20000
    assert b["amount_cents"] == 22000               # home at the stamp
    entries, lines = entry_lines(env, b["id"])
    m = by_code(lines)
    assert m["5700"] == (22000, 0) and m["2010"] == (0, 22000)


def test_bill_payment_gain_when_rate_falls(env):
    b = eur_bill(env)
    # EUR weakens to 1.00: paying the EUR 200 bill costs $200, booked $220.
    run(FX.store_rate(env, TID, "USD", "EUR", "1.00", TODAY, FX.SOURCE_MANUAL))
    res = run(bills.pay_bill(
        b["id"], bills.PayBody(payment_method="bank", pay_date=TODAY), USER))
    assert res["fx_gain_loss_cents"] == 2000
    entries = [e for e in env.journal_entries.docs
               if e.get("source_ref") == b["id"] and e["source"] == "ap_payment"]
    lines = [l for l in env.journal_lines.docs
             if l["entry_id"] == entries[0]["id"]]
    m = by_code(lines)
    assert m["2010"] == (22000, 0) and m["1020"] == (0, 20000)
    assert m["4950"] == (0, 2000)
    assert sum(d for d, _ in m.values()) == sum(c for _, c in m.values())


def test_bill_payment_loss_when_rate_rises(env):
    b = eur_bill(env)
    run(FX.store_rate(env, TID, "USD", "EUR", "1.30", TODAY, FX.SOURCE_MANUAL))
    res = run(bills.pay_bill(
        b["id"], bills.PayBody(payment_method="bank", pay_date=TODAY), USER))
    assert res["fx_gain_loss_cents"] == -4000       # 22000 booked, 26000 paid
    entries = [e for e in env.journal_entries.docs
               if e.get("source_ref") == b["id"] and e["source"] == "ap_payment"]
    lines = [l for l in env.journal_lines.docs
             if l["entry_id"] == entries[0]["id"]]
    m = by_code(lines)
    assert m["2010"] == (22000, 0) and m["6990"] == (4000, 0)
    assert m["1020"] == (0, 26000)
    assert sum(d for d, _ in m.values()) == sum(c for _, c in m.values())


def test_home_bill_unchanged(env):
    body = bills.BillBody(vendor="Local Supply", amount=75.0,
                          expense_code="5700", bill_date=TODAY)
    b = run(bills.create_bill(body, USER))
    assert b["fx_rate"] == "1" and b["amount_cents"] == 7500
    assert b["txn_amount_cents"] == 7500
    res = run(bills.pay_bill(
        b["id"], bills.PayBody(payment_method="bank", pay_date=TODAY), USER))
    assert res["fx_gain_loss_cents"] == 0 and res["status"] == "paid"
    entries = [e for e in env.journal_entries.docs
               if e.get("source_ref") == b["id"] and e["source"] == "ap_payment"]
    lines = [l for l in env.journal_lines.docs
             if l["entry_id"] == entries[0]["id"]]
    assert by_code(lines) == {"2010": (7500, 0), "1020": (0, 7500)}


# ============================ fx router ============================
def test_manual_rate_endpoint(env):
    doc = run(fx_router.manual_rate(
        fx_router.ManualRateBody(quote="EUR", rate=1.25, date=TODAY), USER))
    assert doc["rate"] == "1.25" and doc["source"] == FX.SOURCE_MANUAL
    s = run(FX.resolve_stamp(env, TID, "USD", "EUR", on_date=TODAY))
    assert s["fx_rate"] == "1.25" and s["fx_source"] == FX.SOURCE_MANUAL
