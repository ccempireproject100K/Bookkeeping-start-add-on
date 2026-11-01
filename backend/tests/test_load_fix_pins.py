"""Load-fix pins — books load test 2026-10-26, §3.2 + §5.3 (P4/P6
and the queue-response bug). Each defect pin FAILS on the pre-fix
code; the boundary guards document where behavior must NOT change.

  A. P4 — two concurrent Melio "batch done" completions recorded a
     payout TWICE (150¢ of records against a 75¢ pot). queue_complete
     now claims the item atomically (conditional update, the F-1
     pattern) and records under a deterministic batch_key, which the
     unique index on referral_payouts makes a hard backstop.
  B. P6 — a refund against an already-PAID earning rewrote its share
     to 0 with no balancing entry (permanent gap between payout
     records and member summaries). apply_refund now books a
     negative referral_adjustments row, mirroring the dispute path.
  C. POST /admin/queue and /{id}/advance performed the state change
     but returned HTTP 500: the returned item carried Mongo's
     ObjectId _id. The endpoints now strip _id, like the list one.
  D. §3.2 — concurrent first-posts into a brand-new period: 15 of
     20 took HTTP 500 on an unhandled DuplicateKeyError from
     ensure_period's find-then-insert. It now adopts the winner's
     period.

The fakes here YIELD at every DB op (asyncio.sleep(0)), modelling
real motor I/O — that is what makes the races reproducible. The
shared referral fake (tests/test_referral_host.py) never suspends,
so the races are invisible to it; it also never stamps an _id on
stored docs, which is why bug C survived the unit suite. The
referral fake below subclasses it, adding the yields and the
ObjectId stamp real Mongo applies.

Run from the backend directory:
    python -m pytest tests/test_load_fix_pins.py
"""
import asyncio
import json
import os
from copy import deepcopy

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_load_fix_pins")

import pytest  # noqa: E402
from bson import ObjectId  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

import test_referral_host as TH  # noqa: E402
from lib import ledger as LED  # noqa: E402
from lib import referrals as RL  # noqa: E402
from routers import referrals as RR  # noqa: E402


# ---------------- yielding fakes ----------------

class _YieldCursor(TH._Cursor):
    def __aiter__(self):
        async def gen():
            for d in deepcopy(self._docs):
                await asyncio.sleep(0)
                yield d
        return gen()

    async def to_list(self, length=None):
        await asyncio.sleep(0)
        return await super().to_list(length)


class _YieldCollection(TH._FakeCollection):
    """The shared referral fake + a yield at every op + the _id
    stamp real Mongo applies on insert."""

    async def find_one(self, q=None, projection=None):
        await asyncio.sleep(0)
        return await super().find_one(q, projection)

    def find(self, q=None, projection=None):
        return _YieldCursor(
            [d for d in self.docs if TH._matches(d, q)])

    async def insert_one(self, doc):
        await asyncio.sleep(0)
        doc.setdefault("_id", ObjectId())  # as motor does
        return await super().insert_one(doc)

    async def update_one(self, q, update, upsert=False):
        await asyncio.sleep(0)
        return await super().update_one(q, update, upsert=upsert)

    async def update_many(self, q, update):
        await asyncio.sleep(0)
        return await super().update_many(q, update)

    async def count_documents(self, q=None):
        await asyncio.sleep(0)
        return await super().count_documents(q)

    async def distinct(self, field, q=None):
        await asyncio.sleep(0)
        return await super().distinct(field, q)


class _YieldDB:
    def __init__(self):
        for name in ("users", "tenants", "audit_log",
                     "integration_credentials", "empire_persons",
                     "referral_events", "referral_attributions",
                     "referral_earnings", "referral_disputes",
                     "referral_adjustments", "referral_payouts",
                     "referral_payout_queue", "referral_pay_batches",
                     "referral_payment_events", "referral_free_months"):
            setattr(self, name, _YieldCollection(name))


