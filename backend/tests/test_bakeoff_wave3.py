"""Bake-off wave 3: PDF utilities (#14), plain-text ledger export (#9),
onboarding (#10), import profiles (#11), expected-bills calendar (#12),
cash-position dashboard (#13), guided field-mapping importer (#16).

PDF tests exercise the real libraries with generated documents — no
competitor fixtures involved. DB tests run against fake collections.
"""
import io
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/bakeoff_test")
os.environ.setdefault("DB_NAME", "bakeoff_test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

import pytest
from fastapi import HTTPException

from lib import pdfutil as PU
import lib.ledger as L
from lib.base import Scoped

USER = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.test",
        "roles": ["owner"]}


# ---------- PDF utilities (item 14) ----------

def _make_pdf(text="Hello books", pages=1):
    from pypdf import PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import LETTER
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    for i in range(pages):
        c.drawString(72, 700, f"{text} page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pdf_merge_split_count():
    a, b = _make_pdf("A"), _make_pdf("B", pages=2)
    merged = PU.merge_pdfs([a, b])
    assert PU.page_count(merged) == 3
    one = PU.extract_pages(merged, [2])
    assert PU.page_count(one) == 1
    with pytest.raises(ValueError):
        PU.extract_pages(merged, [9])


def test_pdf_text_and_csv():
    data = _make_pdf("Rent    1200.00")
    assert "Rent" in PU.extract_text(data)
    csv_out = PU.pdf_to_csv(data)
    assert "Rent" in csv_out and "1200.00" in csv_out


def test_pdf_watermark_encrypt_sign_compare():
    data = _make_pdf("Invoice 42")
    stamped = PU.stamp_pdf(data, "PAID")
    assert "PAID" in PU.extract_text(stamped)
    enc = PU.encrypt_pdf(data, "pw1234")
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(enc))
    assert r.is_encrypted
    signed = PU.sign_pdf(data, "Ada Owner", "2026-10-30")
    assert PU.page_count(signed) == 2
    assert "Ada Owner" in PU.extract_text(signed)
    cmp_ = PU.compare_pdfs(data, _make_pdf("Invoice 43"))
    assert cmp_["similarity"] < 1.0 and cmp_["added_lines"]


def test_pdf_form_fields_and_fill():
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import LETTER
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    c.drawString(72, 720, "Customer")
    c.acroForm.textfield(name="customer_name", x=72, y=650, width=200, height=20)
    c.showPage()
    c.save()
    data = buf.getvalue()
    fields = PU.form_fields(data)
    assert any(f["name"] == "customer_name" for f in fields)
    filled = PU.fill_form_pdf(data, {"customer_name": "Maple HVAC"})
    f2 = PU.form_fields(filled)
    assert any(f.get("value") == "Maple HVAC" for f in f2)


def test_pdf_redact_destroys_text():
    data = _make_pdf("Secret code 1234-ABCD here")
    red = PU.redact_pdf(data, ["1234-ABCD"])
    assert "1234-ABCD" not in PU.extract_text(red)


def test_pdf_from_images_and_ocr_fallback():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(buf, format="PNG")
    pdf = PU.images_to_pdf([buf.getvalue()])
    assert PU.page_count(pdf) == 1
    res = PU.ocr_pdf(_make_pdf("Embedded words"))
    assert "Embedded words" in res["text"]


# ---------- fake DB ----------

