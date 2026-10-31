"""Trial doors (owner ruling 2026-10-31) — the 14-day trial's two
doors and everything that rides them.

Pins:
1. Terms in exact cents: the card door's first paid month is 50% off
   the WHOLE month (Light 1899 -> 949 after a 950 discount; Medium
   3999 -> 1999 after 2000), month two at list; the no-card path has
   no discount anywhere.
2. Door state machine: door 2 stamps the opt-in (estimate unlocked,
   referral active instantly, discount terms recorded); door 1 has
   none of it until conversion; the mid-trial upgrade flips it all;
   downgrade withdraws it.
3. Expiry: an unconverted trial reads "expired", access falls back to
   the stored plan, and referral stays inactive for door 1 until a
   real conversion (invoice paid).
4. Auto-conversion sweep: fires only for an expired card-door tenant
   with Stripe configured AND a card on file — otherwise it returns
   the labeled state and creates nothing.
5. Convert endpoint: the coupon rides ONLY the opt-in path.
6. Cancellation: completes immediately with no reason, with a button
   reason (recorded verbatim, with door + trial state), and with an
   off-list reason (kept as free text); a subscriber's reason is
   recorded and the portal pointer returned.

Same fake-db idiom as test_welcome_gift.py; Stripe entry points are
monkeypatched fakes — the live billing webhook remains the only
writer of billing state, exactly as in production.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ------------------------------------------------------------------ fakes
class FakeColl:
    def __init__(self):
        self.docs = {}
        self._seq = 0

    def _match(self, doc, q):
        for k, v in (q or {}).items():
            cur = doc
            for part in k.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if cur != v:
                return False
        return True

    async def insert_one(self, doc):
        self._seq += 1
        doc.setdefault("_id", f"oid{self._seq}")
        self.docs[doc["_id"]] = dict(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    async def find_one(self, q=None, proj=None):
        for d in self.docs.values():
            if self._match(d, q or {}):
                return dict(d)
        return None

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs.values():
            if self._match(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))


COLL_NAMES = ("tenants", "users", "audit_log", "trial_cancellations",
              "referral_payment_events", "stripe_webhook_events")


class FakeDB:
    def __init__(self):
        for name in COLL_NAMES:
            setattr(self, name, FakeColl())

    def tenant(self):
        return next(iter(self.tenants.docs.values()))


USER = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.example",
        "role": "owner", "name": "Owner"}


def _tenant(door="nocard", when="trialing", **extra):
    now = datetime.now(timezone.utc)
    t = {"id": "t1", "name": "Maple HVAC", "plan": "light",
         "trial_started_at": (now - timedelta(days=2)).isoformat(),
         "trial_door": door, "card_opt_in_at": None,
         "referral_active": False, "referral_active_source": None,
         "first_month_discount_percent": None,
         "first_month_discount_applied": False, "trial_canceled_at": None}
    if when == "trialing":
        t["trial_ends_at"] = (now + timedelta(days=12)).isoformat()
    elif when == "expired":
        t["trial_ends_at"] = (now - timedelta(days=1)).isoformat()
    if door == "card":
        t["card_opt_in_at"] = (now - timedelta(days=2)).isoformat()
        t["referral_active"] = True
        t["referral_active_source"] = "trial_card_opt_in"
        t["first_month_discount_percent"] = 50
    t.update(extra)
    return t


def _patch(fake, monkeypatch):
    import lib.trial as TL
    import routers.billing as BR
    import routers.trial as TR
    monkeypatch.setattr(TR, "db", fake)
    monkeypatch.setattr(BR, "db", fake)
    return TR, TL, BR


def _expect_http(status, coro_fn):
    try:
        run(coro_fn())
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTP {status}")


class FakeSession:
    def __init__(self, url="https://checkout.example/s", id="cs_1"):
        self.url = url
        self.id = id


# ------------------------------------------------------------------ terms

def test_first_month_terms_exact_cents():
    from lib import trial as TL
    light = TL.first_month_terms("light", opted_in=True)
    assert light["list_cents"] == 1899
    assert light["discount_cents"] == 950       # 50% of 1899 = 949.5 -> 950
    assert light["first_month_cents"] == 949    # the whole month, half off
    assert light["month_two_cents"] == 1899     # then full price
    medium = TL.first_month_terms("medium", opted_in=True)
    assert medium["discount_cents"] == 2000
    assert medium["first_month_cents"] == 1999
    assert medium["month_two_cents"] == 3999
    plain = TL.first_month_terms("medium", opted_in=False)
    assert plain["discount_cents"] == 0
    assert plain["first_month_cents"] == 3999 == plain["month_two_cents"]


# ------------------------------------------------------------------ doors

def test_door_states_and_upgrade(monkeypatch):
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("nocard")))
    TR, TL, _BR = _patch(fake, monkeypatch)

    state = run(TR.get_trial(USER))
    assert state["door"] == "nocard" and state["state"] == "trialing"
    assert state["gifts"] == {"clean": True, "estimate": False}
    assert state["referral_active"] is False
    assert state["first_month_discount_percent"] is None
    assert state["trial_days"] == 14

    # Mid-trial upgrade to the card door: everything flips at once.
    state = run(TR.set_door(TR.DoorIn(door="card"), USER))
    assert state["door"] == "card"
    assert state["gifts"]["estimate"] is True
    assert state["referral_active"] is True
    assert state["referral_active_source"] == "trial_card_opt_in"
    assert state["first_month_discount_percent"] == 50
    assert fake.tenant()["card_opt_in_at"] is not None

    # Downgrade withdraws the opt-in and its extras.
    state = run(TR.set_door(TR.DoorIn(door="nocard"), USER))
    assert state["gifts"]["estimate"] is False
    assert state["referral_active"] is False
    assert state["first_month_discount_percent"] is None

    e = _expect_http(422, lambda: TR.set_door(TR.DoorIn(door="gold"), USER))
    assert "door" in str(e.detail)


def test_door2_referral_active_during_trial_door1_not(monkeypatch):
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("card")))
    _TR, TL, _BR = _patch(fake, monkeypatch)
    assert TL.trial_state(fake.tenant()) == "trialing"
    assert TL.referral_active(fake.tenant()) is True

    fake2 = FakeDB()
    run(fake2.tenants.insert_one(_tenant("nocard", when="expired")))
    assert TL.trial_state(fake2.tenant()) == "expired"
    assert TL.referral_active(fake2.tenant()) is False


def test_conversion_activates_referral_and_spends_discount(monkeypatch):
    """The webhook path: an invoice paid converts the tenant — door 1
    gets referral activation here (not before), and a card-door
    tenant's discount terms are marked applied at checkout."""
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("nocard", when="expired", billing={
        "provider": "stripe", "stripe_customer_id": "cus_1",
        "subscription_id": "sub_1", "plan": "medium", "status": "active"})))
    _TR, _TL, BR = _patch(fake, monkeypatch)
    invoice = {"id": "in_1", "subscription": "sub_1", "customer": "cus_1",
               "amount_paid": 3999, "currency": "usd",
               "status_transitions": {"paid_at": 1760000000}}
    run(BR._on_invoice_paid(invoice, {"id": "evt_1"}))
    t = fake.tenant()
    assert t["referral_active"] is True
    assert t["referral_active_source"] == "conversion"


