"""Welcome gift (2026-10-31) — the two free onboarding assets.

Pins:
1. Gift A (clean): a brand-new tenant (session auth, no DataClean
   key, no plan) uploads a file and gets the Studio engine's cleaned
   records + changes + duplicate pairs back; only delivery METADATA
   is stored (never the caller's rows); the delivery is audit-logged
   and flips the onboarding + edition checklist steps; re-runs are
   recorded as further deliveries.
2. Gift B (estimate): the form runs the tax engine bridge and stores
   the packet; a stub state stays visibly NOT_IMPLEMENTED (engine
   note verbatim, in the response and in the downloaded packet);
   a state with no engine module falls back to a federal-only
   computation labelled PARTIAL with the exclusion note — never a
   silent fake state number.
3. Engine not built: 503 with code engine_not_built, no delivery
   recorded, the checklist step stays undone — an honest unavailable
   state, never a zero.

Same fake-db idiom as test_dataclean_phase3.py. The engine bridge is
monkeypatched with canned TaxResults in the engine's real shape
(tax-engine/src/types.ts); the live bridge itself is pinned by
test_bakeoff_wave4.py.
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
                if "$exists" in v and ((k in doc) != bool(v["$exists"])):
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

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))


COLL_NAMES = ("welcome_gift_deliveries", "audit_log", "tenants", "users",
              "products", "bank_accounts", "bank_connections", "contractors",
              "sales", "customers", "invoices", "onboarding_progress")


class FakeDB:
    def __init__(self):
        for name in COLL_NAMES:
            setattr(self, name, FakeColl())

    async def seed(self, coll, doc):
        doc = {"tenant_id": "t1", **doc}
        await getattr(self, coll).insert_one(doc)
        return doc


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.client = type("C", (), {"host": "203.0.113.7"})()


USER = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.example",
        "role": "owner"}

CSV_DUPES = (
    "name,email,phone,address\n"
    "Acme Supply Inc,ops@acme.example,(512) 555-0134,"
    "\"9 Oak Ave, Dallas, TX 75001\"\n"
    "ACME SUPPLY INC,ops@acme.example,512.555.0134,"
    "\"9 Oak Ave, Dallas, TX 75001\"\n"
    "Lone Star Plumbing,hello@lsp.example,(512) 555-0142,"
    "\"77 Ranch Rd, Austin, TX 78701\"\n"
)

STUB_NOTE = ("State tax for US-PA is NOT_IMPLEMENTED and is EXCLUDED "
             "from the total below — the total is federal only. "
             "A missing module is never reported as a $0 state liability.")


def _result(status="OK", jurisdiction="US-CA", notes=None, state=None):
    """A TaxResult in the engine's real shape (types.ts)."""
    fed_safe = {"totalTaxCents": 1130364, "currentYear90Cents": 1017328,
                "priorYearBasisCents": None, "priorYearMultiplier": None,
                "requiredAnnualPaymentCents": 1017328,
                "basis": "current_90", "notes": []}
    out = {
        "jurisdiction": jurisdiction, "taxYear": 2026, "status": status,
        "totalLiabilityCents": 1664746,
        "breakdown": [
            {"jurisdiction": "US", "type": "se_tax",
             "label": "Self-employment tax", "amountCents": 1130364},
            {"jurisdiction": "US", "type": "income_tax",
             "label": "Federal income tax", "amountCents": 534382},
        ],
        "federal": {"jurisdiction": "US", "status": "OK",
                    "safeHarbor": fed_safe},
        "safeHarbor": {"federal": fed_safe,
                       "requiredAnnualPaymentCents": 1017328},
        "schedule": [
            {"quarter": 1, "statutoryDate": "2026-04-15",
             "dueDate": "2026-04-15", "amountDueCents": 416187},
            {"quarter": 2, "statutoryDate": "2026-06-15",
             "dueDate": "2026-06-15", "amountDueCents": 416186},
        ],
        "dueDates": ["2026-04-15", "2026-06-15"],
        "notes": notes or [],
        "workpapers": [
            {"jurisdiction": "US", "configVersion": "v2026.2.0",
             "configHash": "abcdef0123456789", "provisional": False,
             "steps": [{"label": "Net earnings",
                        "detail": "92.35% of net profit",
                        "amountCents": 7388000}]},
        ],
    }
    if state is not None:
        out["state"] = state
    return out


