"""Bill-pay PREP (Top-20 #14): queue, VA approval workflow, scheduling,
reminders, ready-to-pay packet, mark-paid recording, partner seam.

Direct-call tests: no mongod, no network. Router endpoints are awaited
directly with a fake user dict and an in-memory FakeDB monkeypatched into
both router modules (harness shared with the sibling lanes, extended with
insert_many + a minimal $match/$group aggregate for ledger balances).
"""
import asyncio
import copy
import os
import sys
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from routers import bills as bills_router
from routers import billpay as bp_router

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Olivia Owner",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}
TODAY = date.today()
D = lambda days: (TODAY + timedelta(days=days)).isoformat()  # noqa: E731


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


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        items = [spec] if isinstance(spec, tuple) else list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: (d.get(field) is None, d.get(field)),
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
        ids = []
        for d in docs:
            r = await self.insert_one(d)
            ids.append(r.inserted_id)
        return SimpleNamespace(inserted_ids=ids)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))

    def aggregate(self, pipeline):  # motor returns a cursor synchronously
        docs = [copy.deepcopy(d) for d in self.docs]
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
                    for out_f, agg in spec.items():
                        if out_f == "_id":
                            continue
                        src = agg["$sum"].lstrip("$")
                        g[out_f] = g.get(out_f, 0) + (d.get(src) or 0)
                docs = list(groups.values())
            else:
                raise NotImplementedError(f"FakeDB aggregate stage {stage}")
        return FakeCursor(docs)

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
@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(bills_router, "db", fake)
    monkeypatch.setattr(bp_router, "db", fake)
    for code in ("1010", "1020", "1050", "2010", "5700"):
        fake.accounts.docs.append({"code": code, "tenant_id": TID,
                                   "archived": False})
        fake.accounts.docs.append({"code": code, "tenant_id": TID2,
                                   "archived": False})
    fake.bank_accounts.docs.append(
        {"id": "ba-1", "name": "Operating", "kind": "bank", "gl_code": "1020",
         "tenant_id": TID, "created_at": "2026-01-01T00:00:00+00:00"})
    return fake


def mk_bill(env, user=USER, **kw):
    body = bills_router.BillBody(
        vendor=kw.pop("vendor", "Acme Supply"), amount=kw.pop("amount", 100.0),
        bill_date=kw.pop("bill_date", D(-10)), due_date=kw.pop("due_date", D(0)),
        **kw)
    return run(bills_router.create_bill(body, user))


def transit(env, bill_id, target, user=USER):
    return run(bp_router.transition(
        bill_id, bp_router.TransitionBody(target=target), user))


def approve_fully(env, bill_id, user=USER):
    transit(env, bill_id, "prepared", user)
    transit(env, bill_id, "reviewed", user)
    return transit(env, bill_id, "approved", user)


# ============================ queue ============================
def test_queue_buckets_totals_and_order(env):
    mk_bill(env, vendor="Due Today Co", amount=100.0, due_date=D(0))
    mk_bill(env, vendor="Week Co", amount=50.0, due_date=D(3))
    mk_bill(env, vendor="Later Co", amount=25.0, due_date=D(30))
    mk_bill(env, vendor="Overdue Co", amount=10.0, due_date=D(-5))
    q = run(bp_router.pay_queue(user=USER))
    assert q["buckets"]["due_now"]["count"] == 2
    assert q["buckets"]["due_now"]["balance_cents"] == 11000
    assert q["buckets"]["due_this_week"]["balance_cents"] == 5000
    assert q["buckets"]["upcoming"]["balance_cents"] == 2500
    assert q["total"]["balance_cents"] == 18500
    vendors = [b["vendor"] for b in q["buckets"]["due_now"]["bills"]]
    assert vendors == ["Overdue Co", "Due Today Co"]  # due-date order
    assert q["rail"]["connected"] is False
    assert "does not send money" in q["note"]


def test_queue_filters_by_vendor_and_job(env):
    mk_bill(env, vendor="Acme Supply", amount=10.0, job_id="job-1")
    mk_bill(env, vendor="Acme Supply", amount=20.0, job_id="job-2")
    mk_bill(env, vendor="Beta Parts", amount=40.0, job_id="job-1")
    q = run(bp_router.pay_queue(vendor="acme supply", user=USER))
    assert q["total"]["count"] == 2 and q["total"]["balance_cents"] == 3000
    q = run(bp_router.pay_queue(job_id="job-1", user=USER))
    assert q["total"]["count"] == 2 and q["total"]["balance_cents"] == 5000
    q = run(bp_router.pay_queue(user=USER))
    assert {v["vendor"] for v in q["by_vendor"]} == {"Acme Supply", "Beta Parts"}
    labels = {j["job_id"] for j in q["by_job"]}
    assert labels == {"job-1", "job-2"}