def _wire_yielding(monkeypatch):
    fdb = _YieldDB()
    monkeypatch.setattr(RL, "db", fdb)
    monkeypatch.setattr(RR, "db", fdb)
    monkeypatch.setattr(TH.IA, "db", fdb)
    monkeypatch.setattr(TH.AU, "db", fdb)
    return fdb


# ---------------- seeding (test_referral_admin's shapes) ----------------

async def _mk_referrer(fdb, tag, pref="stripe"):
    actor = TH._actor("trade_circle")
    res = await RR.ingest_account(
        RR.AccountIn(account_id=f"own_{tag}", email=f"{tag}@example.com",
                     standing={"paying": True}), actor)
    person = await RL.person_by_id(res["person_id"])
    if pref != "stripe":
        await RR.set_preference(
            RR.PreferenceIn(account_id=f"own_{tag}", preference=pref,
                            fee_acknowledged=True), actor)
        person = await RL.person_by_id(person["id"])
    return person, person["code"]


async def _mk_pot(code, tag, members=4, amount=900,
                  product="trade_circle", rounds=2):
    """`members` paying referrals on one site, all cleared. Two rounds
    of payments by default: round 1 (70d ago) qualifies the referrer
    at the 4th member (the qualification month counts in full, so all
    round-1 payments earn 25%); round 2 (65d ago) earns 25% for every
    member. Default pot: 4 members x 2 rounds x 225c = 1800c.
    rounds=1 seeds a single round (used for post-qualification
    additions, where one new member's first payment earns 225c)."""
    actor = TH._actor(product)
    for i in range(members):
        acct = f"{tag}_m{i}"
        await RR.ingest_account(
            RR.AccountIn(account_id=acct,
                         email=f"{tag}m{i}@example.com",
                         referral_code=code), actor)
        await RR.ingest_payment(
            RR.PaymentIn(account_id=acct, payment_id=f"{tag}_pay{i}",
                         amount_cents=amount,
                         paid_at=TH._iso_days_ago(70)), actor)
    if rounds >= 2:
        for i in range(members):
            acct = f"{tag}_m{i}"
            await RR.ingest_payment(
                RR.PaymentIn(account_id=acct,
                             payment_id=f"{tag}_pay{i}b",
                             amount_cents=amount,
                             paid_at=TH._iso_days_ago(65)), actor)


async def _export_one(fdb, person, product="trade_circle"):
    resp = await RR.export_batch(
        RR.ExportIn(period="2026-10", items=[
            RR.ExportItemIn(referrer_id=person["id"],
                            product=product)]), TH.OWNER)
    return resp.headers["X-Pay-Batch-Id"]


# ---------------- Fix A: the double-record race (P4) ----------------

def test_concurrent_batch_done_records_exactly_once(monkeypatch):
    async def _run():
        fdb = _wire_yielding(monkeypatch)
        person, code = await _mk_referrer(fdb, "race", "bank_transfer")
        await _mk_pot(code, "race")  # qualified pot of 1800c (see _mk_pot)
        batch_id = await _export_one(fdb, person)

        r1, r2 = await asyncio.gather(
            RR.batch_done(batch_id, RR.BatchDoneIn(), TH.OWNER),
            RR.batch_done(batch_id, RR.BatchDoneIn(), TH.OWNER))

        payouts = fdb.referral_payouts.docs
        assert len(payouts) == 1  # P4: pre-fix this is 2
        assert payouts[0]["gross_cents"] == 1800
        assert len(r1["recorded"]) + len(r2["recorded"]) == 1
        summary = await RL.site_summary(person["id"], "trade_circle")
        assert summary["paid"] == 1800 == payouts[0]["gross_cents"]
        item = fdb.referral_payout_queue.docs[0]
        assert item["status"] == "completed"
        assert item["payout_id"] == payouts[0]["id"]
        assert "completion_claim" not in item  # claim released
    TH.run(_run())


