"""Empire referral ledger host — B1 (platform credentials + ingest)
and B2 (computation port + TC migration path).

Run from the backend directory with:
    python -m pytest tests/test_referral_host.py

No live MongoDB needed — the db is faked in-memory following
tests/test_books_checkout.py conventions, and router/lib functions
are called directly with actor dicts (the checkout tests' pattern).
The computation cases mirror tests/test_referrals.py in Trade Circle
(the reference implementation), adapted to per-site pots.
"""
import asyncio
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_referral_host")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

from lib import referrals as RL  # noqa: E402
from lib import integration_auth as IA  # noqa: E402
from lib import audit as AU  # noqa: E402
from routers import referrals as RR  # noqa: E402


# ---------------- fake in-memory db ----------------
def _get_path(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _match_value(actual, cond):
    if isinstance(cond, dict):
        for op, v in cond.items():
            if op == "$in":
                if actual not in v:
                    return False
            elif op == "$ne":
                if actual == v:
                    return False
            elif op == "$gte":
                if actual is None or actual < v:
                    return False
            elif op == "$lte":
                if actual is None or actual > v:
                    return False
            elif op == "$elemMatch":
                if not isinstance(actual, list):
                    return False
                if not any(all(_get_path(el, k) == vv
                               for k, vv in v.items()) for el in actual):
                    return False
            else:
                return False
        return True
    if isinstance(actual, list):
        # Mongo equality on an array field matches any element.
        return cond in actual
    return actual == cond


def _matches(doc, q):
    for k, v in (q or {}).items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in v):
                return False
        elif not _match_value(_get_path(doc, k), v):
            return False
    return True


def _apply_set(doc, changes):
    for k, v in changes.items():
        if "." in k:
            parts = k.split(".")
            cur = doc
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = v
        else:
            doc[k] = v


class _Result:
    def __init__(self, matched, modified=None):
        self.matched_count = matched
        self.modified_count = matched if modified is None else modified


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs = sorted(
            self._docs,
            key=lambda d: (_get_path(d, key) is None, _get_path(d, key)),
            reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return deepcopy(self._docs[:length] if length else self._docs)

    def __aiter__(self):
        async def gen():
            for d in deepcopy(self._docs):
                yield d
        return gen()


_UNIQUE = {
    "empire_persons": ["code"],
    "referral_events": ["dedupe_key"],
    "referral_attributions": ["id"],
    "referral_earnings": ["id"],
    "referral_payouts": ["batch_key"],
    "integration_credentials": ["client_id"],
}


class _FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                return deepcopy(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([d for d in self.docs if _matches(d, q)])

    async def insert_one(self, doc):
        for key in _UNIQUE.get(self.name, []):
            if any(_get_path(d, key) == _get_path(doc, key)
                   for d in self.docs):
                raise DuplicateKeyError(f"dup {self.name}.{key}")
        self.docs.append(deepcopy(doc))
        return _Result(1)

    def _apply(self, doc, update, inserting=False):
        for op, changes in (update or {}).items():
            if op == "$set":
                _apply_set(doc, deepcopy(changes))
            elif op == "$setOnInsert":
                if inserting:
                    _apply_set(doc, deepcopy(changes))
            elif op == "$unset":
                for k in changes:
                    doc.pop(k, None)
            elif op == "$push":
                for k, v in changes.items():
                    doc.setdefault(k, []).append(v)

    async def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                return _Result(1)
        if upsert:
            doc = {k: v for k, v in (q or {}).items()
                   if not isinstance(v, dict)}
            self._apply(doc, update, inserting=True)
            self.docs.append(doc)
            return _Result(1)
        return _Result(0)

    async def update_many(self, q, update):
        n = 0
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                n += 1
        return _Result(n)

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q))

    async def distinct(self, field, q=None):
        out = []
        for d in self.docs:
            if _matches(d, q):
                v = _get_path(d, field)
                if v is not None and v not in out:
                    out.append(v)
        return out


class _FakeDB:
    def __init__(self):
        for name in ("users", "tenants", "audit_log",
                     "integration_credentials", "empire_persons",
                     "referral_events", "referral_attributions",
                     "referral_earnings", "referral_disputes",
                     "referral_adjustments", "referral_payouts",
                     "referral_payout_queue", "referral_pay_batches",
                     "referral_payment_events"):
            setattr(self, name, _FakeCollection(name))


def _wire(monkeypatch):
    fdb = _FakeDB()
    monkeypatch.setattr(RL, "db", fdb)
    monkeypatch.setattr(RR, "db", fdb)
    monkeypatch.setattr(IA, "db", fdb)
    monkeypatch.setattr(AU, "db", fdb)
    return fdb