def _patch(fake, monkeypatch, engine=None):
    import routers.edition as ED
    import routers.onboarding as OB
    import routers.welcome_gift as WG
    monkeypatch.setattr(WG, "db", fake)
    monkeypatch.setattr(OB, "db", fake)
    monkeypatch.setattr(ED, "db", fake)
    if engine is not None:
        monkeypatch.setattr(WG, "run_tax_estimate", engine)
    return WG, OB, ED


def _seed_opted_in_tenant(fake, **extra):
    """A tenant on the card trial door (the estimate gift's gate)."""
    doc = {"id": "t1", "name": "Maple HVAC", "niche_id": "hvac",
           "trial_door": "card",
           "card_opt_in_at": "2026-10-31T12:00:00+00:00", **extra}
    run(fake.seed("tenants", doc))
    return doc


def _expect_http(status, coro_fn):
    try:
        run(coro_fn())
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTP {status}")


# ------------------------------------------------------------- gift A

def test_gift_clean_delivers_and_marks_checklists(monkeypatch):
    fake = FakeDB()
    run(fake.seed("tenants", {"id": "t1", "name": "Maple HVAC",
                              "niche_id": "hvac"}))
    WG, OB, ED = _patch(fake, monkeypatch)

    out = run(WG.gift_clean(WG.GiftCleanIn(kind="customers",
                                           csv_text=CSV_DUPES), USER))
    assert out["rows"] == 3
    assert len(out["records"]) == 3
    assert out["records"][0]["email"] == "ops@acme.example"
    assert out["dedupe"]["pairs"], "the two Acme rows must pair up"
    pair = out["dedupe"]["pairs"][0]
    assert "evidence" in pair and "score" in pair
    assert out["delivery"]["gift"] == "clean"

    # Only metadata is stored — never the caller's rows.
    stored = list(fake.welcome_gift_deliveries.docs.values())
    assert len(stored) == 1
    assert "records" not in stored[0] and "csv_text" not in stored[0]
    assert "Acme" not in str(stored[0])
    assert stored[0]["rows"] == 3 and stored[0]["duplicate_pairs"] >= 1

    # The delivery is audit-logged like other product events.
    actions = [d.get("action") for d in fake.audit_log.docs.values()]
    assert "welcome_gift.clean_delivered" in actions

    # Both checklists now read the gift as done.
    state = run(OB.get_onboarding(USER))
    by_key = {s["key"]: s["done"] for s in state["steps"]}
    assert by_key["welcome_clean"] and not by_key["welcome_estimate"]
    checklist = run(ED.setup_checklist(FakeRequest(), USER))
    items = {i["id"]: i for i in checklist["items"]}
    assert items["welcome_clean"]["done"]
    assert items["welcome_clean"]["link"] == "/welcome-gift"
    assert not items["welcome_estimate"]["done"]

    # Re-running is harmless and recorded as a second delivery.
    run(WG.gift_clean(WG.GiftCleanIn(kind="customers",
                                     csv_text=CSV_DUPES), USER))
    status = run(WG.get_welcome_gift(USER))
    assert status["gifts"]["clean"]["deliveries"] == 2
    assert status["gifts"]["clean"]["done"]


def test_gift_clean_validation(monkeypatch):
    fake = FakeDB()
    WG, _OB, _ED = _patch(fake, monkeypatch)
    e = _expect_http(422, lambda: WG.gift_clean(
        WG.GiftCleanIn(kind="widgets", csv_text="a,b\n1,2"), USER))
    assert e.detail["code"] == "unknown_kind"
    assert not fake.welcome_gift_deliveries.docs


