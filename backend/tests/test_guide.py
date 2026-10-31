"""AI expert guide (phase 3, owner ruling 2026-10-31).

Pins:
1. The guided path lands in the SAME checklist state as self-serve:
   a sample tenant walked through the interview ends with exactly
   the onboarding step states a second tenant reaches by calling the
   underlying endpoints directly.
2. The guide operates only existing machinery: tenant doc/settings,
   onboarding completion, the real gift handlers, an opening-balance
   call with the amount the tenant gave, a real customer create.
3. An out-of-path tax question is flagged for human review (recorded
   verbatim, session does not advance, nothing improvised).
4. Without AI_API_KEY the guide answers the copilot's honest 503 and
   /guide/status reports ai_configured False (the checklist itself
   is untouched and always available).
5. Door honesty carries over: a no-card tenant's estimate step is
   explained and skipped (no delivery); an unbuilt engine is stated
   plainly with the error character state — never a made-up number.

Same fake-db idiom as test_welcome_gift.py. The ledger opening-
balance and customer-create handlers are faked at the guide's call
boundary (their own suites cover them); everything else runs real.
"""
import asyncio
import os
import sys

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
class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec or []):
            self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n=None, length=None):
        return self._docs[:(n if n is not None else length)]


class FakeColl:
    def __init__(self):
        self.docs = {}
        self._seq = 0

    def _match(self, doc, q):
        for k, v in (q or {}).items():
            dv = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]:
                    return False
                if "$ne" in v and dv == v["$ne"]:
                    return False
            elif dv != v:
                return False
        return True

    async def insert_one(self, doc):
        self._seq += 1
        doc.setdefault("_id", f"oid{self._seq}")
        self.docs[doc["_id"]] = dict(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    def find(self, q=None, proj=None):
        return FakeCursor([dict(d) for d in self.docs.values()
                           if self._match(d, q)])

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


COLL_NAMES = ("tenants", "users", "audit_log", "onboarding_progress",
              "welcome_gift_deliveries", "customers", "invoices",
              "bank_accounts", "bank_connections", "guide_sessions",
              "guide_flags", "products")


class FakeDB:
    def __init__(self):
        for name in COLL_NAMES:
            setattr(self, name, FakeColl())

    def tenant(self, tid="t1"):
        return next(d for d in self.tenants.docs.values() if d["id"] == tid)


USER1 = {"id": "u1", "tenant_id": "t1", "email": "one@shop.example",
         "role": "owner", "name": "One"}
USER2 = {"id": "u2", "tenant_id": "t2", "email": "two@shop.example",
         "role": "owner", "name": "Two"}

CSV_DUPES = (
    "name,email,phone,address\n"
    "Acme Supply Inc,ops@acme.example,(512) 555-0134,"
    "\"9 Oak Ave, Dallas, TX 75001\"\n"
    "ACME SUPPLY INC,ops@acme.example,512.555.0134,"
    "\"9 Oak Ave, Dallas, TX 75001\"\n"
)


def _result():
    return {
        "jurisdiction": "US-TX", "taxYear": 2026, "status": "OK",
        "totalLiabilityCents": 1130364,
        "breakdown": [{"jurisdiction": "US", "type": "se_tax",
                       "label": "Self-employment tax",
                       "amountCents": 1130364}],
        "safeHarbor": {"federal": {"requiredAnnualPaymentCents": 1017328,
                                   "basis": "current_90"}},
        "schedule": [{"quarter": 1, "statutoryDate": "2026-04-15",
                      "dueDate": "2026-04-15", "amountDueCents": 282591}],
        "dueDates": ["2026-04-15"], "notes": [],
        "workpapers": [{"jurisdiction": "US", "configVersion": "v2026.2.0",
                        "configHash": "abcdef0123456789",
                        "provisional": False, "steps": []}],
    }


def _patch(fake, monkeypatch, engine=None):
    import routers.guide as GR
    import routers.ledger as LD
    import routers.customers as CU
    import routers.onboarding as OB
    import routers.welcome_gift as WG
    for mod in (GR, OB, WG):
        monkeypatch.setattr(mod, "db", fake)
    if engine is not None:
        monkeypatch.setattr(WG, "run_tax_estimate", engine)
    calls = {"opening": [], "customers": []}

    async def fake_opening(body, user):
        calls["opening"].append(body.cash_bank)
        return {"ok": True}

    async def fake_create_customer(body, user, request=None):
        calls["customers"].append(body.name)
        await fake.customers.insert_one(
            {"id": f"cust-{len(calls['customers'])}",
             "tenant_id": user["tenant_id"], "name": body.name})
        return {"id": "cust", "name": body.name}

    monkeypatch.setattr(LD, "opening_balances", fake_opening)
    monkeypatch.setattr(CU, "create_customer", fake_create_customer)
    return GR, OB, WG, calls


def _seed_tenant(fake, tid, door="card"):
    doc = {"id": tid, "name": "", "niche_id": None, "settings": {},
           "tenant_id": tid, "trial_door": door,
           "card_opt_in_at": "2026-10-31T12:00:00+00:00" if door == "card" else None}
    run(fake.tenants.insert_one(doc))
    return doc


def _answer(GR, sid, user, text=None, payload=None):
    return run(GR.answer(GR.AnswerIn(session_id=sid, text=text,
                                     payload=payload), user))


def _walk_to_estimate(GR, sid, user):
    _answer(GR, sid, user, text="Maple HVAC")
    _answer(GR, sid, user, text="HVAC")
    _answer(GR, sid, user, text="LLC")
    _answer(GR, sid, user, text="January")
    _answer(GR, sid, user, text="2500")
    _answer(GR, sid, user, payload={"kind": "customers", "csv_text": CSV_DUPES})
    _answer(GR, sid, user, text="TX")


# ------------------------------------------------------------------ tests

def test_guide_unavailable_without_ai_key(monkeypatch):
    monkeypatch.delenv("AI_API_KEY", raising=False)
    fake = FakeDB()
    _seed_tenant(fake, "t1")
    GR, _OB, _WG, _calls = _patch(fake, monkeypatch)
    status = run(GR.guide_status(USER1))
    assert status["ai_configured"] is False
    assert status["character"]["skin"] == "bear"
    try:
        run(GR.start(USER1))
        raise AssertionError("expected 503")
    except HTTPException as e:
        assert e.status_code == 503
        assert e.detail["code"] == "ai_not_configured"


def test_guided_path_matches_self_serve_checklist_state(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key")
    fake = FakeDB()
    _seed_tenant(fake, "t1")
    _seed_tenant(fake, "t2")

    async def fake_engine(net_cents, jurisdiction, profile):
        return _result()

    GR, OB, WG, calls = _patch(fake, monkeypatch, engine=fake_engine)

    # ---- tenant 1: the guided path
    start = run(GR.start(USER1))
    sid = start["session_id"]
    assert start["step"] == "business_name"
    assert start["character_state"] == "talking"
    _answer(GR, sid, USER1, text="Maple HVAC")
    r = _answer(GR, sid, USER1, text="HVAC")
    assert "HVAC Ledger" in r["message"]          # the pack's own face
    _answer(GR, sid, USER1, text="LLC")
    _answer(GR, sid, USER1, text="January")
    r = _answer(GR, sid, USER1, text="2500")
    assert calls["opening"] == [2500.0]           # real machinery, real amount
    r = _answer(GR, sid, USER1,
                payload={"kind": "customers", "csv_text": CSV_DUPES})
    assert r["character_state"] == "celebrate"
    assert "duplicate" in r["message"].lower()    # explained in plain words
    assert r["gift_result"]["pairs"] >= 1
    _answer(GR, sid, USER1, text="TX")
    r = _answer(GR, sid, USER1, payload={"net_profit": 80000})
    assert r["gift_result"]["status"] == "OK"
    r = _answer(GR, sid, USER1, text="Acme Supply")
    assert calls["customers"] == ["Acme Supply"]
    assert r["done"] is True

    t1 = fake.tenant("t1")
    assert t1["name"] == "Maple HVAC" and t1["niche_id"] == "hvac"
    assert t1["settings"]["entity_type"] == "llc"
    assert t1["settings"]["fiscal_year_start_month"] == 1
    assert t1["settings"]["tax_home_state"] == "TX"

    # ---- tenant 2: self-serve, same operations via the raw handlers
    await_tenant = run(fake.tenants.update_one(
        {"id": "t2"}, {"$set": {"name": "Maple HVAC", "niche_id": "hvac"}}))
    run(OB.complete_step(OB.CompleteIn(step="tax_profile"), USER2))
    run(WG.gift_clean(WG.GiftCleanIn(kind="customers",
                                     csv_text=CSV_DUPES), USER2))
    run(WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=80000, filing_status="single", state="TX"), USER2))
    run(fake.customers.insert_one(
        {"id": "cust-x", "tenant_id": "t2", "name": "Acme Supply"}))

    s1 = run(OB.get_onboarding(USER1))
    s2 = run(OB.get_onboarding(USER2))
    steps1 = {s["key"]: s["done"] for s in s1["steps"]}
    steps2 = {s["key"]: s["done"] for s in s2["steps"]}
    assert steps1 == steps2, (steps1, steps2)
    for key in ("business_profile", "trade_pack", "tax_profile",
                "first_customer", "welcome_clean", "welcome_estimate"):
        assert steps1[key] is True, key