def run(coro):
    return asyncio.run(coro)


def _iso_days_ago(days):
    return (datetime.now(timezone.utc)
            - timedelta(days=days)).isoformat()


def _actor(platform="trade_circle"):
    return {"id": f"integration:ci_{platform}", "kind": "platform",
            "platform": platform, "tenant_id": None,
            "scopes": ["referrals:sync"], "role": "integration",
            "auth_kind": "integration"}


OWNER = {"id": "op1", "tenant_id": "ops", "email": "op@empire.example",
         "name": "Operator", "role": "owner"}


async def _setup_referrer(fdb, actor=None, email="ref@example.com",
                          standing=None):
    """A referrer whose own account is attested (paying by default);
    returns (person, code)."""
    actor = actor or _actor()
    res = await RR.ingest_account(
        RR.AccountIn(account_id="acct_ref", email=email,
                     standing=standing or {"paying": True}), actor)
    person = await RL.person_by_id(res["person_id"])
    return person, person["code"]


async def _paying_member(fdb, code, n, actor=None, amount=900,
                         days_ago=70, product_account=None):
    actor = actor or _actor()
    acct = product_account or f"acct_m{n}"
    await RR.ingest_account(
        RR.AccountIn(account_id=acct, email=f"m{n}@example.com",
                     referral_code=code), actor)
    await RR.ingest_payment(
        RR.PaymentIn(account_id=acct, payment_id=f"pay_{n}",
                     amount_cents=amount, paid_at=_iso_days_ago(days_ago),
                     charge=f"ch_{n}"), actor)
    return acct


