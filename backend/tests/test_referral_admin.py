"""Empire referral ledger host — B3 (admin + payouts), B3a (Melio
pay-file export), B3b (Melio queue: bank transfer + paper check).

Run from the backend directory with:
    python -m pytest tests/test_referral_admin.py

Same fake-db approach as tests/test_referral_host.py (whose helpers
are imported here); Stripe is a fake module in sys.modules, as in
tests/test_books_checkout.py. No Melio API exists to fake — the
queue is operator-mirrored state by design.
"""
import asyncio
import csv
import io
import os
import sys
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_referral_admin")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from lib import referrals as RL  # noqa: E402
from routers import referrals as RR  # noqa: E402
from test_referral_host import OWNER, _actor, _iso_days_ago, _wire, run  # noqa: E402


async def _mk_referrer(fdb, tag, pref="stripe", product="trade_circle"):
    """A referrer attested as paying (good standing), with a payout
    preference; returns (person, code)."""
    actor = _actor(product)
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


async def _mk_pot(fdb, code, tag, product="trade_circle", members=4,
                  amount=900):
    """`members` paying referrals on one site, all cleared. Two rounds
    of payments: round 1 (70 days ago) qualifies the referrer at the
    4th referral (only that payment earns, month 1); round 2 (65 days
    ago) earns month-1 25% for every member. Pot for the default
    4 members x 900 = 225 + 4 x 225 = 1800."""
    actor = _actor(product)
    for i in range(members):
        acct = f"{tag}_m{i}"
        await RR.ingest_account(
            RR.AccountIn(account_id=acct, email=f"{tag}m{i}@example.com",
                         referral_code=code), actor)
        await RR.ingest_payment(
            RR.PaymentIn(account_id=acct, payment_id=f"{tag}_pay{i}",
                         amount_cents=amount,
                         paid_at=_iso_days_ago(70)), actor)
    for i in range(members):
        acct = f"{tag}_m{i}"
        await RR.ingest_payment(
            RR.PaymentIn(account_id=acct, payment_id=f"{tag}_pay{i}b",
                         amount_cents=amount,
                         paid_at=_iso_days_ago(65)), actor)


def _fake_stripe(monkeypatch):
    calls = []
    fake = types.ModuleType("stripe")

    def _create(**kw):
        calls.append(kw)
        return types.SimpleNamespace(id=f"tr_{len(calls)}",
                                     amount=kw.get("amount"))
    fake.Transfer = types.SimpleNamespace(create=_create)
    fake.api_key = None
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    return calls


