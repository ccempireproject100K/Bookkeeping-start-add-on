"""Bake-off wave 4 (autopay #15) + standout extras A/B/C tests."""
import asyncio
import io
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/bakeoff_test")
os.environ.setdefault("DB_NAME", "bakeoff_test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import HTTPException

USER = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.test", "roles": ["owner"]}


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


class FakeAggCursor(FakeCursor):
    pass


class FakeColl:
    def __init__(self):
        self.docs = {}
        self._seq = 0

    async def insert_one(self, doc):
        self._seq += 1
        doc.setdefault("_id", f"oid{self._seq}")
        self.docs[doc["_id"]] = dict(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

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
                if "$gt" in v and not (dv is not None and dv > v["$gt"]):
                    return False
                if "$ne" in v and dv == v["$ne"]:
                    return False
                if "$exists" in v and ((k in doc) != bool(v["$exists"])):
                    return False
            elif dv != v:
                return False
        return True

    def find(self, q=None, proj=None):
        return FakeCursor([d for d in self.docs.values() if self._match_op(d, q or {})])

    async def find_one(self, q=None, proj=None):
        for d in self.docs.values():
            if self._match_op(d, q or {}):
                return dict(d)
        return None

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match_op(d, q or {}))

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs.values():
            if self._match_op(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                for k, v in (upd.get("$push") or {}).items():
                    d.setdefault(k, []).append(v)
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    async def find_one_and_update(self, q, upd, upsert=False, return_document=False):
        for d in self.docs.values():
            if self._match_op(d, q):
                for k, v in (upd.get("$inc") or {}).items():
                    d[k] = (d.get(k) or 0) + v
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                return dict(d)
        if upsert:
            doc = {k: v for k, v in q.items() if not isinstance(v, dict)}
            for k, v in (upd.get("$inc") or {}).items():
                doc[k] = v
            for k, v in (upd.get("$set") or {}).items():
                doc[k] = v
            await self.insert_one(doc)
            return dict(doc)
        return None

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


class FakeDB:
    COLLS = ["accounts", "journal_entries", "journal_lines", "fiscal_periods",
             "audit_log", "counters", "tenants", "customers", "invoices",
             "autopay_configs", "saved_payment_methods", "autopay_attempts",
             "balance_assertions", "bank_reconciliations", "bank_transactions",
             "tax_estimates"]

    def __init__(self):
        for name in self.COLLS:
            setattr(self, name, FakeColl())

    async def seed(self, coll, doc):
        doc = {"tenant_id": "t1", **doc}
        await getattr(self, coll).insert_one(doc)
        return doc


class FakeStripe:
    """Minimal Stripe stand-in: succeeds for pm_good, declines pm_bad."""
    def __init__(self):
        self.charges = []

    class PaymentIntent:
        _outer = None

        @classmethod
        def create(cls, **kw):
            cls._outer.charges.append(kw)
            if kw["payment_method"] == "pm_bad":
                raise Exception("Your card was declined.")
            return {"id": "pi_123", "status": "succeeded"}


def _setup_autopay(monkeypatch):
    import routers.autopay as AP
    db = FakeDB()
    fake = FakeStripe()
    FakeStripe.PaymentIntent._outer = fake
    monkeypatch.setattr(AP, "db", db)
    monkeypatch.setattr(AP, "_require_test_stripe", lambda: fake)
    return AP, db, fake


def test_stripe_mode_and_live_refusal(monkeypatch):
    import routers.autopay as AP
    assert AP.stripe_mode("sk_test_abc") == "test"
    assert AP.stripe_mode("sk_live_abc") == "live"
    assert AP.stripe_mode(None) is None
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_should_be_refused")
    with pytest.raises(HTTPException) as ei:
        AP._require_test_stripe()
    assert ei.value.status_code == 503
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    with pytest.raises(HTTPException):
        AP._require_test_stripe()


def test_retry_ladder():
    import routers.autopay as AP
    assert AP.next_retry_date(1, [1, 3, 7], "2026-10-30") == "2026-10-31"
    assert AP.next_retry_date(3, [1, 3, 7], "2026-10-30") == "2026-11-06"
    assert AP.next_retry_date(4, [1, 3, 7], "2026-10-30") is None


def test_autopay_happy_and_decline(monkeypatch):
    AP, db, fake = _setup_autopay(monkeypatch)

    async def main():
        await db.seed("tenants", {"id": "t1", "name": "Maple HVAC"})
        await db.seed("accounts", {"id": "a1050", "code": "1050", "name": "Stripe Clearing", "type": "asset"})
        await db.seed("accounts", {"id": "a1200", "code": "1200", "name": "Accounts Receivable", "type": "asset"})
        await db.seed("autopay_configs", {"id": "config", "enabled": True,
                                          "retry_days": [1, 3, 7]})
        await db.seed("customers", {"id": "c1", "name": "Pat"})
        await db.seed("saved_payment_methods", {
            "id": "m1", "customer_id": "c1", "stripe_customer_id": "cus_1",
            "stripe_payment_method_id": "pm_good", "status": "active",
            "consent": {"granted": True}})
        await db.seed("saved_payment_methods", {
            "id": "m2", "customer_id": "c2", "stripe_customer_id": "cus_2",
            "stripe_payment_method_id": "pm_bad", "status": "active",
            "consent": {"granted": True}})
        await db.seed("invoices", {
            "id": "inv1", "customer_id": "c1", "template_id": "tpl1",
            "status": "issued", "total_cents": 5000, "paid_cents": 0,
            "due_date": "2026-01-01", "client_name": "Pat"})
        await db.seed("invoices", {
            "id": "inv2", "customer_id": "c2", "template_id": "tpl1",
            "status": "issued", "total_cents": 7000, "paid_cents": 0,
            "due_date": "2026-01-01", "client_name": "Sam"})
        await db.seed("invoices", {   # ad-hoc invoice: never autopay-eligible
            "id": "inv3", "customer_id": "c1", "status": "issued",
            "total_cents": 9000, "paid_cents": 0, "due_date": "2026-01-01"})
        res = await AP.run_due_for_user(USER)
        assert res["charged"] == 1 and res["failed"] == 1
        inv1 = await db.invoices.find_one({"id": "inv1"})
        assert inv1["status"] == "paid" and inv1["payments"][0]["method"] == "card_autopay"
        inv2 = await db.invoices.find_one({"id": "inv2"})
        assert inv2["autopay_status"] == "retry_scheduled" and inv2["autopay_next_retry"]
        inv3 = await db.invoices.find_one({"id": "inv3"})
        assert inv3["status"] == "issued" and "autopay_status" not in inv3
        # consent is mandatory for saving a method
        with pytest.raises(HTTPException):
            await AP.save_method(AP.SaveMethodIn(
                customer_id="c1", payment_method_id="pm_x", consent=False), USER)

    asyncio.run(main())


def test_autopay_disabled_by_default(monkeypatch):
    AP, db, fake = _setup_autopay(monkeypatch)

    async def main():
        res = await AP.run_due_for_user(USER)
        assert res["enabled"] is False and res["charged"] == 0

    asyncio.run(main())


# ---------- extra B: tax packet ----------

def test_tax_packet_assembly(monkeypatch):
    import routers.taxpacket as TP
    db = FakeDB()
    monkeypatch.setattr(TP, "db", db)

    async def main():
        await db.seed("tenants", {"id": "t1", "name": "Maple HVAC", "niche_id": "hvac"})
        await db.seed("journal_entries", {"id": "e1", "number": "JE-1",
                                          "entry_date": "2026-03-01", "status": "posted"})
        await db.seed("journal_lines", {"id": "l1", "entry_id": "e1", "account_code": "1010",
                                        "debit": 100000, "credit": 0, "status": "posted",
                                        "entry_date": "2026-03-01"})
        await db.seed("journal_lines", {"id": "l2", "entry_id": "e1", "account_code": "4010",
                                        "debit": 0, "credit": 100000, "status": "posted",
                                        "entry_date": "2026-03-01"})
        packet = await TP.build_packet(USER, 2026)
        assert packet["profit_and_loss"]["net_profit_cents"] == 100000
        assert packet["tax"]["status"] == "awaiting_tax_engine"
        assert packet["tax"]["engine_inputs"]["net_profit_cents"] == 100000
        assert "PROFIT AND LOSS" in TP._packet_csv(packet)
        assert TP._packet_pdf(packet)[:4] == b"%PDF"
        # when a merged estimate exists, it is included instead
        await db.seed("tax_estimates", {"id": "te1", "year": 2026,
                                        "total_cents": 1664746})
        packet2 = await TP.build_packet(USER, 2026)
        assert packet2["tax"]["status"] == "included"

    asyncio.run(main())


# ---------- extra C: health score ----------

def test_health_score_bands():
    import routers.dashboard as DASH
    good = DASH.compute_health_score({
        "days_since_reconciliation": 10, "ar_open_count": 10, "ar_overdue_count": 0,
        "txn_total_90d": 100, "txn_uncategorized_90d": 2,
        "assertions_failed_unwaived": 0, "unbalanced_entries": 0})
    assert good["score"] == 100 and good["grade"] == "excellent" and not good["flags"]
    bad = DASH.compute_health_score({
        "days_since_reconciliation": None, "ar_open_count": 10, "ar_overdue_count": 6,
        "txn_total_90d": 100, "txn_uncategorized_90d": 60,
        "assertions_failed_unwaived": 2, "unbalanced_entries": 1})
    assert bad["score"] < 30 and len(bad["flags"]) >= 4
    assert all(isinstance(f, str) and f for f in bad["flags"])


# ---------- extra A: invoice2data pilot ----------

def test_pdf_intake_pilot():
    from lib.pdf_intake import extract_pdf_invoice
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import LETTER
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    c.drawString(72, 690, "Invoice #INV-77")
    c.drawString(72, 670, "Invoice Date: 10/15/2026")
    c.drawString(72, 610, "Total: $42.50")
    c.save()
    found = extract_pdf_invoice(buf.getvalue())
    assert found and found["amount"] == 42.50 and found["invoice_number"] == "INV-77"
    assert found["needs_review"] is True and found["source"] == "invoice2data"
    assert extract_pdf_invoice(b"not a pdf") is None


# ---------- extra B activation: real engine through the bridge ----------

def test_tax_engine_golden_case_via_bridge():
    from lib.taxengine import run_tax_estimate
    result = asyncio.run(run_tax_estimate(8_000_000, "US", {"filingStatus": "single"}))
    assert result["status"] == "OK"
    assert result["totalLiabilityCents"] == 1664746   # the engine's golden case
    assert result["schedule"] and result["workpapers"]


def test_tax_packet_estimate_endpoint_activates(monkeypatch):
    import routers.taxpacket as TP
    db = FakeDB()
    monkeypatch.setattr(TP, "db", db)

    async def main():
        await db.seed("tenants", {"id": "t1", "name": "Maple HVAC", "niche_id": "hvac"})
        await db.seed("journal_entries", {"id": "e1", "number": "JE-1",
                                          "entry_date": "2026-03-01", "status": "posted"})
        await db.seed("journal_lines", {"id": "l1", "entry_id": "e1", "account_code": "4010",
                                        "debit": 0, "credit": 8000000, "status": "posted",
                                        "entry_date": "2026-03-01"})
        out = await TP.compute_tax_estimate(2026, TP.TaxEstimateIn(), USER)
        assert out["total_liability_cents"] == 1664746
        packet = await TP.build_packet(USER, 2026)
        assert packet["tax"]["status"] == "included"
        assert packet["tax"]["estimate"]["result"]["totalLiabilityCents"] == 1664746

    asyncio.run(main())