def test_sequential_double_complete_is_a_clean_noop(monkeypatch):
    async def _run():
        fdb = _wire_yielding(monkeypatch)
        person, code = await _mk_referrer(fdb, "seq", "paper_check")
        await _mk_pot(code, "seq")
        batch_id = await _export_one(fdb, person)

        done1 = await RR.batch_done(batch_id, RR.BatchDoneIn(),
                                    TH.OWNER)
        assert len(done1["recorded"]) == 1
        done2 = await RR.batch_done(batch_id, RR.BatchDoneIn(),
                                    TH.OWNER)
        assert done2["recorded"] == []
        assert len(done2["already_recorded"]) == 1
        assert len(fdb.referral_payouts.docs) == 1

        payout = fdb.referral_payouts.docs[0]
        # Deterministic per (batch, referrer, product): a second
        # record is impossible under the unique index, not merely
        # unlikely. Pre-fix the key was a fresh random id.
        assert payout["batch_key"] == (
            f"melio-batch:{batch_id}:{person['id']}:trade_circle")
        assert payout["gross_cents"] == 1800
        assert payout["fee_cents"] == 200  # paper check
        assert payout["net_cents"] == 1600

        # A stale in-memory item driven straight at the lib refuses
        # cleanly — never a second record.
        stale = dict(fdb.referral_payout_queue.docs[0])
        stale["status"] = "sent_to_melio"
        with pytest.raises(RL.ReferralError):
            await RL.queue_complete(stale, by="op@empire.example")
        assert len(fdb.referral_payouts.docs) == 1
    TH.run(_run())


# ---------------- Fix B: refund after payout (P6) ----------------

async def _paid_pot(fdb, tag, members=4):
    """A referrer whose whole pot is already paid out (manual rail).
    4 members x 2 rounds x 900c -> 8 earnings of 225c -> one 1800c
    payout (the qualification month counts in full)."""
    person, code = await _mk_referrer(fdb, tag, "stripe")
    await _mk_pot(code, tag, members=members)
    await RL.record_manual_payout(person["id"], "trade_circle",
                                  note="first payout",
                                  by="op@empire.example")
    return person, code


def test_refund_after_payout_books_negative_adjustment(monkeypatch):
    async def _run():
        fdb = _wire_yielding(monkeypatch)
        person, code = await _paid_pot(fdb, "p6")
        assert len(fdb.referral_payouts.docs) == 1
        assert fdb.referral_payouts.docs[0]["gross_cents"] == 1800

        res = await RL.apply_refund(
            product="trade_circle", payment_id="p6_pay0",
            refunded_delta_cents=900, refunded_total_cents=900)
        assert res["share_cents"] == 0

        adjs = fdb.referral_adjustments.docs
        assert len(adjs) == 1  # P6: pre-fix there are none
        adj = adjs[0]
        assert adj["cents"] == -225 and adj["status"] == "pending"
        assert adj["referrer_id"] == person["id"]
        assert adj["product"] == "trade_circle"
        assert adj["earning_id"] == "trade_circle:p6_pay0"

        summary = await RL.site_summary(person["id"], "trade_circle")
        assert summary["paid"] == 1575  # the refunded share is gone
        assert summary["adjustments"] == -225
        # Reconciliation to the cent: payout records = what the
        # summary still calls paid + what is owed back.
        recorded = sum(p["gross_cents"]
                       for p in fdb.referral_payouts.docs)
        assert recorded == summary["paid"] - summary["adjustments"]
    TH.run(_run())