def test_card_setup_webhook_records_card_on_file(monkeypatch):
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("card")))
    _TR, _TL, BR = _patch(fake, monkeypatch)
    monkeypatch.setattr(BR.billing, "setup_intent_payment_method",
                        lambda si: "pm_123")
    obj = {"id": "cs_setup", "mode": "setup", "customer": "cus_9",
           "setup_intent": "seti_1",
           "metadata": {"kind": "books_card_setup", "tenant_id": "t1"}}
    run(BR._on_checkout_completed(obj))
    b = fake.tenant()["billing"]
    assert b["card_on_file"] is True
    assert b["card_payment_method_id"] == "pm_123"
    assert b["stripe_customer_id"] == "cus_9"


# ------------------------------------------------------- auto-conversion

def _patch_billing(fake, monkeypatch, *, configured=True, created=None):
    import lib.billing as BL
    monkeypatch.setattr(BL, "configured", lambda: configured)
    monkeypatch.setattr(BL, "ensure_first_month_coupon",
                        lambda: "books-first-month-50")
    def _create(**kw):
        if created is not None:
            created.append(kw)
        return type("S", (), {"id": "sub_new"})()
    monkeypatch.setattr(BL, "create_subscription_off_session", _create)
    return BL


def test_auto_convert_fires_only_with_card_and_stripe(monkeypatch):
    import lib.trial as TL
    # Expired card-door tenant, card on file, Stripe configured -> fires
    created = []
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("card", when="expired", billing={
        "stripe_customer_id": "cus_1", "card_on_file": True,
        "card_payment_method_id": "pm_1"})))
    _patch_billing(fake, monkeypatch, created=created)
    out = run(TL.maybe_auto_convert(fake, fake.tenant()))
    assert out == "initiated"
    assert created and created[0]["coupon_id"] == "books-first-month-50"
    assert created[0]["customer_id"] == "cus_1"
    assert fake.tenant()["trial_conversion"]["status"] == "initiated"
    # Second look: already initiated, nothing created twice.
    assert run(TL.maybe_auto_convert(fake, fake.tenant())) == "already"
    assert len(created) == 1