def test_gift_clean_row_cap(monkeypatch):
    fake = FakeDB()
    WG, _OB, _ED = _patch(fake, monkeypatch)
    monkeypatch.setattr(WG, "MAX_ROWS", 2)
    csv3 = "name,email\nA,a@x.example\nB,b@x.example\nC,c@x.example\n"
    e = _expect_http(413, lambda: WG.gift_clean(
        WG.GiftCleanIn(kind="customers", csv_text=csv3), USER))
    assert e.detail["code"] == "payload_too_large"


# ------------------------------------------------------------- gift B

def test_gift_estimate_ok_and_packet(monkeypatch):
    fake = FakeDB()
    _seed_opted_in_tenant(fake)
    calls = []

    async def fake_engine(net_cents, jurisdiction, profile):
        calls.append((net_cents, jurisdiction, profile))
        return _result()

    WG, OB, _ED = _patch(fake, monkeypatch, engine=fake_engine)
    out = run(WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=80000, filing_status="single", state="CA",
        w2_wages=10000), USER))
    assert calls == [(8000000, "US-CA",
                      {"filingStatus": "single", "w2Wages": 10000})]
    assert out["status"] == "OK"
    assert out["total_liability_cents"] == 1664746

    stored = list(fake.welcome_gift_deliveries.docs.values())
    assert len(stored) == 1 and stored[0]["gift"] == "estimate"
    assert stored[0]["result"]["totalLiabilityCents"] == 1664746
    actions = [d.get("action") for d in fake.audit_log.docs.values()]
    assert "welcome_gift.estimate_delivered" in actions

    state = run(OB.get_onboarding(USER))
    by_key = {s["key"]: s["done"] for s in state["steps"]}
    assert by_key["welcome_estimate"]

    # The packet: totals, breakdown, schedule, workpapers — in writing.
    resp = run(WG.download_estimate_packet(out["delivery_id"], "csv", USER))
    text = resp.body.decode()
    assert "Total estimated liability" in text
    assert "$16,647.46" in text
    assert "Self-employment tax" in text
    assert "2026-04-15" in text
    assert "config v2026.2.0" in text
    resp = run(WG.download_estimate_packet(out["delivery_id"], "pdf", USER))
    assert resp.body[:4] == b"%PDF"
    _expect_http(404, lambda: WG.download_estimate_packet(
        "nope", "csv", USER))


def test_gift_estimate_stub_state_stays_not_implemented(monkeypatch):
    fake = FakeDB()
    _seed_opted_in_tenant(fake)

    async def fake_engine(net_cents, jurisdiction, profile):
        assert jurisdiction == "US-PA"
        return _result(status="PARTIAL", jurisdiction="US-PA",
                       notes=[STUB_NOTE],
                       state={"jurisdiction": "US-PA",
                              "status": "NOT_IMPLEMENTED"})

    WG, _OB, _ED = _patch(fake, monkeypatch, engine=fake_engine)
    out = run(WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=50000, filing_status="single", state="PA"), USER))
    assert out["status"] == "PARTIAL"
    assert STUB_NOTE in out["result"]["notes"]
    resp = run(WG.download_estimate_packet(out["delivery_id"], "csv", USER))
    text = resp.body.decode()
    assert "NOT_IMPLEMENTED" in text
    assert "implemented parts only" in text


def test_gift_estimate_unregistered_state_is_federal_only_labelled(
        monkeypatch):
    fake = FakeDB()
    _seed_opted_in_tenant(fake)
    calls = []

    async def fake_engine(net_cents, jurisdiction, profile):
        calls.append(jurisdiction)
        return _result(jurisdiction="US")

    WG, _OB, _ED = _patch(fake, monkeypatch, engine=fake_engine)
    out = run(WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=50000, filing_status="single", state="CO"), USER))
    # The engine ran federal-only; the gift labels it PARTIAL with the
    # exclusion note instead of inventing a Colorado number.
    assert calls == ["US"]
    assert out["requested_jurisdiction"] == "US-CO"
    assert out["computed_jurisdiction"] == "US"
    assert out["status"] == "PARTIAL"
    assert "NOT_IMPLEMENTED" in out["result"]["notes"][0]
    assert "US-CO" in out["result"]["notes"][0]


