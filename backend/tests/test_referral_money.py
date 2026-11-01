"""Money-path tests for the adopted referral law (R-1/R-2/R-3).

Covers: schedule band boundaries (months 6/7, 12/13, 24/25, 36/37,
48 end), the qualification transition (3rd = free month, no cash;
4th = qualified_at, no retroactivity), first-four vs later anchors,
refund recompute per original band, cancel+return mid-clock, the
250-active bonus lock (and NOT at 100), grace tracking, free-month
whole-month crediting, payout gating on qualified_at, and the tail
cap. Same fake-db harness as test_referral_host.
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_referral_money")

import pytest  # noqa: E402

from lib import plans as PLANS  # noqa: E402
from lib import referrals as RL  # noqa: E402
from test_referral_host import _wire, run  # noqa: E402

T0 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def _add_months(dt, n):
    m = dt.month - 1 + n
    year, month = dt.year + m // 12, m % 12 + 1
    day = min(dt.day, [31, 29 if year % 4 == 0 and
              (year % 100 != 0 or year % 400 == 0) else 28,
              31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return dt.replace(year=year, month=month, day=day)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


async def _referrer(fdb, email="ref@money.example", paying=True):
    person = await RL.ensure_person(email)
    person = await RL.link_account(
        person, product="trade_circle", account_id=f"own-{email}",
        email=email, standing={"paying": paying})
    return await RL.person_by_id(person["id"])


async def _pay(fdb, code, acct, pid, amount, at, product="trade_circle",
               signup_plan=None):
    await RL.apply_attribution(
        product=product, account_id=acct, email=f"{acct}@money.example",
        referral_code=code, signup_plan=signup_plan)
    await RL.apply_payment(
        product=product, account_id=acct, payment_id=pid,
        amount_cents=amount, paid_at=_iso(at))
    return await fdb.referral_earnings.find_one(
        {"id": f"{product}:{pid}"})


async def _qualified(fdb, email="ref@money.example", amount=10000, at=T0):
    referrer = await _referrer(fdb, email)
    code = referrer["code"]
    for i in range(1, 5):
        await _pay(fdb, code, f"q{i}", f"qp{i}", amount, at)
    return await RL.person_by_id(referrer["id"]), code


def _earning(fdb, pid):
    return next(d for d in fdb.referral_earnings.docs
                if d["payment_id"] == pid)


# ------------------------------------------------------------- bands
def test_schedule_band_boundaries(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer, code = await _qualified(fdb)
        # Referral q1 anchors at qualified_at (T0). Payments across the
        # whole 36-month entitlement at 10000 cents each.
        cases = [  # (months after anchor, expected rate, expected share)
            (5, 0.25, 2500),    # month 6  — last of band 1
            (6, 0.10, 1000),    # month 7  — first of band 2
            (11, 0.10, 1000),   # month 12 — last of band 2
            (12, 0.02, 200),    # month 13 — first of band 3
            (23, 0.02, 200),    # month 24 — last of band 3
            (24, 0.01, 100),    # month 25 — first of band 4
            (35, 0.01, 100),    # month 36 — last base month
            (36, 0, 0),         # month 37 — no lock: entitlement over
        ]
        for i, (months, rate, share) in enumerate(cases):
            e = await _pay(fdb, code, "q1", f"b{i}", 10000,
                           _add_months(T0, months))
            assert e["month_index"] == months + 1, (months, e)
            assert e["rate"] == rate and e["share_cents"] == share
            if rate == 0:
                assert e["status"] == "beyond_entitlement"
    run(_run())


def test_bonus_year_months_37_to_48_and_tail_cap(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer, code = await _qualified(fdb)
        # Stamp a lock directly (the 250 path is covered separately):
        # simulate the lock the threshold engine would have stamped.
        await fdb.referral_attributions.update_one(
            {"id": "trade_circle:q2"}, {"$set": {"bonus_year_locked": True}})
        cases = [(36, 100), (47, 100), (48, 0)]  # months 37, 48, 49
        for i, (months, share) in enumerate(cases):
            e = await _pay(fdb, code, "q2", f"by{i}", 10000,
                           _add_months(T0, months))
            assert e["month_index"] == months + 1
            assert e["share_cents"] == share, (months, e)
        assert RL.CONFIG["tail_open_ended"] is False
    run(_run())


# ------------------------------------------------------- qualification
def test_qualification_transition_and_no_retroactivity(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer = await _referrer(fdb)
        code = referrer["code"]
        # First three pay before qualification: under the ruled law
        # they never earn, whatever month they paid in.
        dec = datetime(2025, 12, 15, 12, 0, tzinfo=timezone.utc)
        for i in range(1, 4):
            e = await _pay(fdb, code, f"r{i}", f"rp{i}", 10000, dec)
            assert e["status"] == "pre_qualification"
            assert e["share_cents"] == 0
        person = await RL.person_by_id(referrer["id"])
        assert RL.qualified_at_for(person, "trade_circle") is None
        # 3rd paying referral granted the R-2 free month, not cash.
        grants = [d for d in fdb.referral_free_months.docs
                  if d["source"] == "third_paying_referral"]
        assert len(grants) == 1 and grants[0]["plan"] == "paid"
        # 4th paying referral qualifies; ITS payment earns month 1.
        e4 = await _pay(fdb, code, "r4", "rp4", 10000, T0)
        assert e4["status"] == "pending" and e4["share_cents"] == 2500
        person = await RL.person_by_id(referrer["id"])
        assert RL.qualified_at_for(person, "trade_circle") == _iso(T0)
        # Nothing retroactive: the first three stay zero-share audit.
        for i in range(1, 4):
            assert _earning(fdb, f"rp{i}")["share_cents"] == 0
    run(_run())


def test_first_four_anchors_vs_later_anchors(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer, code = await _qualified(fdb)
        atts = {a["account_id"]: a
                for a in fdb.referral_attributions.docs}
        for i in range(1, 5):
            assert atts[f"q{i}"]["schedule_anchor_at"] == _iso(T0)
        later = T0 + timedelta(days=40)
        await _pay(fdb, code, "q5", "qp5", 10000, later)
        atts = {a["account_id"]: a
                for a in fdb.referral_attributions.docs}
        assert atts["q5"]["schedule_anchor_at"] == _iso(later)
    run(_run())


def test_free_month_stacks_sequentially(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer = await _referrer(fdb)
        # An earned free month already active across T0.
        await RL.record_free_month_window(
            referrer["id"], "trade_circle",
            start=_iso(T0 - timedelta(days=5)),
            end=_iso(T0 + timedelta(days=10)), source="cheers_grant")
        code = referrer["code"]
        for i in range(1, 4):
            await _pay(fdb, code, f"s{i}", f"sp{i}", 900, T0)
        grant = next(d for d in fdb.referral_free_months.docs
                     if d["source"] == "third_paying_referral")
        assert grant["start"] == _iso(T0 + timedelta(days=10))
    run(_run())


# ------------------------------------------------------------ refunds
def test_refund_recompute_uses_original_band(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer, code = await _qualified(fdb)
        e = await _pay(fdb, code, "q1", "rf13", 10000, _add_months(T0, 12))
        assert e["rate"] == 0.02 and e["share_cents"] == 200  # month 13
        out = await RL.apply_refund(
            product="trade_circle", payment_id="rf13",
            refunded_delta_cents=5000, refunded_total_cents=5000)
        assert out["share_cents"] == 100  # 2% of net 5000, NOT 25%
        e1 = await _pay(fdb, code, "q1", "rf1", 10000, T0)
        assert e1["share_cents"] == 2500
        out = await RL.apply_refund(
            product="trade_circle", payment_id="rf1",
            refunded_delta_cents=5000, refunded_total_cents=5000)
        assert out["share_cents"] == 1250  # 25% of net 5000
    run(_run())


# ------------------------------------------------------ cancel + return
def test_cancel_and_return_resumes_at_current_month(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer, code = await _qualified(fdb)
        e = await _pay(fdb, code, "q3", "cr1", 10000, T0)
        assert e["month_index"] == 1 and e["rate"] == 0.25
        # Months 2-5 lapse with no payments (cancelled): the return
        # payment joins the qualification payment as q3's 2nd earning.
        assert len([d for d in fdb.referral_earnings.docs
                    if d["account_id"] == "q3"]) == 2
        # Return in month 6: still band 1 (clock never paused/restarted).
        e = await _pay(fdb, code, "q3", "cr6", 10000, _add_months(T0, 5))
        assert e["month_index"] == 6 and e["rate"] == 0.25
        assert e["schedule_anchor_at"] == _iso(T0)
        # Month 8 lands in band 2 — the lapsed months are gone for good.
        e = await _pay(fdb, code, "q3", "cr8", 10000, _add_months(T0, 7))
        assert e["month_index"] == 8 and e["rate"] == 0.10
    run(_run())


# ---------------------------------------------------------- thresholds
def test_threshold_lock_at_250_not_at_100_and_grace(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer, code = await _qualified(fdb, amount=900)
        at = T0 + timedelta(days=1)  # all within the 45-day window
        locked_at = {}
        for i in range(1, 251):
            acct = f"t{i}"
            await _pay(fdb, code, acct, f"tp{i}", 900, at)
            att = await fdb.referral_attributions.find_one(
                {"id": f"trade_circle:{acct}"})
            locked_at[i] = att["bonus_year_locked"]
            if i == 100:
                st = await RL.threshold_state(
                    referrer["id"], "trade_circle", at)
                assert st["milestone_reached"] is True
                assert st["above_bonus_threshold"] is False
        # 100 confers nothing mechanically. The active count includes
        # the 4 qualifiers (paid a day earlier), so the batch payer who
        # brings the total to 250 active is #246 — #245 is not enough.
        assert locked_at[100] is False
        assert locked_at[245] is False
        assert locked_at[246] is True
        st = await RL.threshold_state(referrer["id"], "trade_circle", at)
        assert st["above_bonus_threshold"] is True
        assert st["active_paying_referrals"] >= 250
        # Dip below 250 later: grace is tracked for 3 months, and a new
        # referral during grace does NOT lock (grace_locks_bonus=False).
        dip = at + timedelta(days=60)
        await RL._track_threshold(referrer["id"], "trade_circle", dip)
        st = await RL.threshold_state(referrer["id"], "trade_circle", dip)
        assert st["in_grace"] is True and st["grace_until"] is not None
        e = await _pay(fdb, code, "grace1", "gp1", 900, dip)
        att = await fdb.referral_attributions.find_one(
            {"id": "trade_circle:grace1"})
        assert att["bonus_year_locked"] is False
        # The locked referral keeps its fourth year permanently.
        e = await _pay(fdb, code, "t246", "tp250b", 10000,
                       _add_months(T0 + timedelta(days=1), 36))
        assert e["month_index"] == 37 and e["share_cents"] == 100
    run(_run())


# ------------------------------------------------ free-month cash rule
def test_free_track_cash_only_in_free_month_whole_month(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer = await _referrer(fdb, email="free@money.example",
                                   paying=False)
        code = referrer["code"]
        for i in range(1, 5):
            await _pay(fdb, code, f"f{i}", f"fp{i}", 10000, T0)
        person = await RL.person_by_id(referrer["id"])
        # Free-track referrals still count toward qualification.
        assert RL.qualified_at_for(person, "trade_circle") is not None
        # Earned free month: Mar 1 - Mar 15, 2026 (attested window).
        await RL.record_free_month_window(
            referrer["id"], "trade_circle",
            start="2026-03-01T00:00:00+00:00",
            end="2026-03-15T00:00:00+00:00", source="cheers_grant")
        # Payment Mar 31: outside the window's dates but inside its
        # calendar month -> whole-month rule credits it.
        e = await _pay(fdb, code, "f5", "fp5", 10000,
                       datetime(2026, 3, 31, tzinfo=timezone.utc))
        assert e["status"] == "pending" and e["share_cents"] == 2500
        # Payment in April (no overlap): no cash on the free track.
        e = await _pay(fdb, code, "f6", "fp6", 10000,
                       datetime(2026, 4, 2, tzinfo=timezone.utc))
        assert e["status"] == "not_eligible" and e["share_cents"] == 0
    run(_run())


def test_qualification_month_no_retroactive_conversion(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer = await _referrer(fdb)
        code = referrer["code"]
        dec = datetime(2025, 12, 20, tzinfo=timezone.utc)
        await _pay(fdb, code, "m0", "mp0", 10000, dec)  # earlier month
        jan5 = datetime(2026, 1, 5, tzinfo=timezone.utc)
        jan6 = datetime(2026, 1, 6, tzinfo=timezone.utc)
        jan20 = datetime(2026, 1, 20, tzinfo=timezone.utc)
        await _pay(fdb, code, "m1", "mp1", 10000, jan5)
        await _pay(fdb, code, "m2", "mp2", 10000, jan6)
        # The 4th distinct paying referral qualifies at Jan 20.
        e4 = await _pay(fdb, code, "m3", "mp3", 10000, jan20)
        assert e4["status"] == "pending" and e4["share_cents"] == 2500
        assert e4["month_index"] == 1
        # Ruled law: same-month pre-qualification payments stay zero
        # after qualification — nothing is paid retroactively, in
        # the qualification month or any earlier month.
        for pid in ("mp0", "mp1", "mp2"):
            e = _earning(fdb, pid)
            assert e["status"] == "pre_qualification", (pid, e)
            assert e["share_cents"] == 0 and e["rate"] == 0
            assert e["month_index"] is None
        # Anchors still equal qualified_at for the first four: the
        # qualification month counts in full as month 1 of their
        # clocks (no proration from the qualification day forward).
        person = await RL.person_by_id(referrer["id"])
        assert RL.qualified_at_for(person, "trade_circle") == _iso(jan20)
        atts = {a["account_id"]: a
                for a in fdb.referral_attributions.docs}
        for acct in ("m0", "m1", "m2", "m3"):
            assert atts[acct]["schedule_anchor_at"] == _iso(jan20)
        # A payment later in the qualification month, after
        # qualified_at, earns at the month-1 band from that anchor.
        jan25 = datetime(2026, 1, 25, tzinfo=timezone.utc)
        late = await _pay(fdb, code, "m1", "mp1b", 10000, jan25)
        assert late["status"] == "pending"
        assert late["share_cents"] == 2500 and late["month_index"] == 1
        assert late["schedule_anchor_at"] == _iso(jan20)
    run(_run())


# -------------------------------------------------------- payout gating
def test_payout_gated_on_qualified_at(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        now = datetime.now(timezone.utc)
        at = now - timedelta(days=70)
        # Three paying referrals: not qualified -> payout refused.
        r3 = await _referrer(fdb, "three@money.example")
        for i in range(1, 4):
            await _pay(fdb, r3["code"], f"p{i}", f"pp{i}", 900, at)
        with pytest.raises(RL.ReferralError):
            await RL.record_manual_payout(
                r3["id"], "trade_circle", note="x", by="op", now=now)
        # Qualified referrer with cleared earnings: payout records.
        rq, code = await _qualified(fdb, "four@money.example",
                                    amount=900, at=at)
        out = await RL.record_manual_payout(
            rq["id"], "trade_circle", note="x", by="op", now=now)
        # Only the 4th (qualifying) payment earns: the first three
        # are pre-qualification payments and never convert, even
        # though all four landed in the qualification month.
        assert out["gross_cents"] == 225
        s = await RL.site_summary(rq["id"], "trade_circle", now=now)
        assert s["paid"] == 225 and s["qualified"] is True
    run(_run())


# ------------------------------------------------------- friend benefit
def test_friend_benefit_recorded_and_exposed(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer, code = await _qualified(fdb)
        res = await RL.apply_attribution(
            product="books", account_id="bk-light",
            email="bk-light@money.example", referral_code=code,
            signup_plan="light")
        assert res["friend_benefit"]["type"] == "tier_up"
        assert res["friend_benefit"]["tier"] == "medium"
        assert res["friend_benefit"]["days"] == 14
        res = await RL.apply_attribution(
            product="books", account_id="bk-heavy",
            email="bk-heavy@money.example", referral_code=code,
            signup_plan="heavy")
        assert res["friend_benefit"]["type"] == "half_off"
        assert res["friend_benefit"]["months"] == 2
        summary = await RL.empire_summary(
            await RL.person_by_id(referrer["id"]))
        by_acct = {r["account_id"]: r for r in summary["referrals"]}
        assert by_acct["bk-light"]["friend_benefit"]["tier"] == "medium"
        assert by_acct["bk-heavy"]["friend_benefit"]["type"] == "half_off"
        assert summary["terms"]["bands"][1] == {
            "through_month": 12, "rate": 0.10}
    run(_run())


def test_friend_benefit_tier_up_consumed_by_effective_plan():
    now = datetime.now(timezone.utc)
    tenant = {"plan": "light",
              "friend_benefit": {
                  "type": "tier_up", "tier": "medium",
                  "ends_at": (now + timedelta(days=13)).isoformat()}}
    assert PLANS.effective_plan(tenant) == "medium"
    expired = {"plan": "light",
               "friend_benefit": {
                   "type": "tier_up", "tier": "medium",
                   "ends_at": (now - timedelta(days=1)).isoformat()}}
    assert PLANS.effective_plan(expired) == "light"
    top = {"plan": "heavy",
           "friend_benefit": {"type": "half_off", "months": 2}}
    assert PLANS.effective_plan(top) == "heavy"