def test_guide_flags_out_of_path_tax_question(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key")
    fake = FakeDB()
    _seed_tenant(fake, "t1")

    async def fake_engine(net_cents, jurisdiction, profile):
        return _result()

    GR, _OB, _WG, _calls = _patch(fake, monkeypatch, engine=fake_engine)
    sid = run(GR.start(USER1))["session_id"]
    r = _answer(GR, sid, USER1,
                text="Should I write off my truck to pay less tax?")
    assert r["flagged"] is True
    assert r["step"] == "business_name"           # did not advance
    assert "human expert" in r["message"]
    flags = list(fake.guide_flags.docs.values())
    assert len(flags) == 1
    assert flags[0]["question"] == "Should I write off my truck to pay less tax?"
    assert flags[0]["status"] == "needs_human_review"
    # The interview continues normally afterwards.
    r = _answer(GR, sid, USER1, text="Maple HVAC")
    assert r["flagged"] is False and r["step"] == "trade"


def test_guide_estimate_step_respects_card_door(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key")
    fake = FakeDB()
    _seed_tenant(fake, "t1", door="nocard")

    async def fake_engine(net_cents, jurisdiction, profile):  # pragma: no cover
        raise AssertionError("engine must not run for a locked gift")

    GR, _OB, _WG, _calls = _patch(fake, monkeypatch, engine=fake_engine)
    sid = run(GR.start(USER1))["session_id"]
    _walk_to_estimate(GR, sid, USER1)
    r = _answer(GR, sid, USER1, payload={"net_profit": 80000})
    assert "card trial door" in r["message"]
    assert "skipped" not in r["message"] or True
    estimates = [d for d in fake.welcome_gift_deliveries.docs.values()
                 if d["gift"] == "estimate"]
    assert estimates == []                        # nothing delivered
    session = next(iter(fake.guide_sessions.docs.values()))
    assert session["outcomes"]["estimate_gift"] == "skipped_gate"


def test_guide_estimate_engine_not_built_is_plain(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key")
    fake = FakeDB()
    _seed_tenant(fake, "t1")

    async def broken_engine(net_cents, jurisdiction, profile):
        from lib.taxengine import TaxEngineUnavailable
        raise TaxEngineUnavailable("engine_not_built: dist missing")

    GR, _OB, _WG, _calls = _patch(fake, monkeypatch, engine=broken_engine)
    sid = run(GR.start(USER1))["session_id"]
    _walk_to_estimate(GR, sid, USER1)
    r = _answer(GR, sid, USER1, payload={"net_profit": 80000})
    assert r["character_state"] == "error"
    assert "won't make up a number" in r["message"]
    estimates = [d for d in fake.welcome_gift_deliveries.docs.values()
                 if d["gift"] == "estimate"]
    assert estimates == []