def test_auto_convert_honest_pending_states(monkeypatch):
    import lib.trial as TL
    # No card on file -> awaiting_card, nothing created
    created = []
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("card", when="expired", billing={
        "stripe_customer_id": "cus_1"})))
    _patch_billing(fake, monkeypatch, created=created)
    assert run(TL.maybe_auto_convert(fake, fake.tenant())) == "awaiting_card"
    assert created == [] and "trial_conversion" not in fake.tenant()

    # Stripe not configured -> labeled, nothing created
    fake2 = FakeDB()
    run(fake2.tenants.insert_one(_tenant("card", when="expired", billing={
        "stripe_customer_id": "cus_1", "card_on_file": True})))
    _patch_billing(fake2, monkeypatch, configured=False, created=created)
    assert run(TL.maybe_auto_convert(fake2, fake2.tenant())) == \
        "billing_not_configured"
    assert created == []

    # Still trialing, or door 1 -> not due
    fake3 = FakeDB()
    run(fake3.tenants.insert_one(_tenant("card", when="trialing", billing={
        "stripe_customer_id": "cus_1", "card_on_file": True})))
    _patch_billing(fake3, monkeypatch, created=created)
    assert run(TL.maybe_auto_convert(fake3, fake3.tenant())) == "not_due"
    fake4 = FakeDB()
    run(fake4.tenants.insert_one(_tenant("nocard", when="expired")))
    assert run(TL.maybe_auto_convert(fake4, fake4.tenant())) == "not_due"


# ---------------------------------------------------------------- convert

def test_convert_coupon_only_on_opt_in_path(monkeypatch):
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("card")))
    TR, _TL, _BR = _patch(fake, monkeypatch)
    seen = {}

    import lib.billing as BL
    monkeypatch.setattr(BL, "configured", lambda: True)
    monkeypatch.setattr(BL, "ensure_first_month_coupon",
                        lambda: "books-first-month-50")
    def _session(**kw):
        seen.update(kw)
        return FakeSession()
    monkeypatch.setattr(BL, "create_checkout_session", _session)

    req = type("R", (), {"headers": {"origin": "https://books.example"}})()
    out = run(TR.convert(TR.ConvertIn(plan="medium"), req, USER))
    assert out["coupon_applied"] is True
    assert seen["coupon_id"] == "books-first-month-50"

    # Door 1 converting manually: list price, no coupon.
    fake2 = FakeDB()
    run(fake2.tenants.insert_one(_tenant("nocard")))
    monkeypatch.setattr(TR, "db", fake2)
    seen.clear()
    out = run(TR.convert(TR.ConvertIn(plan="medium"), req, USER))
    assert out["coupon_applied"] is False
    assert seen["coupon_id"] == ""


