"""Bake-off Wave 2: portal quote acceptance, balance assertions + ledger
health check, transaction splits, job tags, customer deposits.

Clean-room builds (see bookkeeping-cleanroom-bakeoff-2026-10-30.md).
No live MongoDB needed.

Run from the backend directory with:
    python -m pytest tests/test_bakeoff_wave2.py
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from lib import ledger as L
from routers import banking as BK
from routers import reports as R
from routers import portal as PO
from routers import estimates as ES

TID = "tenant-1"
OWNER = {"id": "u1", "email": "owner@shop.test", "tenant_id": TID, "role": "owner"}


def _run(coro):
    return asyncio.run(coro)


def _matches(doc, filt):
    for key, cond in (filt or {}).items():
        if isinstance(cond, dict):
            for op, val in cond.items():
                dv = doc.get(key)
                if op == "$in" and dv not in val:
                    return False
                if op == "$ne" and dv == val:
                    return False
                if op == "$lt" and not (dv is not None and dv < val):
                    return False
                if op == "$lte" and not (dv is not None and dv <= val):
                    return False
                if op == "$gte" and not (dv is not None and dv >= val):
                    return False
        elif doc.get(key) != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec):
        if isinstance(spec, tuple):
            spec = [spec]
        for key, direction in reversed(spec):
            self.docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                           reverse=(direction == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=0):
        return self.docs


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    def find(self, filt=None, projection=None):
        return FakeCursor([d for d in self.docs if _matches(d, filt)])

    async def find_one(self, filt=None, projection=None):
        for d in self.docs:
            if _matches(d, filt):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def insert_many(self, docs):
        for d in docs:
            self.docs.append(dict(d))

    async def update_one(self, filt, update, upsert=False):
        for d in self.docs:
            if _matches(d, filt):
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                for k, v in (update.get("$inc") or {}).items():
                    d[k] = d.get(k, 0) + v
                for k, v in (update.get("$push") or {}).items():
                    d.setdefault(k, []).append(v)
                return SimpleNamespace(modified_count=1, matched_count=1)
        return SimpleNamespace(modified_count=0, matched_count=0)

    async def delete_one(self, filt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, filt)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    async def count_documents(self, filt=None):
        return len([d for d in self.docs if _matches(d, filt)])

    def aggregate(self, pipeline):
        docs = list(self.docs)
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                key_field = spec["_id"].lstrip("$")
                groups = {}
                for d in docs:
                    k = d.get(key_field)
                    g = groups.setdefault(k, {"_id": k})
                    for out, expr in spec.items():
                        if out == "_id":
                            continue
                        if "$sum" in expr:
                            f = expr["$sum"].lstrip("$")
                            g[out] = g.get(out, 0) + (d.get(f) or 0)
                        elif "$first" in expr:
                            f = expr["$first"].lstrip("$")
                            g.setdefault(out, d.get(f))
                docs = list(groups.values())
        return FakeCursor(docs)


# ---------------------------------------------------------------- splits

def _txn(**kw):
    d = {"id": "tx1", "tenant_id": TID, "bank_account_id": "a1",
         "txn_date": "2026-10-05", "description": "SUPPLY HOUSE",
         "amount_cents": -10000, "status": "unmatched", "category_code": None,
         "tags": [], "vendor": None, "reconciled": False}
    d.update(kw)
    return d


def test_split_sums_exactly_and_unsplit_restores(monkeypatch):
    async def run():
        txns = FakeCollection([_txn()])
        monkeypatch.setattr(BK, "db", SimpleNamespace(bank_transactions=txns))
        body = BK.SplitBody(parts=[
            BK.SplitPart(amount=-60.0, category_code="5700", tags=["job-a"]),
            BK.SplitPart(amount=-40.0, category_code="5200", tags=["job-b"]),
        ])
        res = await BK.split_txn("tx1", body, OWNER)
        assert len(res["parts"]) == 2
        assert sum(p["amount_cents"] for p in res["parts"]) == -10000
        assert txns.docs[0]["status"] == "split"
        # wrong-sum split rejected on a fresh txn
        txns.docs.append(_txn(id="tx2"))
        bad = BK.SplitBody(parts=[BK.SplitPart(amount=-60.0), BK.SplitPart(amount=-30.0)])
        with pytest.raises(HTTPException) as exc:
            await BK.split_txn("tx2", bad, OWNER)
        assert exc.value.status_code == 422
        # unsplit restores
        out = await BK.unsplit_txn("tx1", OWNER)
        assert out["status"] == "unmatched" and out["parts_removed"] == 2
        assert len(txns.docs) == 2
    _run(run())


def test_split_rejects_direction_flip(monkeypatch):
    async def run():
        monkeypatch.setattr(BK, "db", SimpleNamespace(
            bank_transactions=FakeCollection([_txn()])))
        body = BK.SplitBody(parts=[BK.SplitPart(amount=-120.0), BK.SplitPart(amount=20.0)])
        with pytest.raises(HTTPException) as exc:
            await BK.split_txn("tx1", body, OWNER)
        assert exc.value.status_code == 422
    _run(run())


# ---------------------------------------------------------------- health check

def test_health_check_flags_unbalanced_and_duplicate(monkeypatch):
    async def run():
        accounts = FakeCollection([
            {"code": "1020", "tenant_id": TID}, {"code": "4020", "tenant_id": TID},
            {"code": "5700", "tenant_id": TID}])
        entries = FakeCollection([
            {"id": "e1", "tenant_id": TID, "number": "JE-1", "status": "posted",
             "entry_date": "2026-10-01", "total_cents": 5000, "source": "manual"},
            {"id": "e2", "tenant_id": TID, "number": "JE-2", "status": "posted",
             "entry_date": "2026-10-01", "total_cents": 5000, "source": "manual"},
            {"id": "e3", "tenant_id": TID, "number": "JE-3", "status": "posted",
             "entry_date": "2026-10-02", "total_cents": 7000, "source": "manual"},
        ])
        lines = FakeCollection([
            {"id": "l1", "tenant_id": TID, "entry_id": "e1", "entry_number": "JE-1", "account_code": "1020", "debit": 5000, "credit": 0, "status": "posted"},
            {"id": "l2", "tenant_id": TID, "entry_id": "e1", "entry_number": "JE-1", "account_code": "4020", "debit": 0, "credit": 5000, "status": "posted"},
            {"id": "l3", "tenant_id": TID, "entry_id": "e2", "entry_number": "JE-2", "account_code": "1020", "debit": 5000, "credit": 0, "status": "posted"},
            {"id": "l4", "tenant_id": TID, "entry_id": "e2", "entry_number": "JE-2", "account_code": "4020", "debit": 0, "credit": 5000, "status": "posted"},
            {"id": "l5", "tenant_id": TID, "entry_id": "e3", "entry_number": "JE-3", "account_code": "5700", "debit": 7000, "credit": 0, "status": "posted"},
            {"id": "l6", "tenant_id": TID, "entry_id": "e3", "entry_number": "JE-3", "account_code": "1020", "debit": 0, "credit": 6000, "status": "posted"},
        ])
        fake = SimpleNamespace(accounts=accounts, journal_entries=entries,
                               journal_lines=lines, fiscal_periods=FakeCollection(),
                               audit_log=FakeCollection(), counters=FakeCollection())
        res = await L.run_health_check(fake, TID)
        kinds = {v["kind"] for v in res["violations"]}
        assert "unbalanced_entry" in kinds
        assert "duplicate_entry" in kinds
        assert res["ok"] is False
    _run(run())


def test_health_check_clean_book(monkeypatch):
    async def run():
        fake = SimpleNamespace(
            accounts=FakeCollection([{"code": "1020", "tenant_id": TID},
                                     {"code": "4020", "tenant_id": TID}]),
            journal_entries=FakeCollection([
                {"id": "e1", "tenant_id": TID, "number": "JE-1", "status": "posted",
                 "entry_date": "2026-10-01", "total_cents": 5000, "source": "manual"}]),
            journal_lines=FakeCollection([
                {"id": "l1", "tenant_id": TID, "entry_id": "e1", "entry_number": "JE-1", "account_code": "1020", "debit": 5000, "credit": 0, "status": "posted"},
                {"id": "l2", "tenant_id": TID, "entry_id": "e1", "entry_number": "JE-1", "account_code": "4020", "debit": 0, "credit": 5000, "status": "posted"}]),
            fiscal_periods=FakeCollection(), audit_log=FakeCollection(),
            counters=FakeCollection())
        res = await L.run_health_check(fake, TID)
        assert res["ok"] is True and res["violations"] == []
    _run(run())


# ---------------------------------------------------------------- assertions

def test_assertion_pass_and_fail(monkeypatch):
    async def run():
        accounts = FakeCollection([{"code": "1020", "tenant_id": TID},
                                   {"code": "4020", "tenant_id": TID}])
        lines = FakeCollection([
            {"id": "l1", "tenant_id": TID, "entry_id": "e1", "account_code": "1020", "debit": 10000, "credit": 0, "status": "posted", "entry_date": "2026-10-01"},
            {"id": "l2", "tenant_id": TID, "entry_id": "e1", "account_code": "4020", "debit": 0, "credit": 10000, "status": "posted", "entry_date": "2026-10-01"}])
        fake = SimpleNamespace(
            bank_accounts=FakeCollection([
                {"id": "ba1", "tenant_id": TID, "name": "Main", "gl_code": "1020"}]),
            balance_assertions=FakeCollection(), accounts=accounts,
            journal_entries=FakeCollection(), journal_lines=lines,
            fiscal_periods=FakeCollection(), audit_log=FakeCollection(),
            counters=FakeCollection())
        monkeypatch.setattr(R, "db", fake)
        ok = await R.create_assertion(
            R.AssertionBody(bank_account_id="ba1", statement_date="2026-10-31",
                            statement_balance=100.0), OWNER)
        assert ok["status"] == "passed" and ok["difference_cents"] == 0
        bad = await R.create_assertion(
            R.AssertionBody(bank_account_id="ba1", statement_date="2026-10-31",
                            statement_balance=90.0), OWNER)
        assert bad["status"] == "failed" and bad["difference_cents"] == -1000
        # health endpoint surfaces the failure
        health = await R.ledger_health(OWNER)
        assert health["ok"] is False
        assert health["counts"]["assertion_failures"] == 1
        # waive it -> health clears (assertion itself remains on record)
        w = await R.waive_assertion(bad["id"], R.WaiveBody(reason="bank fee not yet imported"), OWNER)
        assert w["waived"] is True
        health2 = await R.ledger_health(OWNER)
        assert health2["counts"]["assertion_failures"] == 0
    _run(run())


# ---------------------------------------------------------------- portal quote acceptance

def _estimate_link_setup(monkeypatch, est_status="sent"):
    est = {"id": "est1", "tenant_id": TID, "number": "EST-0001",
           "client_name": "Smith", "customer_id": "c1", "status": est_status,
           "lines": [{"kind": "service", "name": "Install", "amount": 900.0}],
           "subtotal_cents": 90000, "tax_cents": 0, "total_cents": 90000,
           "deposit_cents": 20000, "deposit_due_date": "2026-11-01",
           "invoice_id": None}
    links = FakeCollection()
    fake = SimpleNamespace(estimates=FakeCollection([est]), portal_links=links,
                           tenants=FakeCollection([{"id": TID, "name": "Shop"}]),
                           invoices=FakeCollection())
    monkeypatch.setattr(PO, "db", fake)
    monkeypatch.setattr(PO, "_audit", AsyncMock())
    return fake, est


def test_portal_estimate_accept_converts(monkeypatch):
    async def run():
        fake, est = _estimate_link_setup(monkeypatch)
        link = await PO.create_estimate_link(PO.EstimateLinkIn(estimate_id="est1"), OWNER)
        token = link["token"]
        view = await PO.portal_estimate_view(token)
        assert view["estimate"]["total_cents"] == 90000
        assert view["estimate"]["deposit_cents"] == 20000

        async def fake_convert(estimate_id, user):
            assert user["tenant_id"] == TID
            return {"id": "inv-9", "total_cents": 90000}
        monkeypatch.setattr(ES, "convert_to_invoice", fake_convert)
        res = await PO.portal_estimate_respond(
            token, PO.EstimateRespondIn(decision="accept", name="Pat Smith",
                                        terms_accepted=True))
        assert res["status"] == "accepted" and res["invoice_id"] == "inv-9"
        stored = fake.estimates.docs[0]
        assert stored["portal_response"]["name"] == "Pat Smith"
        assert stored["portal_response"]["decision"] == "accept"
        # second answer refused
        fake2, _ = _estimate_link_setup(monkeypatch)
        fake2.estimates.docs[0]["portal_response"] = stored["portal_response"]
        fake2.estimates.docs[0]["status"] = "accepted"
        link2 = await PO.create_estimate_link(PO.EstimateLinkIn(estimate_id="est1"), OWNER)
        with pytest.raises(HTTPException) as exc:
            await PO.portal_estimate_respond(
                link2["token"], PO.EstimateRespondIn(decision="accept", name="X",
                                                     terms_accepted=True))
        assert exc.value.status_code == 409
    _run(run())


def test_portal_estimate_decline_and_terms_gate(monkeypatch):
    async def run():
        fake, est = _estimate_link_setup(monkeypatch)
        link = await PO.create_estimate_link(PO.EstimateLinkIn(estimate_id="est1"), OWNER)
        with pytest.raises(HTTPException) as exc:
            await PO.portal_estimate_respond(
                link["token"], PO.EstimateRespondIn(decision="accept", name="Pat",
                                                    terms_accepted=False))
        assert exc.value.status_code == 422
        res = await PO.portal_estimate_respond(
            link["token"], PO.EstimateRespondIn(decision="decline", name="Pat"))
        assert res["status"] == "declined"
        assert fake.estimates.docs[0]["status"] == "declined"
    _run(run())


# ---------------------------------------------------------------- deposits

def test_deposit_record_and_apply(monkeypatch):
    async def run():
        est = {"id": "est1", "tenant_id": TID, "number": "EST-0001",
               "client_name": "Smith", "customer_id": "c1", "status": "accepted",
               "lines": [], "invoice_id": None}
        inv = {"id": "inv1", "tenant_id": TID, "client_name": "Smith",
               "total_cents": 90000, "paid_cents": 0, "status": "issued",
               "payments": []}
        deps = FakeCollection()
        invs = FakeCollection([inv])
        fake = SimpleNamespace(estimates=FakeCollection([est]),
                               customer_deposits=deps, invoices=invs)
        monkeypatch.setattr(ES, "db", fake)
        entries = []

        async def fake_post(db_, tid, user, draft, status="posted", idempotency_key=None):
            entries.append(draft)
            return {"id": f"je-{len(entries)}", "number": f"JE-{len(entries)}"}
        monkeypatch.setattr(ES.L, "post_journal_entry", fake_post)
        dep = await ES.record_deposit(
            "est1", ES.DepositBody(amount=200.0, payment_method="card"), OWNER)
        assert dep["remaining_cents"] == 20000 and dep["status"] == "held"
        assert entries[0]["lines"][0]["account_code"] == "1050"   # card cash
        assert entries[0]["lines"][1]["account_code"] == "2200"   # held liability
        applied = await ES._apply_deposits(OWNER, "est1", "inv1")
        assert applied == 20000
        assert deps.docs[0]["status"] == "applied"
        assert invs.docs[0]["paid_cents"] == 20000
        assert invs.docs[0]["status"] == "partial"
        assert invs.docs[0]["payments"][0]["method"] == "deposit"
    _run(run())


# ---------------------------------------------------------------- by-tag report

def test_by_tag_report(monkeypatch):
    async def run():
        txns = FakeCollection([
            {"id": "t1", "tenant_id": TID, "status": "cleared", "txn_date": "2026-10-02",
             "amount_cents": -5000, "tags": ["job-a"]},
            {"id": "t2", "tenant_id": TID, "status": "cleared", "txn_date": "2026-10-03",
             "amount_cents": -3000, "tags": ["job-a", "rush"]},
            {"id": "t3", "tenant_id": TID, "status": "unmatched", "txn_date": "2026-10-03",
             "amount_cents": -9999, "tags": ["job-a"]},
        ])
        monkeypatch.setattr(R, "db", SimpleNamespace(bank_transactions=txns))
        res = await R.by_tag(user=OWNER)
        by = {a["tag"]: a for a in res["tags"]}
        assert by["job-a"]["out_cents"] == 8000 and by["job-a"]["count"] == 2
        assert by["rush"]["net_cents"] == -3000
    _run(run())
