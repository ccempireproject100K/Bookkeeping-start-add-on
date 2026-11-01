"""Admin balances pagination (referral perf fix).

The endpoint used to return every referrer in one response built
from per-referrer ledger scans. It now paginates with limit/offset
over the global sort (payable desc, then email). This test pins the
slicing contract: pages are disjoint, concatenate to the full sorted
list, and the global order is computed BEFORE slicing (a payable
referrer outranks the email sort even on a later page boundary).
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_referral_balances_pagination")

from lib import referrals as RL  # noqa: E402
from routers import referrals as RR  # noqa: E402
from test_referral_host import OWNER, _wire, run  # noqa: E402


async def _referrer(fdb, label):
    person = await RL.ensure_person(f"{label}@page.example")
    person = await RL.link_account(
        person, product="trade_circle", account_id=f"own-{label}",
        email=f"{label}@page.example", standing={"paying": True})
    return await RL.person_by_id(person["id"])


async def _build(fdb):
    now = datetime.now(timezone.utc)
    for i in range(7):
        p = await _referrer(fdb, f"pg{i}")
        await RL.apply_attribution(
            product="books", account_id=f"pg{i}-bk",
            email=f"pg{i}-bk@page.example", referral_code=p["code"])
    # One payable referrer whose email sorts LAST: 4 cleared paying
    # referrals on trade_circle qualify the pot (only the 4th, the
    # qualifying payment, earns 25%; the first three never convert).
    top = await _referrer(fdb, "zz_top")
    for j in range(4):
        acct = f"zz-{j}"
        await RL.apply_attribution(
            product="trade_circle", account_id=acct,
            email=f"{acct}@page.example", referral_code=top["code"])
        await RL.apply_payment(
            product="trade_circle", account_id=acct,
            payment_id=f"pay-{acct}", amount_cents=900,
            paid_at=(now - timedelta(days=90)).isoformat())


def test_balances_pagination(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        await _build(fdb)
        full = await RR.admin_balances(OWNER)
        assert len(full) == 8
        assert full[0]["email"] == "zz_top@page.example"
        assert full[0]["payable"] == 225  # 1 x round(900 * 0.25)
        assert [r["email"] for r in full[1:]] == sorted(
            r["email"] for r in full[1:])
        pages = []
        for offset in (0, 3, 6):
            pages += await RR.admin_balances(OWNER, limit=3,
                                             offset=offset)
        assert [r["email"] for r in pages] == \
            [r["email"] for r in full]
        first = await RR.admin_balances(OWNER, limit=1, offset=0)
        assert [r["email"] for r in first] == ["zz_top@page.example"]
        assert await RR.admin_balances(OWNER, limit=3, offset=99) == []

    run(_run())
