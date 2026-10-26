"""Referral read-back / preference scoping (security audit
2026-10-26, N-3).

Run from the backend directory with:
    python -m pytest tests/test_referral_scoping.py

A platform credential may resolve a member by email ONLY when that
person holds an account/attribution on the credential's own product.
Before the fix, the email fallback was global: any product's
credential could read any person's empire summary by email and flip
any person's payout preference (and the preference path even CREATED
a person for an unknown email). Money could never be redirected —
Stripe pays the member's own account — so the exposure was earnings
enumeration plus a $2-fee nuisance flip; both are pinned shut here.

Reuses tests/test_referral_host.py's fake-db harness.
"""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_referral_scoping")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from lib import referrals as RL  # noqa: E402
from routers import referrals as RR  # noqa: E402
from test_referral_host import _actor, _wire, run  # noqa: E402

TC = _actor("trade_circle")
BOOKS = _actor("books")


async def _setup(fdb):
    """Two people, each a member of exactly one product."""
    tc_res = await RR.ingest_account(
        RR.AccountIn(account_id="tc_1", email="tc@example.com",
                     standing={"paying": True}), TC)
    bk_res = await RR.ingest_account(
        RR.AccountIn(account_id="bk_1", email="books@example.com",
                     standing={"paying": True}), BOOKS)
    tc_person = await RL.person_by_id(tc_res["person_id"])
    bk_person = await RL.person_by_id(bk_res["person_id"])
    assert tc_person["id"] != bk_person["id"]
    return tc_person, bk_person


def test_readback_by_email_in_scope_works(monkeypatch):
    fdb = _wire(monkeypatch)

    async def go():
        tc_person, _ = await _setup(fdb)
        summary = await RR.readback_summary(None, "tc@example.com", TC)
        assert summary["empire_id"] == tc_person["id"]
    run(go())


def test_readback_by_email_cross_product_is_404(monkeypatch):
    fdb = _wire(monkeypatch)

    async def go():
        await _setup(fdb)
        with pytest.raises(HTTPException) as e:
            await RR.readback_summary(None, "books@example.com", TC)
        assert e.value.status_code == 404
    run(go())


def test_readback_by_account_id_stays_scoped(monkeypatch):
    fdb = _wire(monkeypatch)

    async def go():
        tc_person, _ = await _setup(fdb)
        summary = await RR.readback_summary("tc_1", None, TC)
        assert summary["empire_id"] == tc_person["id"]
        with pytest.raises(HTTPException) as e:
            await RR.readback_summary("tc_1", None, BOOKS)
        assert e.value.status_code == 404
    run(go())


def test_readback_unknown_email_is_404(monkeypatch):
    fdb = _wire(monkeypatch)

    async def go():
        await _setup(fdb)
        with pytest.raises(HTTPException) as e:
            await RR.readback_summary(None, "ghost@example.com", TC)
        assert e.value.status_code == 404
    run(go())


def test_preference_by_email_cross_product_refused_and_unchanged(
        monkeypatch):
    fdb = _wire(monkeypatch)

    async def go():
        _, bk_person = await _setup(fdb)
        with pytest.raises(HTTPException) as e:
            await RR.set_preference(
                RR.PreferenceIn(email="books@example.com",
                                preference="paper_check",
                                fee_acknowledged=True), TC)
        assert e.value.status_code == 404
        after = await RL.person_by_id(bk_person["id"])
        assert after["payout_preference"] == "stripe"  # not flipped
    run(go())


def test_preference_by_email_in_scope_works(monkeypatch):
    fdb = _wire(monkeypatch)

    async def go():
        tc_person, _ = await _setup(fdb)
        out = await RR.set_preference(
            RR.PreferenceIn(email="tc@example.com",
                            preference="bank_transfer"), TC)
        assert out["payout_preference"] == "bank_transfer"
        after = await RL.person_by_id(tc_person["id"])
        assert after["payout_preference"] == "bank_transfer"
    run(go())


def test_preference_unknown_email_creates_nobody(monkeypatch):
    fdb = _wire(monkeypatch)

    async def go():
        await _setup(fdb)
        before = await fdb.empire_persons.count_documents({})
        with pytest.raises(HTTPException) as e:
            await RR.set_preference(
                RR.PreferenceIn(email="ghost@example.com",
                                preference="stripe"), TC)
        assert e.value.status_code == 404
        assert await fdb.empire_persons.count_documents({}) == before
        assert await RL.person_by_email("ghost@example.com") is None
    run(go())


def test_preference_by_account_id_cross_product_is_404(monkeypatch):
    fdb = _wire(monkeypatch)

    async def go():
        await _setup(fdb)
        with pytest.raises(HTTPException) as e:
            await RR.set_preference(
                RR.PreferenceIn(account_id="tc_1",
                                preference="bank_transfer"), BOOKS)
        assert e.value.status_code == 404
    run(go())