def test_refunded_share_comes_off_the_next_pot(monkeypatch):
    async def _run():
        fdb = _wire_yielding(monkeypatch)
        person, code = await _paid_pot(fdb, "nxt")
        await RL.apply_refund(
            product="trade_circle", payment_id="nxt_pay0",
            refunded_delta_cents=900, refunded_total_cents=900)

        # One more cleared referral (+225c) exactly cancels the
        # clawback: nothing is payable, and asking is refused.
        await _mk_pot(code, "nxt2", members=1, rounds=1)
        s = await RL.site_summary(person["id"], "trade_circle")
        assert s["available"] == 225 and s["adjustments"] == -225
        assert s["payable"] == 0
        with pytest.raises(RL.ReferralError):
            await RL.record_manual_payout(
                person["id"], "trade_circle", note="too soon",
                by="op@empire.example")

        # A second new referral tips the pot positive by exactly the
        # amount past the clawback, and the payout applies it.
        await _mk_pot(code, "nxt3", members=1, rounds=1)
        payout = await RL.record_manual_payout(
            person["id"], "trade_circle", note="second",
            by="op@empire.example")
        assert payout["gross_cents"] == 225  # 450 cleared - 225 owed
        adj = fdb.referral_adjustments.docs[0]
        assert adj["status"] == "applied"
        assert adj["payout_id"] == payout["id"]
    TH.run(_run())


def test_refund_of_unpaid_earning_books_no_adjustment(monkeypatch):
    """Boundary guard: a refund against a PENDING earning needs no
    adjustment — the share simply shrinks before any money moves.
    This passed pre-fix and must keep passing."""
    async def _run():
        fdb = _wire_yielding(monkeypatch)
        person, code = await _mk_referrer(fdb, "pend", "stripe")
        await _mk_pot(code, "pend", members=4)
        res = await RL.apply_refund(
            product="trade_circle", payment_id="pend_pay1",
            refunded_delta_cents=900, refunded_total_cents=900)
        assert res["share_cents"] == 0
        assert fdb.referral_adjustments.docs == []
        s = await RL.site_summary(person["id"], "trade_circle")
        assert s["available"] == 1575  # the other seven shares stand
    TH.run(_run())


# ---------------- Fix C: queue endpoints' responses ----------------

def test_queue_endpoints_return_json_clean_items(monkeypatch):
    async def _run():
        fdb = _wire_yielding(monkeypatch)
        person, code = await _mk_referrer(fdb, "jc", "bank_transfer")
        await _mk_pot(code, "jc")

        item = await RR.queue_create(
            RR.QueueIn(referrer_id=person["id"],
                       product="trade_circle"), TH.OWNER)
        # Pre-fix: the dict carries Mongo's _id (an ObjectId), which
        # FastAPI cannot serialize — HTTP 500 after success.
        assert "_id" not in item
        json.dumps(item)
        assert item["status"] == "queued"
        assert len(fdb.referral_payout_queue.docs) == 1

        # A repeat create still refuses 409 and changes nothing.
        with pytest.raises(HTTPException) as e:
            await RR.queue_create(
                RR.QueueIn(referrer_id=person["id"],
                           product="trade_circle"), TH.OWNER)
        assert e.value.status_code == 409
        assert len(fdb.referral_payout_queue.docs) == 1

        advanced = await RR.queue_advance(
            item["id"], RR.QueueAdvanceIn(status="sent_to_melio"),
            TH.OWNER)
        assert "_id" not in advanced
        json.dumps(advanced)
        assert advanced["status"] == "sent_to_melio"

        completed = await RR.queue_advance(
            item["id"], RR.QueueAdvanceIn(status="completed"),
            TH.OWNER)
        assert "_id" not in completed
        json.dumps(completed)
        assert completed["status"] == "completed"
        assert len(fdb.referral_payouts.docs) == 1
    TH.run(_run())


# ---------------- Fix D: the new-period race (§3.2) ----------------