def test_cash_position_is_recorded_books_figures(env):
    b = mk_bill(env, amount=200.0)
    run(bp_router.mark_paid(b["id"], bp_router.MarkPaidBody(
        payment_method="bank", reference="CHK-1"), USER))
    q = run(bp_router.pay_queue(user=USER))
    cp = q["cash_position"]
    assert cp["accounts"][0]["name"] == "Operating"
    assert cp["total_cents"] == -20000  # recorded: $200 left the bank account
    assert "not a live bank feed" in cp["label"]


# ============================ approval workflow ============================
def test_state_machine_happy_path_and_trail_visible(env):
    b = mk_bill(env)
    out = transit(env, b["id"], "prepared")
    assert out["prep_state"] == "prepared"
    assert out["approval"]["prepared"]["by"] == "Olivia Owner"
    transit(env, b["id"], "reviewed")
    out = transit(env, b["id"], "approved")
    assert out["approval"]["approved"]["by"] == "Olivia Owner"
    assert out["approval"]["approved"]["at"]
    # and the trail shows on the queue row itself
    q = run(bp_router.pay_queue(user=USER))
    row = q["buckets"]["due_now"]["bills"][0]
    assert row["approval"]["approved"]["by"] == "Olivia Owner"


def test_state_machine_no_skipping_and_no_redeciding(env):
    b = mk_bill(env)
    with pytest.raises(HTTPException) as e:
        transit(env, b["id"], "approved")
    assert e.value.status_code == 409
    transit(env, b["id"], "prepared")
    with pytest.raises(HTTPException) as e:
        transit(env, b["id"], "prepared")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        transit(env, b["id"], "approved")
    assert e.value.status_code == 409  # must review first


def test_batch_transition(env):
    a = mk_bill(env, vendor="A")
    b = mk_bill(env, vendor="B")
    res = run(bp_router.batch_transition(
        bp_router.BatchBody(bill_ids=[a["id"], b["id"], "missing"],
                            target="prepared"), USER))
    assert res["succeeded"] == 2 and res["failed"] == 1
    assert res["results"][2]["ok"] is False


def test_schedule_requires_approved(env):
    b = mk_bill(env)
    with pytest.raises(HTTPException) as e:
        run(bp_router.schedule(b["id"], bp_router.ScheduleBody(pay_date=D(1)), USER))
    assert e.value.status_code == 409
    approve_fully(env, b["id"])
    out = run(bp_router.schedule(b["id"], bp_router.ScheduleBody(pay_date=D(1)), USER))
    assert out["pay_date_planned"] == D(1)


# ============================ reminders ============================
def test_reminders_fire_once_and_go_quiet_when_paid(env):
    b = mk_bill(env)
    approve_fully(env, b["id"])
    run(bp_router.schedule(b["id"], bp_router.ScheduleBody(pay_date=D(0)), USER))
    r1 = run(bp_router.run_reminders(USER))
    assert r1["created"] == 1
    r2 = run(bp_router.run_reminders(USER))
    assert r2["created"] == 0  # deduped — fires exactly once
    notes = run(bp_router.list_notifications(USER))
    assert notes["unread"] == 1
    assert notes["notifications"][0]["kind"] == "bill_pay_date_due"
    # pay it off, re-run: still quiet, nothing new
    run(bp_router.mark_paid(b["id"], bp_router.MarkPaidBody(), USER))
    r3 = run(bp_router.run_reminders(USER))
    assert r3["created"] == 0


def test_discount_window_reminder(env):
    mk_bill(env, discount_percent=2.0, discount_date=D(1))
    r = run(bp_router.run_reminders(USER))
    assert r["created"] == 1
    notes = run(bp_router.list_notifications(USER))
    assert notes["notifications"][0]["kind"] == "bill_discount_closing"
    assert "save $2.00" in notes["notifications"][0]["body"]