class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec or []):
            self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n=None, length=None):
        return self._docs[:(n if n is not None else length)]

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeAggCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return self._docs

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeColl:
    def __init__(self):
        self.docs = {}
        self._seq = 0

    async def insert_one(self, doc):
        self._seq += 1
        doc.setdefault("_id", f"oid{self._seq}")
        self.docs[doc["_id"]] = dict(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    def _match(self, doc, q):
        for k, v in (q or {}).items():
            dv = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]:
                    return False
                if "$lt" in v and not (isinstance(dv, (int, float)) and dv < v["$lt"]):
                    return False
                if "$ne" in v and dv == v["$ne"]:
                    return False
            elif dv != v:
                return False
        return True

    def find(self, q=None, proj=None):
        return FakeCursor([d for d in self.docs.values() if self._match(d, q)])

    async def find_one(self, q=None, proj=None):
        for d in self.docs.values():
            if self._match(d, q or {}):
                return dict(d)
        return None

    def _match_op(self, doc, q):
        for k, v in (q or {}).items():
            dv = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]:
                    return False
                if "$nin" in v and dv in v["$nin"]:
                    return False
                if "$lt" in v and not (dv is not None and dv < v["$lt"]):
                    return False
                if "$lte" in v and not (dv is not None and dv <= v["$lte"]):
                    return False
                if "$gte" in v and not (dv is not None and dv >= v["$gte"]):
                    return False
                if "$ne" in v and dv == v["$ne"]:
                    return False
                if "$exists" in v and ((k in doc) != bool(v["$exists"])):
                    return False
            elif dv != v:
                return False
        return True

    def aggregate(self, pipeline):
        docs = list(self.docs.values())
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if self._match_op(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                groups = {}
                for d in docs:
                    key = d.get(str(spec["_id"]).lstrip("$"))
                    g = groups.setdefault(key, {"_id": key})
                    for out_f, expr in spec.items():
                        if out_f == "_id":
                            continue
                        if isinstance(expr, dict) and "$sum" in expr:
                            src = str(expr["$sum"]).lstrip("$")
                            g[out_f] = g.get(out_f, 0) + (d.get(src) or 0)
                        elif isinstance(expr, dict) and "$first" in expr:
                            src = str(expr["$first"]).lstrip("$")
                            g.setdefault(out_f, d.get(src))
                docs = list(groups.values())
        return FakeAggCursor(docs)

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs.values():
            if self._match(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                for k, v in (upd.get("$addToSet") or {}).items():
                    cur = d.setdefault(k, [])
                    if v not in cur:
                        cur.append(v)
                return type("R", (), {"modified_count": 1})()
        if upsert:
            doc = dict(q)
            for k, v in (upd.get("$set") or {}).items():
                doc[k] = v
            for k, v in (upd.get("$addToSet") or {}).items():
                doc[k] = [v]
            await self.insert_one(doc)
        return type("R", (), {"modified_count": 0})()

    async def delete_one(self, q):
        for k, d in list(self.docs.items()):
            if self._match(d, q):
                del self.docs[k]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()


class FakeDB:
    def __init__(self):
        self.accounts = FakeColl()
        self.journal_entries = FakeColl()
        self.journal_lines = FakeColl()
        self.fiscal_periods = FakeColl()
        self.audit_log = FakeColl()
        self.counters = FakeColl()
        self.balance_assertions = FakeColl()
        self.tenants = FakeColl()
        self.bank_accounts = FakeColl()
        self.bank_connections = FakeColl()
        self.bank_transactions = FakeColl()
        self.customers = FakeColl()
        self.vendors = FakeColl()
        self.products = FakeColl()
        self.invoices = FakeColl()
        self.bills = FakeColl()
        self.recurring_bill_templates = FakeColl()
        self.recurring_bills = FakeColl()
        self.bank_rules = FakeColl()
        self.import_profiles = FakeColl()
        self.onboarding_progress = FakeColl()

    async def seed(self, coll, doc):
        doc = {"tenant_id": "t1", **doc}
        await getattr(self, coll).insert_one(doc)
        return doc


# ---------- ledger export round-trip (item 9) ----------

def test_ledger_export_round_trip():
    asyncio.run(_test_ledger_export_round_trip())


async def _test_ledger_export_round_trip():
    db = FakeDB()
    await db.seed("accounts", {"id": "a1", "code": "1010", "name": "Operating Cash", "type": "asset"})
    await db.seed("accounts", {"id": "a2", "code": "4010", "name": "Service Revenue", "type": "revenue"})
    await db.seed("journal_entries", {"id": "e1", "number": "JE-1", "entry_date": "2026-10-01",
                                      "status": "posted", "source": "banking", "memo": "Job payment"})
    await db.seed("journal_lines", {"id": "l1", "entry_id": "e1", "account_code": "1010",
                                    "debit": 25000, "credit": 0, "status": "posted", "memo": "", "entry_date": "2026-10-01"})
    await db.seed("journal_lines", {"id": "l2", "entry_id": "e1", "account_code": "4010",
                                    "debit": 0, "credit": 25000, "status": "posted", "memo": "", "entry_date": "2026-10-01"})
    text = await L.export_ledger_text(db, "t1", tenant_name="Maple HVAC")
    assert "sha256:" in text
    # independent re-parse: rebuild the trial balance from the text alone
    per_account = {}
    for line in text.splitlines():
        m = re.match(r"\s+LINE (\d+) \| (debit|credit) ([\d.]+)", line)
        if m:
            code, side, amt = m.group(1), m.group(2), int(round(float(m.group(3)) * 100))
            per_account.setdefault(code, {"debit": 0, "credit": 0})[side] += amt
    tb = await L.trial_balance(db, "t1", "2026-12-31")
    total_d = sum(v["debit"] for v in per_account.values())
    total_c = sum(v["credit"] for v in per_account.values())
    assert total_d == tb["total_debit"] == 25000
    assert total_c == tb["total_credit"] == 25000


# ---------- onboarding (item 10) ----------

def test_onboarding_steps_compute(monkeypatch):
    asyncio.run(_test_onboarding_steps_compute(monkeypatch))


async def _test_onboarding_steps_compute(monkeypatch):
    import routers.onboarding as OB
    db = FakeDB()
    await db.seed("tenants", {"id": "t1", "name": "Maple HVAC", "niche_id": "hvac"})
    await db.seed("bank_accounts", {"id": "ba1", "name": "Op"})
    monkeypatch.setattr(OB, "db", db)
    state = await OB.get_onboarding(USER)
    by_key = {s["key"]: s["done"] for s in state["steps"]}
    assert by_key["business_profile"] and by_key["trade_pack"] and by_key["connect_bank"]
    assert not by_key["first_customer"] and state["next_step"]["key"] == "tax_profile"
    with pytest.raises(HTTPException):
        await OB.complete_step(OB.CompleteIn(step="first_customer"), USER)
    await OB.complete_step(OB.CompleteIn(step="tax_profile"), USER)
    state = await OB.get_onboarding(USER)
    assert {s["key"]: s["done"] for s in state["steps"]}["tax_profile"]
    assert state["next_step"]["key"] == "first_customer"


# ---------- cash position (item 13) ----------

def test_cash_position(monkeypatch):
    asyncio.run(_test_cash_position(monkeypatch))


async def _test_cash_position(monkeypatch):
    import routers.dashboard as DASH
    from datetime import date, timedelta
    db = FakeDB()
    await db.seed("accounts", {"id": "a1", "code": "1010", "name": "Cash", "type": "asset"})
    await db.seed("journal_entries", {"id": "e1", "number": "JE-1", "entry_date": "2026-01-01",
                                      "status": "posted"})
    await db.seed("journal_lines", {"id": "l1", "entry_id": "e1", "account_code": "1010",
                                    "debit": 500000, "credit": 0, "status": "posted", "entry_date": "2026-01-01"})
    today = date.today().isoformat()
    soon = (date.today() + timedelta(days=10)).isoformat()
    old = (date.today() - timedelta(days=40)).isoformat()
    await db.seed("invoices", {"id": "i1", "status": "issued", "total_cents": 100000,
                               "paid_cents": 0, "due_date": soon})
    await db.seed("invoices", {"id": "i2", "status": "issued", "total_cents": 30000,
                               "paid_cents": 0, "due_date": old})
    await db.seed("bills", {"id": "b1", "status": "open", "amount_cents": 50000,
                            "paid_cents": 0, "due_date": soon})
    monkeypatch.setattr(DASH, "db", db)
    pos = await DASH.cash_position(USER)
    assert pos["cash_cents"] == 500000
    assert pos["receivables"]["open_cents"] == 130000
    assert pos["receivables"]["overdue_cents"] == 30000
    assert pos["payables"]["open_cents"] == 50000
    assert pos["next_30_days"]["projected_cash_cents"] == 500000 + 100000 - 50000


# ---------- expected bills (item 12) ----------

def test_expected_bills_match(monkeypatch):
    asyncio.run(_test_expected_bills_match(monkeypatch))


async def _test_expected_bills_match(monkeypatch):
    import routers.bills as BILLS
    from datetime import date, timedelta
    db = FakeDB()
    nxt = (date.today() + timedelta(days=5)).isoformat()
    await db.seed("recurring_bills", {
        "id": "rt1", "vendor": "City Power", "active": True, "status": "active",
        "frequency": "monthly", "next_run": nxt, "amount": 150.00, "line_items": []})
    await db.seed("bank_transactions", {
        "id": "tx1", "amount_cents": -15000, "txn_date": nxt,
        "description": "City Power utility"})
    monkeypatch.setattr(BILLS, "db", db)
    out = await BILLS.expected_bills(days=60, user=USER)
    first = out["expectations"][0]
    assert first["status"] == "matched" and first["matched_txn_id"] == "tx1"
    assert out["summary"]["matched"] >= 1


# ---------- import profiles (item 11) ----------

def test_import_profile_external_id_dedupe(monkeypatch):
    asyncio.run(_test_import_profile_external_id_dedupe(monkeypatch))


async def _test_import_profile_external_id_dedupe(monkeypatch):
    import routers.banking as BK
    db = FakeDB()
    await db.seed("bank_accounts", {"id": "ba1", "gl_code": "1010"})
    monkeypatch.setattr(BK, "db", db)
    prof = await BK.create_import_profile(BK.ImportProfileBody(
        name="Chase biz", column_map={"date": "Posted", "description": "Memo", "amount": "Amt"},
        dedupe_mode="external_id", external_id_column="Ref"), USER)
    csv_text = "Posted,Memo,Amt,Ref\n2026-10-01,Supply run,-25.00,R1\n2026-10-02,Deposit,100.00,R2\n"
    r1 = await BK.import_txns(BK.ImportBody(bank_account_id="ba1", csv_text=csv_text,
                                            profile_id=prof["id"]), USER)
    assert r1["imported"] == 2 and r1["dedupe_mode"] == "external_id"
    r2 = await BK.import_txns(BK.ImportBody(bank_account_id="ba1", csv_text=csv_text,
                                            profile_id=prof["id"]), USER)
    assert r2["imported"] == 0 and r2["duplicates"] == 2
    r3 = await BK.import_txns(BK.ImportBody(bank_account_id="ba1", csv_text=csv_text,
                                            profile_id=prof["id"], allow_duplicates=True), USER)
    assert r3["imported"] == 2   # deliberate import-anyway choice


# ---------- guided importer (item 16) ----------

def test_guided_importer(monkeypatch):
    asyncio.run(_test_guided_importer(monkeypatch))


async def _test_guided_importer(monkeypatch):
    import routers.importer as IMP
    db = FakeDB()
    monkeypatch.setattr(IMP, "db", db)

    async def no_mail(*a, **k):
        return ("skipped", "no smtp")
    monkeypatch.setattr(IMP, "send_email", no_mail)
    csv_text = "Client,E-mail,Phone\nMaple Dental,a@b.c,555-1\nMaple Dental,a@b.c,555-1\n,,555-2\n"
    prev = await IMP.preview(IMP.PreviewIn(kind="customers", csv_text=csv_text), USER)
    mapping = {m["csv"]: m["field"] for m in prev["mapping"]}
    assert mapping.get("Client") == "name" and mapping.get("E-mail") == "email"
    assert prev["error_count"] == 1
    out = await IMP.run_import(IMP.RunIn(kind="customers", csv_text=csv_text,
                                         email_summary=True), USER)
    assert out["created"] == 1 and out["duplicates_skipped"] == 1
    assert out["error_count"] == 1