# ---------------- B3: manual rail + balances ----------------
def test_manual_payout_records_and_locked_is_refused(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        locked, lcode = await _mk_referrer(fdb, "lk")
        await _mk_pot(fdb, lcode, "lk", members=1)
        with pytest.raises(HTTPException) as e:
            await RR.manual_payout(
                RR.ManualPayoutIn(referrer_id=locked["id"],
                                  product="trade_circle",
                                  note="Zelle"), OWNER)
        assert e.value.status_code == 409
        assert "qualifies" in e.value.detail

        person, code = await _mk_referrer(fdb, "ok")
        await _mk_pot(fdb, code, "ok")
        rows = await RR.admin_balances(OWNER)
        row = next(r for r in rows if r["empire_id"] == person["id"])
        assert row["sites"]["trade_circle"]["payable"] == 1800
        out = await RR.manual_payout(
            RR.ManualPayoutIn(referrer_id=person["id"],
                              product="trade_circle", note="Zelle 10/24"),
            OWNER)
        assert out["cents"] == 1800 and out["rail"] == "manual"
        s = await RL.site_summary(person["id"], "trade_circle")
        assert s["paid"] == 1800 and s["payable"] == 0
        with pytest.raises(HTTPException):
            await RR.manual_payout(
                RR.ManualPayoutIn(referrer_id=person["id"],
                                  product="trade_circle", note="again"),
                OWNER)
    run(_run())


# ---------------- B3: Stripe batch ----------------
def test_stripe_batch_refuses_honestly_when_unconfigured(monkeypatch):
    async def _run():
        _wire(monkeypatch)
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        with pytest.raises(HTTPException) as e:
            await RR.stripe_batch(RR.BatchIn(period="2026-10"), OWNER)
        assert e.value.status_code == 503
    run(_run())


def test_stripe_batch_pays_skips_and_never_twice(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        calls = _fake_stripe(monkeypatch)
        # A: stripe pref + payable Connect account via a books tenant.
        a, acode = await _mk_referrer(fdb, "aa")
        await _mk_pot(fdb, acode, "aa")
        await RR.ingest_account(
            RR.AccountIn(account_id="tA", tenant_id="tA",
                         email="aa@example.com"), _actor("books"))
        fdb.tenants.docs.append({
            "id": "tA", "stripe_connect": {
                "account_id": "acct_A", "charges_enabled": True,
                "payouts_enabled": True, "details_submitted": True}})
        # B: stripe pref, no books account -> skipped, listed.
        b, bcode = await _mk_referrer(fdb, "bb")
        await _mk_pot(fdb, bcode, "bb")
        # C: locked (1 paying referral) -> skipped with the reason.
        c, ccode = await _mk_referrer(fdb, "cc")
        await _mk_pot(fdb, ccode, "cc", members=1)
        # D: Melio preference -> skipped to the queue.
        d, dcode = await _mk_referrer(fdb, "dd", pref="bank_transfer")
        await _mk_pot(fdb, dcode, "dd")

        out = await RR.stripe_batch(RR.BatchIn(period="2026-10"), OWNER)
        assert len(out["paid"]) == 1
        assert out["paid"][0]["referrer_id"] == a["id"]
        assert out["paid"][0]["cents"] == 1800
        reasons = {(s["referrer_id"], s["product"]): s["reason"]
                   for s in out["skipped"]}
        assert "no payable Stripe account" in reasons[(b["id"], "trade_circle")]
        # c is not qualified and has no earnings yet (pre-qualification
        # entries carry zero share), so the batch passes over c
        # silently — there is no pot to skip or pay.
        assert (c["id"], "trade_circle") not in reasons
        assert all(p["referrer_id"] != c["id"] for p in out["paid"])
        assert "Melio queue" in reasons[(d["id"], "trade_circle")]
        assert len(calls) == 1
        assert calls[0]["destination"] == "acct_A"
        assert calls[0]["amount"] == 1800
        assert "2026-10" in calls[0]["idempotency_key"]

        out2 = await RR.stripe_batch(RR.BatchIn(period="2026-10"), OWNER)
        assert out2["paid"] == [] and len(calls) == 1  # never twice
        s = await RL.site_summary(a["id"], "trade_circle")
        assert s["paid"] == 1800
    run(_run())


# ---------------- B3b: preference + fee disclosure ----------------
def test_preference_disclosure_and_fee_math_fields(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        actor = _actor()
        opts = await RR.payout_options(actor)
        by_id = {p["id"]: p for p in opts["preferences"]}
        assert by_id["paper_check"]["fee_cents"] == 200
        assert "$2.00" in by_id["paper_check"]["note"]
        assert by_id["bank_transfer"]["rail"] == "melio_ach"
        assert by_id["stripe"]["fee_cents"] == 0
        person, _ = await _mk_referrer(fdb, "pf")
        with pytest.raises(HTTPException) as e:
            await RR.set_preference(
                RR.PreferenceIn(account_id="own_pf",
                                preference="paper_check"), actor)
        assert e.value.status_code == 422
        assert "$2.00" in e.value.detail
        out = await RR.set_preference(
            RR.PreferenceIn(account_id="own_pf", preference="paper_check",
                            fee_acknowledged=True), actor)
        assert out["payout_preference"] == "paper_check"
        assert out["paper_check_fee_cents"] == 200
    run(_run())


# ---------------- B3a: Melio pay-file export + batch done ----------------
def _rows(resp):
    text = resp.body.decode()
    return list(csv.DictReader(io.StringIO(text)))


def test_melio_export_mixed_batch_and_bulk_record(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        d, dcode = await _mk_referrer(fdb, "dx", pref="bank_transfer")
        await _mk_pot(fdb, dcode, "dx", product="books")
        e, ecode = await _mk_referrer(fdb, "ex", pref="paper_check")
        await _mk_pot(fdb, ecode, "ex", product="trade_circle")
        resp = await RR.export_batch(
            RR.ExportIn(period="2026-10", items=[
                RR.ExportItemIn(referrer_id=d["id"], product="books"),
                RR.ExportItemIn(referrer_id=e["id"],
                                product="trade_circle")]), OWNER)
        batch_id = resp.headers["X-Pay-Batch-Id"]
        rows = {r["referrer_id"]: r for r in _rows(resp)}
        ach = rows[d["id"]]
        assert ach["method"] == "ach" and ach["fee_cents"] == "0"
        assert ach["amount_cents"] == "1800" and ach["product"] == "books"
        chk = rows[e["id"]]
        assert chk["method"] == "check" and chk["fee_cents"] == "200"
        assert chk["gross_cents"] == "1800" and chk["amount_cents"] == "1600"

        done = await RR.batch_done(batch_id, RR.BatchDoneIn(), OWNER)
        assert len(done["recorded"]) == 2 and done["batch_status"] == "done"
        payouts = {p["referrer_id"]: p for p in fdb.referral_payouts.docs}
        assert payouts[d["id"]]["rail"] == "melio_ach"
        assert payouts[d["id"]]["net_cents"] == 1800
        assert payouts[e["id"]]["rail"] == "paper_check_melio"
        assert payouts[e["id"]]["gross_cents"] == 1800
        assert payouts[e["id"]]["fee_cents"] == 200
        assert payouts[e["id"]]["net_cents"] == 1600  # gross - fee = net

        # Member read-back shows the deduction plainly.
        summary = await RL.empire_summary(
            await RL.person_by_id(e["id"]))
        assert summary["payouts"][0]["fee_cents"] == 200
        assert summary["payouts"][0]["net_cents"] == 1600
        assert summary["paper_check_fee_cents"] == 200

        done2 = await RR.batch_done(batch_id, RR.BatchDoneIn(), OWNER)
        assert done2["recorded"] == []
        assert len(done2["already_recorded"]) == 2
        assert len(fdb.referral_payouts.docs) == 2  # never re-paid
    run(_run())


def test_export_refuses_a_locked_selection_wholesale(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        f, fcode = await _mk_referrer(fdb, "fx", pref="bank_transfer")
        await _mk_pot(fdb, fcode, "fx", members=1)  # locked
        with pytest.raises(HTTPException) as e:
            await RR.export_batch(
                RR.ExportIn(period="2026-10", items=[
                    RR.ExportItemIn(referrer_id=f["id"],
                                    product="trade_circle")]), OWNER)
        assert e.value.status_code == 409
        assert fdb.referral_pay_batches.docs == []  # nothing staged
    run(_run())


# ---------------- B3b: queue lifecycle, both rails ----------------
def test_queue_lifecycle_ach_unclaimed_stays_visible(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        g, gcode = await _mk_referrer(fdb, "gx", pref="bank_transfer")
        await _mk_pot(fdb, gcode, "gx")
        item = await RR.queue_create(
            RR.QueueIn(referrer_id=g["id"], product="trade_circle"), OWNER)
        assert item["status"] == "queued" and item["rail"] == "melio_ach"
        assert item["fee_cents"] == 0 and item["net_cents"] == 1800
        item = await RR.queue_advance(
            item["id"], RR.QueueAdvanceIn(status="sent_to_melio"), OWNER)
        item = await RR.queue_advance(
            item["id"], RR.QueueAdvanceIn(status="awaiting_claim"), OWNER)
        # Unclaimed: still visible in the queue, NO payout recorded —
        # the money has not moved yet.
        rows = await RR.queue_list(None, None, OWNER)
        assert rows[0]["status"] == "awaiting_claim"
        assert fdb.referral_payouts.docs == []
        item = await RR.queue_advance(
            item["id"], RR.QueueAdvanceIn(status="claimed"), OWNER)
        item = await RR.queue_advance(
            item["id"], RR.QueueAdvanceIn(status="completed"), OWNER)
        assert item["status"] == "completed" and item["payout_id"]
        payout = fdb.referral_payouts.docs[0]
        assert payout["rail"] == "melio_ach" and payout["net_cents"] == 1800
        with pytest.raises(HTTPException):
            await RR.queue_advance(
                item["id"], RR.QueueAdvanceIn(status="failed"), OWNER)
    run(_run())


def test_queue_lifecycle_paper_check_records_with_fee(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        h, hcode = await _mk_referrer(fdb, "hx", pref="paper_check")
        await _mk_pot(fdb, hcode, "hx")
        item = await RR.queue_create(
            RR.QueueIn(referrer_id=h["id"], product="trade_circle"), OWNER)
        # The snapshot itself shows gross - fee = net before it moves.
        assert (item["gross_cents"], item["fee_cents"],
                item["net_cents"]) == (1800, 200, 1600)
        assert item["rail"] == "paper_check_melio"
        await RR.queue_advance(
            item["id"], RR.QueueAdvanceIn(status="sent_to_melio"), OWNER)
        item = await RR.queue_advance(
            item["id"], RR.QueueAdvanceIn(status="completed",
                                          reference="melio-chk-1"), OWNER)
        assert item["status"] == "completed"
        payout = fdb.referral_payouts.docs[0]
        assert payout["rail"] == "paper_check_melio"
        assert payout["reference"] == "melio-chk-1"
        assert payout["gross_cents"] - payout["fee_cents"] \
            == payout["net_cents"] == 1600
    run(_run())


def test_queue_refusals_stripe_pref_locked_and_duplicate(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        s, scode = await _mk_referrer(fdb, "sx")  # stripe preference
        await _mk_pot(fdb, scode, "sx")
        with pytest.raises(HTTPException) as e:
            await RR.queue_create(
                RR.QueueIn(referrer_id=s["id"], product="trade_circle"),
                OWNER)
        assert e.value.status_code == 409
        lk, lkcode = await _mk_referrer(fdb, "lx", pref="bank_transfer")
        await _mk_pot(fdb, lkcode, "lx", members=2)  # locked
        with pytest.raises(HTTPException):
            await RR.queue_create(
                RR.QueueIn(referrer_id=lk["id"], product="trade_circle"),
                OWNER)
        ok, okcode = await _mk_referrer(fdb, "ox", pref="bank_transfer")
        await _mk_pot(fdb, okcode, "ox")
        await RR.queue_create(
            RR.QueueIn(referrer_id=ok["id"], product="trade_circle"), OWNER)
        with pytest.raises(HTTPException):
            await RR.queue_create(
                RR.QueueIn(referrer_id=ok["id"], product="trade_circle"),
                OWNER)
    run(_run())