def test_gift_estimate_engine_not_built_is_honest_503(monkeypatch):
    fake = FakeDB()
    _seed_opted_in_tenant(fake)

    async def broken_engine(net_cents, jurisdiction, profile):
        from lib.taxengine import TaxEngineUnavailable
        raise TaxEngineUnavailable(
            "engine_not_built: tax-engine dist missing — run: "
            "cd tax-engine && npm ci && npm run build")

    WG, OB, _ED = _patch(fake, monkeypatch, engine=broken_engine)
    e = _expect_http(503, lambda: WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=50000, filing_status="single", state="CA"), USER))
    assert e.detail["code"] == "engine_not_built"
    # Nothing was delivered, nothing recorded, step stays undone.
    assert not fake.welcome_gift_deliveries.docs
    state = run(OB.get_onboarding(USER))
    by_key = {s["key"]: s["done"] for s in state["steps"]}
    assert not by_key["welcome_estimate"]


def test_gift_estimate_locked_until_card_opt_in(monkeypatch):
    """The trial-doors split (owner ruling 2026-10-31): the clean is
    ungated, the estimate packet rides the card opt-in. Locked before,
    unlocked the moment the opt-in is on record."""
    fake = FakeDB()
    run(fake.seed("tenants", {"id": "t1", "name": "Maple HVAC",
                              "niche_id": "hvac", "trial_door": "nocard",
                              "card_opt_in_at": None}))
    calls = []

    async def fake_engine(net_cents, jurisdiction, profile):
        calls.append(jurisdiction)
        return _result()

    WG, _OB, _ED = _patch(fake, monkeypatch, engine=fake_engine)

    # The clean runs with no trial/card state at all.
    out = run(WG.gift_clean(WG.GiftCleanIn(kind="customers",
                                           csv_text=CSV_DUPES), USER))
    assert out["rows"] == 3

    # The estimate is locked: 403 with the plain reason, engine never
    # called, nothing delivered.
    meta = run(WG.get_welcome_gift(USER))
    assert meta["estimate_unlocked"] is False
    e = _expect_http(403, lambda: WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=50000, filing_status="single", state="CA"), USER))
    assert e.detail["code"] == "card_opt_in_required"
    assert calls == []
    assert [d for d in fake.welcome_gift_deliveries.docs.values()
            if d["gift"] == "estimate"] == []

    # The opt-in lands (door upgrade) -> the same call succeeds.
    coll = fake.tenants
    doc = next(iter(coll.docs.values()))
    doc["card_opt_in_at"] = "2026-10-31T13:00:00+00:00"
    doc["trial_door"] = "card"
    meta = run(WG.get_welcome_gift(USER))
    assert meta["estimate_unlocked"] is True
    out = run(WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=80000, filing_status="single", state="CA"), USER))
    assert out["status"] == "OK" and calls == ["US-CA"]


def test_gift_estimate_validation(monkeypatch):
    fake = FakeDB()
    _seed_opted_in_tenant(fake)

    async def fake_engine(net_cents, jurisdiction, profile):  # pragma: no cover
        return _result()

    WG, _OB, _ED = _patch(fake, monkeypatch, engine=fake_engine)
    e = _expect_http(422, lambda: WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=100, filing_status="corp"), USER))
    assert e.detail["code"] == "invalid_filing_status"
    e = _expect_http(422, lambda: WG.gift_estimate(WG.GiftEstimateIn(
        net_profit=100, state="Texas"), USER))
    assert e.detail["code"] == "invalid_state"