class _LedgerCollection:
    """Yielding ledger fake. fiscal_periods enforces the production
    unique index on (tenant_id, key) — the arbiter of the race."""

    def __init__(self, name):
        self.name = name
        self.docs = []

    async def find_one(self, q=None, projection=None):
        await asyncio.sleep(0)
        for d in self.docs:
            if TH._matches(d, q):
                return deepcopy(d)
        return None

    def find(self, q=None, projection=None):
        return _YieldCursor(
            [d for d in self.docs if TH._matches(d, q)])

    async def insert_one(self, doc):
        await asyncio.sleep(0)
        if self.name == "fiscal_periods" and any(
                d.get("tenant_id") == doc.get("tenant_id")
                and d.get("key") == doc.get("key")
                for d in self.docs):
            raise DuplicateKeyError(
                "dup fiscal_periods (tenant_id, key)")
        doc.setdefault("_id", ObjectId())
        self.docs.append(deepcopy(doc))
        return TH._Result(1)

    async def insert_many(self, docs):
        await asyncio.sleep(0)
        for d in docs:
            d.setdefault("_id", ObjectId())
            self.docs.append(deepcopy(d))
        return TH._Result(len(docs))

    async def update_one(self, q, update, upsert=False):
        await asyncio.sleep(0)
        for d in self.docs:
            if TH._matches(d, q):
                self._apply(d, update)
                return TH._Result(1)
        return TH._Result(0)

    async def update_many(self, q, update):
        await asyncio.sleep(0)
        n = 0
        for d in self.docs:
            if TH._matches(d, q):
                self._apply(d, update)
                n += 1
        return TH._Result(n)

    async def count_documents(self, q=None):
        await asyncio.sleep(0)
        return sum(1 for d in self.docs if TH._matches(d, q))

    async def find_one_and_update(self, q, update, upsert=False,
                                  return_document=False):
        await asyncio.sleep(0)
        for d in self.docs:
            if TH._matches(d, q):
                self._apply(d, update)
                return deepcopy(d)
        if upsert:
            doc = {k: v for k, v in (q or {}).items()
                   if not isinstance(v, dict)}
            self._apply(doc, update)
            doc.setdefault("_id", ObjectId())
            self.docs.append(deepcopy(doc))
            return deepcopy(doc)
        return None

    @staticmethod
    def _apply(doc, update):
        for op, changes in (update or {}).items():
            if op == "$set":
                TH._apply_set(doc, deepcopy(changes))
            elif op == "$inc":
                for k, v in changes.items():
                    doc[k] = doc.get(k, 0) + v
            elif op == "$unset":
                for k in changes:
                    doc.pop(k, None)


class _LedgerDB:
    def __init__(self):
        for name in ("accounts", "journal_entries", "journal_lines",
                     "fiscal_periods", "audit_log", "counters"):
            setattr(self, name, _LedgerCollection(name))
    # No audit_chain attribute on purpose: lib.audit.append_audit
    # falls back to a plain audit_log insert by design.


def test_concurrent_first_posts_into_new_period_all_succeed():
    async def _run():
        db = _LedgerDB()
        tid = "tenant-race"
        for code in ("1010", "4000"):
            db.accounts.docs.append(
                {"id": f"acct-{code}", "tenant_id": tid,
                 "code": code, "archived": False})
        user = {"email": "op@example.com"}
        draft = {"entry_date": "2027-01-15",
                 "lines": [
                     {"account_code": "1010", "debit": 1000,
                      "credit": 0},
                     {"account_code": "4000", "debit": 0,
                      "credit": 1000}]}
        results = await asyncio.gather(
            *[LED.post_journal_entry(db, tid, user, deepcopy(draft))
              for _ in range(10)],
            return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        assert errors == []  # §3.2: pre-fix 9 of 10 raise here
        periods = [d for d in db.fiscal_periods.docs
                   if d["tenant_id"] == tid]
        assert len(periods) == 1 and periods[0]["key"] == "2027-01"
        assert len(db.journal_entries.docs) == 10
        debits = sum(ln["debit"] for ln in db.journal_lines.docs)
        credits = sum(ln["credit"] for ln in db.journal_lines.docs)
        assert debits == credits == 10_000
    asyncio.run(_run())