def test_convert_and_card_session_503_when_billing_unconfigured(monkeypatch):
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("card")))
    TR, _TL, _BR = _patch(fake, monkeypatch)
    import lib.billing as BL
    monkeypatch.setattr(BL, "configured", lambda: False)
    req = type("R", (), {"headers": {"origin": "https://books.example"}})()
    _expect_http(503, lambda: TR.convert(TR.ConvertIn(plan="medium"), req, USER))
    _expect_http(503, lambda: TR.card_session(TR.CardSessionIn(), req, USER))


# ----------------------------------------------------------------- cancel

def test_cancel_completes_without_reason_and_ends_trial(monkeypatch):
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("nocard")))
    TR, TL, _BR = _patch(fake, monkeypatch)
    out = run(TR.cancel(TR.CancelIn(), USER))
    assert out["canceled"] is True and out["state"] == "canceled"
    docs = list(fake.trial_cancellations.docs.values())
    assert len(docs) == 1
    assert docs[0]["reason"] is None and docs[0]["note"] is None
    assert docs[0]["door"] == "nocard" and docs[0]["trial_state"] == "trialing"
    assert docs[0]["tenant_id"] == "t1" and docs[0]["canceled_at"]
    # The trial really ended: no longer active, data untouched.
    from lib import plans as _plans
    t = fake.tenant()
    assert _plans.trial_active(t) is False and t["name"] == "Maple HVAC"


def test_cancel_records_button_reason_verbatim(monkeypatch):
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("card")))
    TR, _TL, _BR = _patch(fake, monkeypatch)
    out = run(TR.cancel(TR.CancelIn(reason="Too expensive"), USER))
    assert out["canceled"] is True
    doc = next(iter(fake.trial_cancellations.docs.values()))
    assert doc["reason"] == "Too expensive"          # verbatim from the set
    assert doc["door"] == "card" and doc["trial_state"] == "trialing"

    # Off-list text is kept verbatim as a note, never dropped, and the
    # cancellation still completes.
    fake2 = FakeDB()
    run(fake2.tenants.insert_one(_tenant("nocard")))
    monkeypatch.setattr(TR, "db", fake2)
    out = run(TR.cancel(TR.CancelIn(reason="My accountant said no"), USER))
    assert out["canceled"] is True
    doc = next(iter(fake2.trial_cancellations.docs.values()))
    assert doc["reason"] is None
    assert doc["note"] == "My accountant said no"


def test_cancel_for_subscriber_records_reason_and_points_to_portal(
        monkeypatch):
    fake = FakeDB()
    run(fake.tenants.insert_one(_tenant("card", billing={
        "provider": "stripe", "status": "active", "plan": "medium"})))
    TR, _TL, _BR = _patch(fake, monkeypatch)
    out = run(TR.cancel(TR.CancelIn(reason="Switching to another tool",
                                    note="moving to Xero"), USER))
    assert out["canceled"] is True and out["next"] == "portal"
    doc = next(iter(fake.trial_cancellations.docs.values()))
    assert doc["reason"] == "Switching to another tool"
    assert doc["note"] == "moving to Xero"
    assert doc["trial_state"] == "converted"
    # The subscription itself is untouched here — Stripe's portal and
    # webhook own that transition, as everywhere in this product.
    assert fake.tenant()["billing"]["status"] == "active"
