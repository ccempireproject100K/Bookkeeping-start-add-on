"""Bake-off Wave 1: bank rules engine v2, recurring automation, reminder
ladder extensions (flat fee, repeating final, bill-side reminders).

Clean-room builds — behavior studied from competitor docs, implemented
independently (see bookkeeping-cleanroom-bakeoff-2026-10-30.md).

No live MongoDB needed (db collections are faked).

Run from the backend directory with:
    python -m pytest tests/test_bakeoff_wave1.py
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from routers import banking as BK
from routers import recurring_invoices as RI
from routers import bills as BL
from routers import collections as C

TID = "tenant-1"
OWNER = {"id": "u1", "email": "owner@shop.test", "tenant_id": TID, "role": "owner"}


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- fakes

def _matches(doc, filt):
    for key, cond in (filt or {}).items():
        if isinstance(cond, dict):
            for op, val in cond.items():
                dv = doc.get(key)
                if op == "$in" and dv not in val:
                    return False
                if op == "$nin" and dv in val:
                    return False
                if op == "$ne" and dv == val:
                    return False
                if op == "$lt" and not (dv is not None and dv < val):
                    return False
                if op == "$lte" and not (dv is not None and dv <= val):
                    return False
                if op == "$gt" and not (dv is not None and dv > val):
                    return False
                if op == "$gte" and not (dv is not None and dv >= val):
                    return False
                if op == "$exists" and (key in doc) != bool(val):
                    return False
        elif doc.get(key) != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec):
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
                return SimpleNamespace(modified_count=1)
        if upsert:
            d = dict(filt or {})
            d.update(update.get("$set") or {})
            self.docs.append(d)
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def delete_one(self, filt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, filt)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    async def count_documents(self, filt=None):
        return len([d for d in self.docs if _matches(d, filt)])

    async def find_one_and_update(self, filt, update, upsert=False,
                                  return_document=False, projection=None):
        for d in self.docs:
            if _matches(d, filt):
                for k, v in (update.get("$inc") or {}).items():
                    d[k] = d.get(k, 0) + v
                return dict(d)
        if upsert:
            d = dict(filt or {})
            for k, v in (update.get("$inc") or {}).items():
                d[k] = v
            self.docs.append(d)
            return dict(d)
        return None


# ---------------------------------------------------------------- rules v2: pure matcher

def test_rule_description_ops():
    txn = {"description": "HOME DEPOT #441 SUPPLIES", "amount_cents": -12500,
           "bank_account_id": "a1", "category_code": None, "tags": []}
    assert BK._eval_condition({"field": "description", "op": "contains", "value": "home depot"}, txn)
    assert BK._eval_condition({"field": "description", "op": "starts_with", "value": "home"}, txn)
    assert not BK._eval_condition({"field": "description", "op": "exact", "value": "home depot"}, txn)
    assert BK._eval_condition({"field": "description", "op": "contains", "value": "zzz",
                               "negate": True}, txn)


def test_rule_amount_band_and_modes():
    rule = {"active": True, "match_mode": "all", "conditions": [
        {"field": "description", "op": "contains", "value": "depot"},
        {"field": "amount", "op": "between", "value": [100, 200]},
    ]}
    txn = {"description": "HOME DEPOT", "amount_cents": -15000,
           "bank_account_id": "a1", "category_code": None, "tags": []}
    assert BK.rule_matches(rule, txn)
    txn2 = dict(txn, amount_cents=-5000)
    assert not BK.rule_matches(rule, txn2)
    rule_any = dict(rule, match_mode="any")
    assert BK.rule_matches(rule_any, txn2)          # description alone suffices
    assert not BK.rule_matches(dict(rule, active=False), txn)
    assert not BK.rule_matches({"active": True, "conditions": []}, txn)


def test_rule_category_and_tag_conditions():
    txn = {"description": "X", "amount_cents": 100, "bank_account_id": "a",
           "category_code": None, "tags": ["job-7"]}
    assert BK._eval_condition({"field": "category", "op": "is_unset"}, txn)
    assert BK._eval_condition({"field": "tag", "op": "has", "value": "job-7"}, txn)
    assert not BK._eval_condition({"field": "tag", "op": "has", "value": "job-9"}, txn)


# ---------------------------------------------------------------- rules v2: insert path applies actions

def test_insert_txn_applies_v2_rule_actions(monkeypatch):
    async def run():
        fake = SimpleNamespace(
            bank_rules=FakeCollection([
                {"id": "r1", "tenant_id": TID, "version": 2, "name": "Depot materials",
                 "priority": 10, "match_mode": "all", "active": True, "stop_processing": True,
                 "conditions": [{"field": "description", "op": "contains", "value": "depot"}],
                 "actions": {"category_code": "5700", "vendor": "Home Depot", "tags": ["materials"]},
                 "created_at": "2026-01-01"},
            ]),
            bank_transactions=FakeCollection(),
        )
        monkeypatch.setattr(BK, "db", fake)
        doc = await BK._insert_txn(OWNER, "a1", "2026-10-01", "HOME DEPOT RUN", -42.50, "b1")
        assert doc is not None
        assert doc["category_code"] == "5700"
        assert doc["vendor"] == "Home Depot"
        assert doc["tags"] == ["materials"]
        assert doc["rule_ids"] == ["r1"]
        # duplicate guard still intact
        dup = await BK._insert_txn(OWNER, "a1", "2026-10-01", "HOME DEPOT RUN", -42.50, "b1")
        assert dup is None
    _run(run())


def test_v2_rule_validation_rejects_bad_field(monkeypatch):
    async def run():
        monkeypatch.setattr(BK, "db", SimpleNamespace(bank_rules=FakeCollection()))
        body = BK.RuleV2Body(name="bad", conditions=[{"field": "nope", "op": "exact", "value": 1}])
        with pytest.raises(HTTPException) as exc:
            await BK.create_rule_v2(body, OWNER)
        assert exc.value.status_code == 422
    _run(run())


# ---------------------------------------------------------------- recurring: advance + end rules (pure)

def test_advance_frequencies():
    assert RI._advance("2026-01-31", "monthly") == "2026-02-28"   # clamped
    assert RI._advance("2026-01-15", "weekly") == "2026-01-22"
    assert RI._advance("2026-01-15", "biweekly") == "2026-01-29"
    assert RI._advance("2026-01-15", "quarterly") == "2026-04-15"
    assert RI._advance("2026-01-15", "annual") == "2027-01-15"
    assert RI._advance("2026-01-15", "daily") == "2026-01-16"
    assert RI._advance("2026-01-15", "custom", 45) == "2026-03-01"


def test_template_finished_rules():
    assert RI.template_finished({"end_type": "after_count", "end_count": 3}, 3, "2026-01-01")
    assert not RI.template_finished({"end_type": "after_count", "end_count": 3}, 2, "2026-01-01")
    assert RI.template_finished({"end_type": "on_date", "end_date": "2026-06-01"}, 0, "2026-06-02")
    assert not RI.template_finished({"end_type": "never"}, 99, "2030-01-01")
    assert BL._bill_template_finished({"end_type": "after_count", "end_count": 1}, 1, "2026-01-01")


def test_recurring_run_honors_end_count_and_pause(monkeypatch):
    async def run():
        templates = FakeCollection([
            {"id": "t1", "tenant_id": TID, "name": "Plan A", "client_name": "Smith",
             "lines": [{"kind": "service", "name": "Svc", "revenue_code": "4020", "amount": 50.0}],
             "tax": 0, "frequency": "weekly", "next_run": "2026-10-01", "active": True,
             "end_type": "after_count", "end_count": 2, "runs_made": 0},
            {"id": "t2", "tenant_id": TID, "name": "Paused", "client_name": "Jones",
             "lines": [{"kind": "service", "name": "Svc", "revenue_code": "4020", "amount": 10.0}],
             "tax": 0, "frequency": "weekly", "next_run": "2026-10-01", "active": True,
             "paused": True, "runs_made": 0},
        ])
        fake = SimpleNamespace(recurring_invoices=templates,
                               customers=FakeCollection(), invoices=FakeCollection())
        monkeypatch.setattr(RI, "db", fake)
        monkeypatch.setattr(RI, "_audit", AsyncMock())
        issued = []

        async def fake_issue(body, user):
            issued.append(body)
            return {"id": f"inv-{len(issued)}", "total_cents": 5000,
                    "due_date": body.due_date}
        monkeypatch.setattr(RI, "issue_invoice", fake_issue)
        res = await RI.run_due_for_user(OWNER)
        assert res["generated"] == 2                     # end_count = 2, catch-up capped run
        t1 = templates.docs[0]
        assert t1["runs_made"] == 2
        assert t1["active"] is False and t1["status"] == "completed"
        t2 = templates.docs[1]
        assert t2["runs_made"] == 0 and t2["active"] is True   # paused untouched
    _run(run())


# ---------------------------------------------------------------- collections extensions (pure + config)

def test_fee_amount_flat_wins_and_percent_clamps():
    cfg = {"late_fee_percent": 1.5, "late_fee_min_cents": 2500,
           "late_fee_max_cents": 25000, "late_fee_flat_cents": 0}
    assert C.fee_amount(cfg, 100_00) == 2500          # 1.5% = $1.50 -> min $25
    assert C.fee_amount(cfg, 100_000_00) == 25000     # capped
    assert C.fee_amount(dict(cfg, late_fee_flat_cents=4000), 100_00) == 4000


def test_due_steps_repeat_final():
    cfg = {"steps": [{"days": 3, "action": "nudge", "template": ""},
                     {"days": 21, "action": "final", "template": ""}],
           "late_fee_days": 21, "repeat_final_days": 14}
    done = ["nudge", "final"]
    # day 30: no repeat yet (first repeat at 21+14=35)
    assert C._due_steps(cfg, 30, done) == []
    due = C._due_steps(cfg, 36, done)
    assert [d.get("stage") for d in due] == ["final_r1"]
    # after r1 recorded, next repeat at 49
    assert C._due_steps(cfg, 40, done + ["final_r1"]) == []
    due2 = C._due_steps(cfg, 50, done + ["final_r1"])
    assert [d.get("stage") for d in due2] == ["final_r2"]
    # repeat off by default
    cfg_off = dict(cfg, repeat_final_days=0)
    assert C._due_steps(cfg_off, 90, done) == []


def test_bill_reminders_fire_once_per_offset(monkeypatch):
    async def run():
        bills = FakeCollection([
            {"id": "b1", "tenant_id": TID, "vendor": "Supply Co", "amount_cents": 12000,
             "paid_cents": 0, "status": "open", "due_date": "2026-11-06"},  # 7 days after 2026-10-30
            {"id": "b2", "tenant_id": TID, "vendor": "Far Co", "amount_cents": 5000,
             "paid_cents": 0, "status": "open", "due_date": "2026-12-25"},
        ])
        reminders = FakeCollection()
        fake = SimpleNamespace(bills=bills, payment_reminders=reminders, vendors=FakeCollection())
        monkeypatch.setattr(C, "db", fake)
        monkeypatch.setattr(C, "audit", AsyncMock())
        import routers.collections as CC
        monkeypatch.setattr(CC, "today_str", lambda: "2026-10-30")
        cfg = dict(C.DEFAULT_CONFIG, bill_reminders_enabled=True, bill_reminder_days=[7, 1])
        r1 = await CC.run_bill_reminders(OWNER, cfg)
        assert r1["sent"] == 1
        r2 = await CC.run_bill_reminders(OWNER, cfg)
        assert r2["sent"] == 0                       # idempotent
        assert reminders.docs[0]["kind"] == "bill"
        assert "Supply Co" in reminders.docs[0]["message"]
    _run(run())
