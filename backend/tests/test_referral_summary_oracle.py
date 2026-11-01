"""Golden-oracle test for the referral summary read paths.

The referral law is computed from the earnings/adjustments ledger by
`site_summary`, `paying_referral_count`, `empire_summary`, and the
admin balances endpoint. This test builds one rich ledger through the
REAL write paths (attribution / payment / refund / dispute / payout),
then records what those read paths produce in
tests/fixtures/referral_summary_oracle.json.

The fixture was generated with the ORIGINAL cursor-iterating
implementation (main @ 76cf6027). Any re-implementation of the read
paths — aggregation pipelines, rollups, pagination — must reproduce
the fixture EXACTLY: every cent, every flag. If the law itself ever
changes on purpose, regenerate with ORACLE_REGEN=1 and review the
diff like a contract change.

Scenario coverage: a 250-referral whale; per-site 2-vs-3 unlock
boundaries both ways; a not-eligible referrer (zero shares that still
count toward unlock); attributions with no payments; cleared vs
mid-clearing pendings; partial and full refunds (a full refund drops
the referred member out of the paying count); a manual payout; a
refund AFTER that payout (negative adjustment) absorbed by a second
payout; disputes open (month clawed across products, frozen), won
(restored, adjustments cancelled), and lost (claw stands); three
disputes tripping the manual-review freeze; and the 183-day window
(a payment past the window earns nothing).

All payment ages are day-granularity offsets from the build time with
wide margins from the 60-day clearing boundary, so the captured
values are stable whenever the test runs.
"""
import asyncio
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_referral_oracle")

import pytest  # noqa: E402

from lib import referrals as RL  # noqa: E402
from routers import referrals as RR  # noqa: E402
from test_referral_host import OWNER, _wire, run  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "referral_summary_oracle.json")
PRODUCTS = ("trade_circle", "books", "momo")
AMOUNTS = [900, 300, 1899, 3999, 999, 15999]


async def _referrer(fdb, label, paying=True, preference="stripe"):
    person = await RL.ensure_person(f"{label}@oracle.example")
    person = await RL.link_account(
        person, product="trade_circle", account_id=f"own-{label}",
        email=f"{label}@oracle.example", standing={"paying": paying})
    if preference != "stripe":
        await fdb.empire_persons.update_one(
            {"id": person["id"]},
            {"$set": {"payout_preference": preference}})
    return await RL.person_by_id(person["id"])


async def _pay(code, product, acct, amount, when, pid=None):
    pid = pid or f"pay-{acct}"
    await RL.apply_attribution(
        product=product, account_id=acct,
        email=f"{acct}@oracle.example", referral_code=code)
    res = await RL.apply_payment(
        product=product, account_id=acct, payment_id=pid,
        amount_cents=amount, paid_at=when.isoformat())
    return res["earning_id"]


def _month_day1(now, days_ago):
    """Day 1 of the month containing (now - days_ago), as a datetime.
    With the day counts used here the date is always 65+ days old, so
    its earnings are always past the 60-day clearing boundary."""
    base = now - timedelta(days=days_ago)
    return base.replace(day=1, hour=12, minute=0, second=0, microsecond=0)


