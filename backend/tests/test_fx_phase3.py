"""Phase 3 multi-currency: forex gain/loss postings.

Covers realized gain/loss on invoice/bill payments (same- and
cross-currency), the USD-only no-op path, unrealized month-end
revaluation math, idempotency of re-runs, and reversal of unrealized
amounts when a document is later paid.

No live MongoDB or network needed (fully faked). The journal-posting
layer is captured, not executed, via fx_posting._post_entry.

Run from the backend directory with: python -m pytest tests/test_fx_phase3.py
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib import currencies as C
from lib import fx_posting as FXP
from lib.base import Scoped
from lib.coa import (FX_GAIN_CODE, FX_LOSS_CODE, ensure_fx_accounts,
                     ACCOUNT_TYPE_OF)

TID = "tenant-1"
USER = {"id": "u1", "email": "owner@shop.test", "tenant_id": TID}


# ---------------------------------------------------------------- fakes

class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec):
            self.docs.sort(key=lambda d: d.get(key),
                           reverse=(direction == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=0):
        return self.docs


class FakeCollection:
    """Minimal async Mongo stand-in."""

    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    def _match(self, doc, filt):
        for key, cond in filt.items():
            if isinstance(cond, dict):
                for op, val in cond.items():
                    if op == "$lte" and not (doc.get(key) is not None
                                             and doc.get(key) <= val):
                        return False
                    elif op == "$in" and doc.get(key) not in val:
                        return False
                    elif op == "$ne" and doc.get(key) == val:
                        return False
            elif doc.get(key) != cond:
                return False
        return True

    async def find_one(self, filt=None, *args, sort=None, projection=None):
        # motor: find_one(filter, projection) — second positional is projection
        if args and projection is None:
            projection = args[0]
        cands = [d for d in self.docs if self._match(d, filt or {})]
        if sort:
            for key, direction in reversed(sort):
                cands.sort(key=lambda d: d.get(key),
                           reverse=(direction == -1))
        return dict(cands[0]) if cands else None

    def find(self, filt=None, projection=None):
        return FakeCursor([d for d in self.docs
                           if self._match(d, filt or {})])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=len(self.docs))

    async def insert_many(self, docs):
        for d in docs:
            self.docs.append(dict(d))
        return SimpleNamespace(inserted_ids=list(range(len(docs))))

    async def update_one(self, filt, update, upsert=False):
        for doc in self.docs:
            if self._match(doc, filt):
                doc.update(update.get("$set", {}))
                for k, v in update.get("$inc", {}).items():
                    doc[k] = doc.get(k, 0) + v
                return SimpleNamespace(matched_count=1)
        if upsert:
            new = {k: v for k, v in filt.items()
                   if not isinstance(v, dict)}
            new.update(update.get("$set", {}))
            self.docs.append(new)
            return SimpleNamespace(matched_count=0)
        return SimpleNamespace(matched_count=0)

    async def delete_one(self, filt):
        for i, doc in enumerate(self.docs):
            if self._match(doc, filt):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def count_documents(self, filt=None):
        return sum(1 for d in self.docs if self._match(d, filt or {}))


def _rates(*triples):
    """triples: (base, quote, date, rate)"""
    coll = FakeCollection()
    for base, quote, date, rate in triples:
        coll.docs.append({"base": base, "quote": quote, "date": date,
                          "rate": rate, "source": "test"})
    return coll


def _db(rates_coll=None, tenants_settings=None, extra=None):
    tenants = FakeCollection([{"id": TID, "tenant_id": TID,
                               "settings": tenants_settings or {}}])
    db = SimpleNamespace(
        tenants=tenants,
        accounts=FakeCollection(),
        journal_entries=FakeCollection(),
        journal_lines=FakeCollection(),
        invoices=FakeCollection(),
        bills=FakeCollection(),
        fx_reval_state=FakeCollection(),
        rates=rates_coll or FakeCollection(),
    )
    for k, v in (extra or {}).items():
        setattr(db, k, v)
    return db


class _Capture:
    """Stand-in for fx_posting._post_entry: records drafts, returns entries."""

    def __init__(self):
        self.drafts = []
        self.n = 0

    async def __call__(self, db, tenant_id, user, draft, status="posted"):
        self.n += 1
        self.drafts.append(dict(draft))
        return {"id": f"je-{self.n}", "number": f"JE-TEST-{self.n:04d}"}


def _with_capture(monkeypatch):
    cap = _Capture()

    async def fake(db, tenant_id, user, draft, status="posted"):
        return await cap(db, tenant_id, user, draft, status=status)

    monkeypatch.setattr(FXP, "_post_entry", fake)
    return cap


def _invoice(currency="EUR", total=10000, paid=0, issue_entry="JE-000001"):
    return {"id": "inv-1", "tenant_id": TID, "currency": currency,
            "total_cents": total, "paid_cents": paid,
            "client_name": "Acme", "issue_entry": issue_entry,
            "status": "issued", "created_at": "2026-01-15T10:00:00"}


def _bill(currency="GBP", amount=20000, paid=0):
    return {"id": "bill-1", "tenant_id": TID, "currency": currency,
            "amount_cents": amount, "paid_cents": paid, "vendor": "Vendor",
            "bill_date": "2026-01-10", "status": "open",
            "created_at": "2026-01-10T10:00:00"}


def _issue_entry(db, number="JE-000001", entry_date="2026-01-15"):
    db.journal_entries.docs.append(
        {"id": "e-issue", "tenant_id": TID, "number": number,
         "entry_date": entry_date, "status": "posted"})


# ---------------------------------------------------------------- tests

def test_fx_accounts_registered_and_created_once():
    assert ACCOUNT_TYPE_OF[FX_GAIN_CODE] == "revenue"
    assert ACCOUNT_TYPE_OF[FX_LOSS_CODE] == "expense"

    async def go():
        db = _db()
        accounts = Scoped(db.accounts, TID)
        created = await ensure_fx_accounts(accounts)
        assert set(created) == {FX_GAIN_CODE, FX_LOSS_CODE}
        again = await ensure_fx_accounts(accounts)
        assert again == []
        codes = {a["code"] for a in await accounts.find_list()}
        assert {FX_GAIN_CODE, FX_LOSS_CODE} <= codes

    asyncio.run(go())


def test_realized_gain_invoice_same_currency():
    """EUR 100 invoice @1.10, paid EUR 100 @1.12 -> $2.00 gain."""
    async def go():
        rates = _rates(("EUR", "USD", "2026-01-15", 1.10),
                       ("EUR", "USD", "2026-02-15", 1.12))
        db = _db(rates)
        _issue_entry(db)
        cap = []
        orig = FXP._post_entry

        async def fake(db_, tid, user, draft, status="posted"):
            cap.append(draft)
            return {"id": "je-1", "number": "JE-TEST-0001"}

        FXP._post_entry = fake
        try:
            entry = await FXP.post_realized_fx(
                db=db, tenant_id=TID, user=USER, kind="invoice",
                doc=_invoice(), pay_currency="EUR", pay_amount_minor=10000,
                pay_date="2026-02-15")
        finally:
            FXP._post_entry = orig
        assert entry["number"] == "JE-TEST-0001"
        (draft,) = cap
        assert draft["currency"] == "USD"
        assert draft["source"] == "fx_realized"
        lines = draft["lines"]
        assert lines[0]["account_code"] == "1200" and lines[0]["debit"] == 200
        assert lines[1]["account_code"] == FX_GAIN_CODE and lines[1]["credit"] == 200
        det = draft["details"]
        assert det["doc_currency"] == "EUR" and det["pay_currency"] == "EUR"
        assert det["doc_rate_to_base"] == 1.10 and det["pay_rate_to_base"] == 1.12
        assert det["doc_date"] == "2026-01-15" and det["pay_date"] == "2026-02-15"
        assert det["diff_base_minor"] == 200

    asyncio.run(go())


def test_realized_loss_bill_cross_currency():
    """GBP 200 bill @1.25 ($250), paid $260 USD -> $10.00 loss."""
    async def go():
        rates = _rates(("GBP", "USD", "2026-01-10", 1.25),
                       ("GBP", "USD", "2026-02-10", 1.27))
        db = _db(rates)
        cap = []
        orig = FXP._post_entry

        async def fake(db_, tid, user, draft, status="posted"):
            cap.append(draft)
            return {"id": "je-1", "number": "JE-TEST-0001"}

        FXP._post_entry = fake
        try:
            entry = await FXP.post_realized_fx(
                db=db, tenant_id=TID, user=USER, kind="bill",
                doc=_bill(), pay_currency="USD", pay_amount_minor=26000,
                applied_doc_minor=20000, pay_date="2026-02-10")
        finally:
            FXP._post_entry = orig
        assert entry is not None
        (draft,) = cap
        lines = draft["lines"]
        # loss: dr 5960 / cr 2010
        assert lines[0]["account_code"] == FX_LOSS_CODE and lines[0]["debit"] == 1000
        assert lines[1]["account_code"] == "2010" and lines[1]["credit"] == 1000
        assert draft["details"]["diff_base_minor"] == -1000

    asyncio.run(go())


def test_noop_usd_only_flow():
    async def go():
        db = _db(_rates())
        called = []

        async def fake(db_, tid, user, draft, status="posted"):
            called.append(draft)
            return {"id": "je-x", "number": "JE-X"}

        orig = FXP._post_entry
        FXP._post_entry = fake
        try:
            res = await FXP.post_realized_fx(
                db=db, tenant_id=TID, user=USER, kind="invoice",
                doc=_invoice(currency="USD"), pay_currency="USD",
                pay_amount_minor=5000, pay_date="2026-02-15")
        finally:
            FXP._post_entry = orig
        assert res is None and called == []

    asyncio.run(go())


def test_noop_zero_difference():
    """Same rate on both dates -> no posting."""
    async def go():
        rates = _rates(("EUR", "USD", "2026-01-15", 1.10),
                       ("EUR", "USD", "2026-02-15", 1.10))
        db = _db(rates)
        _issue_entry(db)
        called = []
        orig = FXP._post_entry

        async def fake(db_, tid, user, draft, status="posted"):
            called.append(draft)
            return {"id": "je-x", "number": "JE-X"}

        FXP._post_entry = fake
        try:
            res = await FXP.post_realized_fx(
                db=db, tenant_id=TID, user=USER, kind="invoice",
                doc=_invoice(), pay_currency="EUR", pay_amount_minor=10000,
                pay_date="2026-02-15")
        finally:
            FXP._post_entry = orig
        assert res is None and called == []

    asyncio.run(go())


def test_missing_rates_never_breaks_payment():
    async def go():
        db = _db(_rates())  # empty rates
        _issue_entry(db)
        called = []
        orig = FXP._post_entry

        async def fake(db_, tid, user, draft, status="posted"):
            called.append(draft)
            return {"id": "je-x", "number": "JE-X"}

        FXP._post_entry = fake
        try:
            res = await FXP.post_realized_fx(
                db=db, tenant_id=TID, user=USER, kind="invoice",
                doc=_invoice(), pay_currency="EUR", pay_amount_minor=10000,
                pay_date="2026-02-15")
        finally:
            FXP._post_entry = orig
        assert res is None and called == []

    asyncio.run(go())


def test_unrealized_revaluation_and_idempotency():
    """EUR 100 invoice @1.10, revalue @1.08 -> $2 unrealized loss; re-run no-ops."""
    async def go():
        rates = _rates(("EUR", "USD", "2026-01-15", 1.10),
                       ("EUR", "USD", "2026-01-31", 1.08))
        db = _db(rates)
        db.invoices.docs.append(_invoice())
        _issue_entry(db)
        # no bank balances
        orig_bank = FXP._bank_balances_by_currency
        FXP._bank_balances_by_currency = AsyncMock(return_value={})
        drafts = []
        orig_post = FXP._post_entry

        async def fake(db_, tid, user, draft, status="posted"):
            drafts.append(draft)
            return {"id": f"je-{len(drafts)}",
                    "number": f"JE-TEST-{len(drafts):04d}"}

        FXP._post_entry = fake
        try:
            r1 = await FXP.revalue_open_balances(db, TID, USER, "2026-01-31")
            r2 = await FXP.revalue_open_balances(db, TID, USER, "2026-01-31")
        finally:
            FXP._post_entry = orig_post
            FXP._bank_balances_by_currency = orig_bank
        assert r1["status"] == "posted" and r1["entries_posted"] == 1
        assert r2["status"] == "already_done" and r2["entries_posted"] == 0
        assert len(drafts) == 1
        (draft,) = drafts
        assert draft["source"] == "fx_revalue"
        assert draft["currency"] == "USD"
        lines = draft["lines"]
        assert lines[0]["account_code"] == FX_LOSS_CODE and lines[0]["debit"] == 200
        assert lines[1]["account_code"] == "1200" and lines[1]["credit"] == 200
        det = draft["details"]
        assert det["kind"] == "unrealized" and det["delta_base_minor"] == -200
        assert det["revalue_date"] == "2026-01-31"

    asyncio.run(go())


def test_unrealized_reversal_on_payment():
    """Unrealized loss reverses when the invoice is later paid; realized
    is then computed against the original recognition rate."""
    async def go():
        rates = _rates(("EUR", "USD", "2026-01-15", 1.10),
                       ("EUR", "USD", "2026-01-31", 1.08),
                       ("EUR", "USD", "2026-02-15", 1.09))
        db = _db(rates)
        db.invoices.docs.append(_invoice())
        _issue_entry(db)
        orig_bank = FXP._bank_balances_by_currency
        FXP._bank_balances_by_currency = AsyncMock(return_value={})
        drafts = []
        orig_post = FXP._post_entry

        async def fake(db_, tid, user, draft, status="posted"):
            drafts.append(draft)
            return {"id": f"je-{len(drafts)}",
                    "number": f"JE-TEST-{len(drafts):04d}"}

        FXP._post_entry = fake
        try:
            await FXP.revalue_open_balances(db, TID, USER, "2026-01-31")
            assert len(drafts) == 1  # the unrealized loss
            await FXP.post_realized_fx(
                db=db, tenant_id=TID, user=USER, kind="invoice",
                doc=_invoice(), pay_currency="EUR", pay_amount_minor=10000,
                pay_date="2026-02-15")
        finally:
            FXP._post_entry = orig_post
            FXP._bank_balances_by_currency = orig_bank
        assert len(drafts) == 3
        unreal, reversal, realized = drafts
        assert unreal["source"] == "fx_revalue"
        # reversal: dr 1200 200 / cr 5960 200
        assert reversal["source"] == "fx_revalue_reversal"
        assert reversal["lines"][0]["account_code"] == "1200"
        assert reversal["lines"][0]["debit"] == 200
        assert reversal["lines"][1]["account_code"] == FX_LOSS_CODE
        assert reversal["lines"][1]["credit"] == 200
        # realized: 100*(1.09-1.10) = -$1.00 loss
        assert realized["source"] == "fx_realized"
        assert realized["details"]["diff_base_minor"] == -100
        assert realized["lines"][0]["account_code"] == FX_LOSS_CODE

    asyncio.run(go())


def test_revalue_skips_base_currency_docs():
    async def go():
        rates = _rates()
        db = _db(rates)
        db.invoices.docs.append(_invoice(currency="USD"))
        db.bills.docs.append(_bill(currency="USD"))
        orig_bank = FXP._bank_balances_by_currency
        FXP._bank_balances_by_currency = AsyncMock(return_value={})
        drafts = []
        orig_post = FXP._post_entry

        async def fake(db_, tid, user, draft, status="posted"):
            drafts.append(draft)
            return {"id": "je-1", "number": "JE-TEST-0001"}

        FXP._post_entry = fake
        try:
            res = await FXP.revalue_open_balances(db, TID, USER, "2026-01-31")
        finally:
            FXP._post_entry = orig_post
            FXP._bank_balances_by_currency = orig_bank
        assert res["entries_posted"] == 0 and drafts == []

    asyncio.run(go())