# ============================ discount math ============================
def test_discount_math_computed_never_applied(env):
    b = mk_bill(env, amount=1000.0, discount_percent=2.0, discount_date=D(5))
    q = run(bp_router.pay_queue(user=USER))
    row = q["buckets"]["due_now"]["bills"][0]
    d = row["discount"]
    assert d["save_cents"] == 2000 and d["pay_cents"] == 98000
    assert d["window_open"] is True and d["days_left"] == 5
    # the bill itself is untouched — full balance still owed in the books
    assert row["balance_cents"] == 100000
    b2 = mk_bill(env, vendor="Late Co", discount_percent=2.0, discount_date=D(-1))
    q = run(bp_router.pay_queue(vendor="Late Co", user=USER))
    row = q["buckets"]["due_now"]["bills"][0]
    assert row["discount"]["window_open"] is False


# ============================ packet ============================
def test_packet_matches_approved_batch_exactly(env):
    a = mk_bill(env, vendor="Acme Supply", amount=100.0, bill_ref="INV-9",
                job_id="job-1")
    b = mk_bill(env, vendor="Beta Parts", amount=50.0)
    mk_bill(env, vendor="Not Approved Co", amount=999.0)
    approve_fully(env, a["id"])
    approve_fully(env, b["id"])
    run(bp_router.schedule(a["id"], bp_router.ScheduleBody(pay_date=D(2)), USER))
    resp = run(bp_router.packet(user=USER))
    text = resp.body.decode()
    lines = [ln for ln in text.strip().split("\n") if ln]
    assert lines[0] == "vendor,bill_ref,amount,due_date,pay_date,bill_id"
    assert "Acme Supply" in text and "INV-9" in text and "100.00" in text
    assert "Beta Parts" in text and "50.00" in text
    assert "Not Approved Co" not in text
    assert lines[-1].startswith("TOTAL,,150.00")
    # filter narrows the packet the same way as the queue
    resp = run(bp_router.packet(job_id="job-1", user=USER))
    text = resp.body.decode()
    assert "Acme Supply" in text and "Beta Parts" not in text
    # PDF renders
    resp = run(bp_router.packet(format="pdf", user=USER))
    assert resp.body[:4] == b"%PDF"


# ============================ mark paid ============================
def test_mark_paid_posts_balanced_je_and_refuses_double(env):
    b = mk_bill(env, amount=300.0)
    approve_fully(env, b["id"])
    res = run(bp_router.mark_paid(b["id"], bp_router.MarkPaidBody(
        payment_method="bank", reference="ACH-778", pay_date=D(0)), USER))
    assert res["recorded"] is True and res["status"] == "paid"
    lines = [ln for ln in env.journal_lines.docs
             if ln["tenant_id"] == TID and ln.get("entry_number") == res["entry_number"]]
    dr = sum(ln["debit"] for ln in lines)
    cr = sum(ln["credit"] for ln in lines)
    assert dr == cr == 30000
    assert {ln["account_code"] for ln in lines} == {"2010", "1020"}
    bill = run(bills_router.list_bills(USER))[0]
    assert bill["payments"][0]["reference"] == "ACH-778"
    assert bill["payments"][0]["method"] == "bank"
    with pytest.raises(HTTPException) as e:
        run(bp_router.mark_paid(b["id"], bp_router.MarkPaidBody(), USER))
    assert e.value.status_code == 409  # no double-recording


def test_mark_paid_partial_keeps_balance(env):
    b = mk_bill(env, amount=300.0)
    res = run(bp_router.mark_paid(b["id"], bp_router.MarkPaidBody(amount=120.0), USER))
    assert res["status"] == "partial" and res["balance_cents"] == 18000


# ============================ isolation + untouched bills ============================
def test_tenant_isolation(env):
    b = mk_bill(env)
    approve_fully(env, b["id"])
    q = run(bp_router.pay_queue(user=USER2))
    assert q["total"]["count"] == 0
    with pytest.raises(HTTPException) as e:
        transit(env, b["id"], "prepared", user=USER2)
    assert e.value.status_code == 404


def test_plain_bills_behave_as_today(env):
    b = mk_bill(env, amount=80.0)
    rows = run(bills_router.list_bills(USER))
    assert rows[0]["status"] == "open" and rows[0]["balance_cents"] == 8000
    res = run(bills_router.pay_bill(b["id"], bills_router.PayBody(
        payment_method="cash"), USER))
    assert res["status"] == "paid"
    # a paid bill never shows in the pay queue
    q = run(bp_router.pay_queue(user=USER))
    assert q["total"]["count"] == 0


def test_rail_status_honest(env):
    s = run(bp_router.rail(USER))
    assert s["connected"] is False and s["provider"] is None
    assert "does not send money" in s["message"]