async def build_oracle_ledger(fdb):
    """Returns {label: person} for every referrer in the scenario."""
    now = datetime.now(timezone.utc)
    people = {}

    async def ref(label, **kw):
        people[label] = await _referrer(fdb, label, **kw)
        return people[label]

    # ---------------------------------------------------------- whale
    whale = await ref("whale", preference="bank_transfer")
    wcode = whale["code"]
    for i in range(250):
        acct = f"w{i}"
        if i % 10 < 2:  # 20% recent (still clearing)
            first = now - timedelta(days=10 + (i % 20))
        else:           # 80% old (mostly cleared)
            first = now - timedelta(days=70 + (i * 7) % 90)
        for k in range(1 + (i % 4)):
            await _pay(wcode, "trade_circle", acct,
                       AMOUNTS[(i + k) % len(AMOUNTS)],
                       first + timedelta(days=8 * k),
                       pid=f"pay-{acct}-{k}")
    # partial refund on a cleared whale earning; full refund on another
    await RL.apply_refund(product="trade_circle", payment_id="pay-w5-0",
                          refunded_delta_cents=700,
                          refunded_total_cents=700)
    amt6 = AMOUNTS[6 % len(AMOUNTS)]
    await RL.apply_refund(product="trade_circle", payment_id="pay-w6-0",
                          refunded_delta_cents=amt6,
                          refunded_total_cents=amt6)

    # ------------------------------------------------- unlock boundary
    ba = await ref("boundary_a", preference="paper_check")
    for j in range(3):
        await _pay(ba["code"], "trade_circle", f"ba-tc{j}", 900,
                   now - timedelta(days=90))
    for j in range(2):
        await _pay(ba["code"], "books", f"ba-bk{j}", 1899,
                   now - timedelta(days=90))
    bb = await ref("boundary_b")
    for j in range(2):
        await _pay(bb["code"], "trade_circle", f"bb-tc{j}", 300,
                   now - timedelta(days=90))
    for j in range(3):
        await _pay(bb["code"], "momo", f"bb-mo{j}", 999,
                   now - timedelta(days=90))

    # --------------------------------------------------- not eligible
    ne = await ref("notelig", paying=False)
    for j in range(6):
        await _pay(ne["code"], "trade_circle", f"ne-{j}", 900,
                   now - timedelta(days=90))

    # --------------------------------------- attributions, no payments
    ao = await ref("att_only")
    await RL.apply_attribution(product="books", account_id="ao-bk",
                               email="ao-bk@oracle.example",
                               referral_code=ao["code"])
    await RL.apply_attribution(product="momo", account_id="ao-mo",
                               email="ao-mo@oracle.example",
                               referral_code=ao["code"])

    # --------------------------------------------- the kitchen sink
    mx = await ref("mixed")
    mc = mx["code"]
    await _pay(mc, "books", "mx-a", 3999, now - timedelta(days=100))
    await _pay(mc, "books", "mx-b", 1899, now - timedelta(days=95))
    await _pay(mc, "books", "mx-h", 999, now - timedelta(days=100))
    await _pay(mc, "books", "mx-i", 999, now - timedelta(days=100))
    await RL.record_manual_payout(mx["id"], "books", note="oracle p1",
                                  by="oracle", now=now)
    # refund AFTER the payout -> negative adjustment (load probe P6)
    await RL.apply_refund(product="books", payment_id="pay-mx-a",
                          refunded_delta_cents=1000,
                          refunded_total_cents=1000)
    await _pay(mc, "books", "mx-c", 3999, now - timedelta(days=90))
    await _pay(mc, "books", "mx-d", 1899, now - timedelta(days=12))
    eid_e = await _pay(mc, "books", "mx-e", 3999,
                       now - timedelta(days=100))
    await RL.apply_refund(product="books", payment_id="pay-mx-e",
                          refunded_delta_cents=999,
                          refunded_total_cents=999)
    await _pay(mc, "books", "mx-f", 999, now - timedelta(days=100))
    await RL.apply_refund(product="books", payment_id="pay-mx-f",
                          refunded_delta_cents=999,
                          refunded_total_cents=999)  # full: drops out
    await _pay(mc, "books", "mx-g", 20000, now - timedelta(days=90))
    # second payout absorbs the negative adjustment
    await RL.record_manual_payout(mx["id"], "books", note="oracle p2",
                                  by="oracle", now=now)
    for j, amt in enumerate((15999, 15999)):
        await _pay(mc, "momo", f"mx-m{j}", amt, now - timedelta(days=80))
    await _pay(mc, "momo", "mx-m2", 999, now - timedelta(days=5))

    # ------------------------------------------- dispute left open
    do = await ref("disp_open")
    dc = do["code"]
    m_open = _month_day1(now, 100)
    await _pay(dc, "trade_circle", "do-0", 900, m_open + timedelta(days=4))
    await _pay(dc, "trade_circle", "do-1", 900, m_open + timedelta(days=9))
    await _pay(dc, "trade_circle", "do-2", 900, m_open + timedelta(days=19))
    await _pay(dc, "trade_circle", "do-6", 900, m_open + timedelta(days=24))
    await RL.record_manual_payout(do["id"], "trade_circle",
                                  note="oracle", by="oracle", now=now)
    await _pay(dc, "books", "do-3", 1899, m_open + timedelta(days=9))
    await _pay(dc, "momo", "do-4", 15999, m_open + timedelta(days=11))
    await _pay(dc, "trade_circle", "do-5", 900, now - timedelta(days=15))
    await RL.apply_dispute(product="trade_circle", dispute_id="disp-open",
                           payment_id="pay-do-1", phase="opened")

    # --------------------------------------------- dispute won
    dw = await ref("disp_won")
    wc = dw["code"]
    m_won = _month_day1(now, 110)
    await _pay(wc, "trade_circle", "dw-0", 300, m_won + timedelta(days=7))
    await _pay(wc, "trade_circle", "dw-1", 300, m_won + timedelta(days=17))
    await _pay(wc, "trade_circle", "dw-2", 300, m_won + timedelta(days=2))
    await _pay(wc, "trade_circle", "dw-6", 300, m_won + timedelta(days=22))
    await RL.record_manual_payout(dw["id"], "trade_circle",
                                  note="oracle", by="oracle", now=now)
    await _pay(wc, "books", "dw-3", 1899, now - timedelta(days=30))
    await RL.apply_dispute(product="trade_circle", dispute_id="disp-won",
                           payment_id="pay-dw-1", phase="opened")
    await RL.apply_dispute(product="trade_circle", dispute_id="disp-won",
                           payment_id="pay-dw-1", phase="closed",
                           outcome="won")

    # --------------------------------------------- dispute lost
    dl = await ref("disp_lost")
    lc = dl["code"]
    m_lost = _month_day1(now, 140)
    await _pay(lc, "trade_circle", "dl-0", 900, m_lost + timedelta(days=10))
    await _pay(lc, "trade_circle", "dl-1", 900, m_lost + timedelta(days=20))
    await _pay(lc, "momo", "dl-2", 999, now - timedelta(days=20))
    await RL.apply_dispute(product="trade_circle", dispute_id="disp-lost",
                           payment_id="pay-dl-0", phase="opened")
    await RL.apply_dispute(product="trade_circle", dispute_id="disp-lost",
                           payment_id="pay-dl-0", phase="closed",
                           outcome="lost")

    # --------------------------------- three disputes -> review freeze
    rv = await ref("review")
    rc = rv["code"]
    for n, ago in enumerate((170, 120, 95)):
        m = _month_day1(now, ago)
        await _pay(rc, "trade_circle", f"rv-{n}", 900,
                   m + timedelta(days=14))
        await RL.apply_dispute(product="trade_circle",
                               dispute_id=f"disp-rv-{n}",
                               payment_id=f"pay-rv-{n}", phase="opened")
        await RL.apply_dispute(product="trade_circle",
                               dispute_id=f"disp-rv-{n}",
                               payment_id=f"pay-rv-{n}", phase="closed",
                               outcome="won")
    assert (await RL.person_by_id(rv["id"]))["review"] is True

    # --------------------------------------- pre-qualification audit
    # Under the adopted law there is no 183-day window: payments from
    # an unqualified referrer's referrals are stored as zero-share
    # pre_qualification audit entries, whenever they land.
    wn = await ref("window")
    await _pay(wn["code"], "trade_circle", "wn-0", 300,
               now - timedelta(days=200), pid="pay-wn-old")
    late = await _pay(wn["code"], "trade_circle", "wn-0", 300,
                      now - timedelta(days=10), pid="pay-wn-late")
    assert late is not None  # audit entry exists, at zero share
    late_doc = await fdb.referral_earnings.find_one({"id": late})
    assert late_doc["status"] == "pre_qualification"
    assert late_doc["share_cents"] == 0
    await _pay(wn["code"], "trade_circle", "wn-1", 300,
               now - timedelta(days=90))

    return {label: await RL.person_by_id(p["id"])
            for label, p in people.items()}, now