# ---------------- B1: credential classes ----------------
def test_tenant_credential_is_refused_on_ingest(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        doc, full = IA.new_credential_doc(
            tenant_id="t1", created_by="op", label="Momo sync",
            expires_in_days=365)
        await fdb.integration_credentials.insert_one(doc)
        req = type("Req", (), {"headers":
                               {"Authorization": f"Bearer {full}"}})()
        with pytest.raises(HTTPException) as e:
            await IA.get_platform_actor(req)
        assert e.value.status_code == 403
    run(_run())


def test_platform_credential_without_scope_is_refused(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        doc, full = IA.new_platform_credential_doc(
            platform="trade_circle", created_by="op", label="TC",
            expires_in_days=365)
        doc["scopes"] = ["books:sync"]  # platform class, wrong scope
        await fdb.integration_credentials.insert_one(doc)
        req = type("Req", (), {"headers":
                               {"Authorization": f"Bearer {full}"}})()
        with pytest.raises(HTTPException) as e:
            await IA.get_platform_actor(req)
        assert e.value.status_code == 403
        assert "referrals:sync" in e.value.detail
    run(_run())


def test_platform_credential_authenticates_and_is_product_bound(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        out = await RR.issue_platform_credential(
            RR.PlatformIssueBody(platform="trade_circle"), OWNER)
        assert out["kind"] == "platform" and out["credential"]
        stored = fdb.integration_credentials.docs[0]
        assert stored["tenant_id"] is None
        assert stored["scopes"] == ["referrals:sync"]
        req = type("Req", (), {"headers":
                               {"Authorization":
                                f"Bearer {out['credential']}"}})()
        actor = await IA.get_platform_actor(req)
        assert actor["platform"] == "trade_circle"
        # ...and may not report for another product.
        with pytest.raises(HTTPException) as e:
            await RR.ingest_payment(
                RR.PaymentIn(product="momo", account_id="a1",
                             payment_id="p1", amount_cents=100,
                             paid_at=_iso_days_ago(1)), actor)
        assert e.value.status_code == 403
    run(_run())


# ---------------- B1/B2: ingest + computation parity ----------------
def test_earns_a_quarter_once_per_payment_and_unlock_locks(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        actor = _actor()
        referrer, code = await _setup_referrer(fdb, actor)
        acct = await _paying_member(fdb, code, 1, actor)
        dup = await RR.ingest_payment(
            RR.PaymentIn(account_id=acct, payment_id="pay_1",
                         amount_cents=900, paid_at=_iso_days_ago(70),
                         charge="ch_1"), actor)
        assert dup["duplicate"] is True
        assert len(fdb.referral_earnings.docs) == 1
        s = await RL.site_summary(referrer["id"], "trade_circle")
        # Cleared (70 days) but locked: 1 of 3 paying referrals.
        assert s["available"] == 225 and s["paying_referrals"] == 1
        assert s["locked"] is True and s["payable"] == 0
        assert s["locked_cents"] == 225 and s["earned_cents"] == 225
    run(_run())


def test_only_referrers_in_good_standing_earn(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        actor = _actor()
        # Referrer never attested as paying anywhere -> not eligible.
        referrer, code = await _setup_referrer(
            fdb, actor, email="free@example.com", standing={"paying": False})
        await _paying_member(fdb, code, 1, actor)
        e = fdb.referral_earnings.docs[0]
        assert e["status"] == "not_eligible" and e["share_cents"] == 0
        # ...but the payment still proves the member paid (counts).
        s = await RL.site_summary(referrer["id"], "trade_circle")
        assert s["paying_referrals"] == 1
    run(_run())


def test_six_month_window(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        actor = _actor()
        referrer, code = await _setup_referrer(fdb, actor)
        acct = "acct_w"
        await RR.ingest_account(
            RR.AccountIn(account_id=acct, email="w@example.com",
                         referral_code=code), actor)
        for pid, days in (("pay_w1", 200), ("pay_w2", 100), ("pay_w3", 10)):
            await RR.ingest_payment(
                RR.PaymentIn(account_id=acct, payment_id=pid,
                             amount_cents=900,
                             paid_at=_iso_days_ago(days)), actor)
        # First payment 200d ago anchors the window: +100d earns,
        # +190d is past 183 days and does not.
        assert len(fdb.referral_earnings.docs) == 2
    run(_run())


def test_refunds_reduce_the_earning_and_replays_collapse(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        actor = _actor()
        referrer, code = await _setup_referrer(fdb, actor)
        acct = await _paying_member(fdb, code, 1, actor)
        body = RR.RefundIn(account_id=acct, payment_id="pay_1",
                           refunded_delta_cents=400,
                           refunded_total_cents=400)
        await RR.ingest_refund(body, actor)
        dup = await RR.ingest_refund(body, actor)
        assert dup["duplicate"] is True
        e = fdb.referral_earnings.docs[0]
        assert (e["revenue_cents"] - e["refunded_cents"],
                e["share_cents"]) == (500, 125)
        # A second, sequential partial refund under a new cumulative key.
        await RR.ingest_refund(
            RR.RefundIn(account_id=acct, payment_id="pay_1",
                        refunded_delta_cents=100,
                        refunded_total_cents=500), actor)
        e = fdb.referral_earnings.docs[0]
        assert e["refunded_cents"] == 500 and e["share_cents"] == 100
    run(_run())


def test_chargeback_claws_back_the_month_and_freezes_until_won(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        actor = _actor()
        referrer, code = await _setup_referrer(fdb, actor)
        await _paying_member(fdb, code, 1, actor)
        await RR.ingest_dispute(
            RR.DisputeIn(dispute_id="dp_1", payment_id="pay_1",
                         phase="opened"), actor)
        s = await RL.site_summary(referrer["id"], "trade_circle")
        assert s["frozen"] and s["clawed_back"] == 225
        assert s["payable"] == 0
        await RR.ingest_dispute(
            RR.DisputeIn(dispute_id="dp_1", phase="closed",
                         outcome="won"), actor)
        s = await RL.site_summary(referrer["id"], "trade_circle")
        assert not s["frozen"] and s["clawed_back"] == 0
        assert s["available"] == 225  # released; still locked (1 payer)
    run(_run())


def test_three_disputes_in_six_months_trigger_review(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        actor = _actor()
        referrer, code = await _setup_referrer(fdb, actor)
        for n in range(3):
            await _paying_member(fdb, code, n, actor,
                                 product_account=f"acct_d{n}")
            await RR.ingest_dispute(
                RR.DisputeIn(dispute_id=f"dp_{n}", payment_id=f"pay_{n}",
                             phase="opened"), actor)
            await RR.ingest_dispute(
                RR.DisputeIn(dispute_id=f"dp_{n}", phase="closed",
                             outcome="won"), actor)
        person = await RL.person_by_id(referrer["id"])
        assert person["review"] is True
        s = await RL.site_summary(referrer["id"], "trade_circle")
        assert s["frozen"] is True
    run(_run())


def test_self_referral_links_accounts_but_earns_nothing(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        actor = _actor()
        referrer, code = await _setup_referrer(fdb, actor)
        res = await RR.ingest_account(
            RR.AccountIn(account_id="acct_books_self",
                         email="ref@example.com", referral_code=code),
            _actor("books"))
        assert res["attribution_status"] == "self"
        await RR.ingest_payment(
            RR.PaymentIn(account_id="acct_books_self",
                         payment_id="pay_self", amount_cents=900,
                         paid_at=_iso_days_ago(70)), _actor("books"))
        assert fdb.referral_earnings.docs == []
    run(_run())


def test_per_site_unlock_isolation(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        tc, books = _actor("trade_circle"), _actor("books")
        referrer, code = await _setup_referrer(fdb, tc)
        for n in range(3):
            await _paying_member(fdb, code, n, tc,
                                 product_account=f"acct_tc{n}")
        for n in range(2):
            await _paying_member(fdb, code, n + 10, books,
                                 product_account=f"acct_bk{n}")
        s_tc = await RL.site_summary(referrer["id"], "trade_circle")
        s_bk = await RL.site_summary(referrer["id"], "books")
        # 3 paying on TC unlocks the TC pot in full...
        assert s_tc["locked"] is False and s_tc["payable"] == 675
        # ...while 2 paying on books leaves the books pot locked.
        assert s_bk["locked"] is True and s_bk["payable"] == 0
        assert s_bk["locked_cents"] == 450
        summary = await RR.readback_summary("acct_ref", None, tc)
        assert summary["totals"]["payable"] == 675
        assert summary["terms"]["unlock_is_per_site"] is True
    run(_run())


# ---------------- B2: books outbox drain ----------------
def test_books_outbox_drains_into_the_ledger_once(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        referrer, code = await _setup_referrer(fdb)
        fdb.tenants.docs.append(
            {"id": "t9", "name": "Referred Shop", "referred_by_code": code})
        fdb.referral_payment_events.docs.append({
            "id": "evt1", "type": "referral.payment", "product": "books",
            "tenant_id": "t9", "referred_account_id": "t9",
            "referred_email": "buyer@example.com", "referral_code": code,
            "payment_id": "in_books1", "amount_cents": 4000,
            "currency": "usd", "paid_at": _iso_days_ago(70),
            "emitted_at": _iso_days_ago(70)})
        out = await RR.drain_books_outbox(OWNER)
        assert out["processed"] == 1
        assert len(fdb.referral_earnings.docs) == 1
        e = fdb.referral_earnings.docs[0]
        assert e["product"] == "books" and e["share_cents"] == 1000
        out2 = await RR.drain_books_outbox(OWNER)
        assert out2["duplicates"] == 1
        assert len(fdb.referral_earnings.docs) == 1
    run(_run())


# ---------------- B2: TC migration import ----------------
def _tc_fixture():
    return {
        "members": [
            {"tc_user_id": "u_ref", "email": "ref@example.com",
             "code": "REFCODE1", "paying": True},
            {"tc_user_id": "u_mem", "email": "mem@example.com",
             "code": "MEMCODE1", "referred_by_tc_user_id": "u_ref",
             "referral_first_paid_at": _iso_days_ago(70)},
        ],
        "earnings": [
            {"invoice_id": "in_1", "referrer_tc_user_id": "u_ref",
             "referred_tc_user_id": "u_mem", "revenue_cents": 900,
             "refunded_cents": 0, "share_cents": 225, "status": "paid",
             "month": "2026-08", "earned_at": _iso_days_ago(70),
             "charge": "ch_1", "payout_id": "po_1"},
        ],
        "payouts": [
            {"id": "po_1", "referrer_tc_user_id": "u_ref", "cents": 225,
             "note": "PayPal", "at": _iso_days_ago(5)},
        ],
        "adjustments": [],
        "disputes": [],
    }


def test_tc_migration_import_lands_as_site_history(monkeypatch):
    async def _run():
        fdb = _wire(monkeypatch)
        counts = await RR.tc_import(
            RR.TcImportIn(**_tc_fixture()), OWNER)
        assert counts["persons"] == 2 and counts["earnings"] == 1
        assert counts["payouts"] == 1
        referrer = await RL.person_by_email("ref@example.com")
        assert referrer["code"] == "REFCODE1"  # shared links keep working
        s = await RL.site_summary(referrer["id"], "trade_circle")
        assert s["paid"] == 225 and s["paying_referrals"] == 1
        payout = fdb.referral_payouts.docs[0]
        assert payout["rail"] == "manual" and payout["net_cents"] == 225
        # Re-import is idempotent: everything already landed is skipped.
        counts2 = await RR.tc_import(
            RR.TcImportIn(**_tc_fixture()), OWNER)
        assert counts2["earnings"] == 0 and counts2["payouts"] == 0
        assert len(fdb.referral_earnings.docs) == 1
        assert len(fdb.referral_payouts.docs) == 1
    run(_run())