async def collect_oracle_outputs(fdb):
    people, now = await build_oracle_ledger(fdb)
    label_of = {p["id"]: label for label, p in people.items()}
    out = {"site": {}, "paying": {}, "empire": {}, "balances": []}
    for label, person in people.items():
        rid = person["id"]
        out["site"][label] = {}
        out["paying"][label] = {}
        for product in PRODUCTS:
            site = await RL.site_summary(rid, product, now=now)
            site["qualified_at"] = bool(site.get("qualified_at"))
            out["site"][label][product] = site
            out["paying"][label][product] = \
                await RL.paying_referral_count(rid, product)
        empire = deepcopy(await RL.empire_summary(person, now=now))
        empire["empire_id"] = label
        empire.pop("code", None)
        # Wall-clock-derived timestamps are not semantics (they move
        # with the run's `now`); every cent, flag and count around
        # them is. Normalize the same way payout ids are normalized.
        empire["qualified_at"] = bool(empire.get("qualified_at"))
        empire["qualified_at_by_product"] = {
            k: bool(v) for k, v in
            (empire.get("qualified_at_by_product") or {}).items()}
        for site in empire["sites"].values():
            site["qualified_at"] = bool(site.get("qualified_at"))
        for r in empire.get("referrals", []):
            r["schedule_anchor_at"] = bool(r.get("schedule_anchor_at"))
            r.pop("referred_id", None)  # random person UUID, not semantics
        for w in empire.get("free_months", []):
            w["start"] = w["end"] = "<window>"
        for payout in empire["payouts"]:
            # Random payout ids and the wall-clock `at` stamp are not
            # semantics; every cent and flag around them is.
            payout.pop("id", None)
            payout.pop("at", None)
        out["empire"][label] = empire
    rows = await RR.admin_balances(OWNER)
    for row in rows:
        row = deepcopy(row)
        for site in (row.get("sites") or {}).values():
            site["qualified_at"] = bool(site.get("qualified_at"))
        row["empire_id"] = label_of.get(row["empire_id"], row["empire_id"])
        row.pop("code", None)
        out["balances"].append(row)
    return out


def test_summary_oracle(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        return await collect_oracle_outputs(fdb)

    out = run(_run())
    if os.environ.get("ORACLE_REGEN"):
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with open(FIXTURE, "w") as f:
            json.dump(out, f, indent=1, sort_keys=True)
        pytest.skip("oracle fixture regenerated")
    with open(FIXTURE) as f:
        expected = json.load(f)
    assert out == expected
