"""DataClean power-up Phase A tests (2026-11-03).

Pins, per the power-up design:
P2 cross-list dedupe — pairs only across the A x B boundary, same
    evidence shape + a_source/b_source labels, labeled-fixture
    precision == 1.00.
P5 tenant date policy (us_month_first default unchanged / day_first /
    iso_only) at engine, key-default and per-request levels; the
    conservative non-US address lane (postcode/city extraction only,
    text never rewritten; honest ok=False outside the country set).
P6 matching upgrades — nickname booster (fires only with an agreeing
    contact signal + compatible surname; adversarial pair must NOT
    suggest), phonetic blocking (pair reachable ONLY via the phonetic
    block), per-niche token rules, DBA trade-name keys, and the
    separated review band (review pairs never appear in `pairs`).
Verbatim composition: cleanup/clean output posted into a cross-list
    dedupe scan finds the known pair (the phase-3 regression motif).

Same fake-db idiom as test_dataclean_phase3.py.
"""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402

from lib.dataclean import linkage, nicknames, normalize  # noqa: E402
from lib.dataclean.addresses import parse_address  # noqa: E402
from lib.dataclean.cleaning import clean_row  # noqa: E402


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

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs.values():
            if self._match(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                for k, v in (upd.get("$inc") or {}).items():
                    d[k] = (d.get(k) or 0) + v
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))


COLL_NAMES = ("dataclean_api_keys", "dataclean_api_usage", "audit_log",
              "customers", "vendors", "products", "import_profiles")


class FakeDB:
    def __init__(self):
        for name in COLL_NAMES:
            setattr(self, name, FakeColl())


class FakeRequest:
    def __init__(self, headers=None, host="203.0.113.7"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()


USER1 = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.example",
         "role": "owner", "permissions": ["*"]}


def _patch_all(fake):
    import lib.audit as AUD
    import lib.dataclean_api as DCA
    import routers.dataclean_admin as ADM
    import routers.dataclean_api as API
    DCA.db = fake
    API.db = fake
    ADM.db = fake
    AUD.db = fake
    DCA.key_limiter._hits.clear()
    DCA.playground_limiter._hits.clear()
    return DCA, API, ADM


def _actor_for(fake, **key_kw):
    DCA, _API, ADM = _patch_all(fake)
    out = run(ADM.create_key(ADM.CreateKeyBody(**key_kw), USER1))
    return run(DCA.get_dataclean_actor(
        FakeRequest({"X-API-Key": out["key"]})))


def _expect_http(status, fn, *args, **kwargs):
    try:
        run(fn(*args, **kwargs))
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTP {status}")


def _pairset(result, key="pairs"):
    return {(p["a_id"], p["b_id"]) for p in result[key]} | \
           {(p["b_id"], p["a_id"]) for p in result[key]}


# ------------------------------------------------------- P5: date policy
def test_date_policy_default_is_byte_identical_us():
    res = normalize.normalize_date("03/04/2026")
    assert res["normalized"] == "2026-03-04"
    assert res["date_policy"] == "us_month_first"
    assert res["confidence"] == 0.7
    assert "month-first" in res["notes"][0]


def test_date_policy_day_first_flips_with_disclosure():
    res = normalize.normalize_date("03/04/2026", policy="day_first")
    assert res["normalized"] == "2026-04-03"
    assert res["date_policy"] == "day_first"
    assert "day-first" in res["notes"][0]
    # A date only readable day-first parses under both policies.
    assert normalize.normalize_date("13/04/2026")["normalized"] == "2026-04-13"
    assert normalize.normalize_date(
        "13/04/2026", policy="day_first")["normalized"] == "2026-04-13"


def test_date_policy_iso_only_refuses_ambiguous_numerics():
    res = normalize.normalize_date("03/04/2026", policy="iso_only")
    assert res["ok"] is False and res["normalized"] is None
    assert "iso_only" in res["notes"][0]
    assert normalize.normalize_date(
        "2026-03-04", policy="iso_only")["normalized"] == "2026-03-04"
    assert normalize.normalize_date(
        "March 4, 2026", policy="iso_only")["normalized"] == "2026-03-04"


def test_date_policy_unknown_policy_falls_back_to_default():
    res = normalize.normalize_date("03/04/2026", policy="european-ish")
    assert res["date_policy"] == "us_month_first"
    assert res["normalized"] == "2026-03-04"


def test_key_default_date_policy_and_request_override():
    fake = FakeDB()
    _DCA, API, _ADM = _patch_all(fake)
    actor = _actor_for(fake, date_policy="day_first")
    out = run(API.normalize(
        API.NormalizeIn(type="date", values=["03/04/2026"]), actor))
    assert out["results"][0]["normalized"] == "2026-04-03"
    out = run(API.normalize(
        API.NormalizeIn(type="date", values=["03/04/2026"],
                        date_policy="us_month_first"), actor))
    assert out["results"][0]["normalized"] == "2026-03-04"
    _expect_http(422, API.normalize,
                 API.NormalizeIn(type="date", values=["03/04/2026"],
                                 date_policy="martian"), actor)


def test_invalid_key_date_policy_rejected_at_issue():
    fake = FakeDB()
    _DCA, _API, ADM = _patch_all(fake)
    _expect_http(422, ADM.create_key,
                 ADM.CreateKeyBody(date_policy="martian"), USER1)


# ---------------------------------------------- P5: non-US address lane
def test_intl_address_lane_extracts_postcode_and_city_only():
    for addr, cc, pc, city in (
            ("10 Downing Street, London SW1A 2AA", "GB", "SW1A 2AA",
             "London"),
            ("350 Yonge Street, Toronto, ON M5B 1S5", "CA", "M5B 1S5",
             "Toronto"),
            ("1 George Street, Sydney NSW 2000", "AU", "2000", "Sydney"),
            ("Unter den Linden 1, 10117 Berlin", "DE", "10117", "Berlin")):
        res = parse_address(addr, country=cc)
        assert res["ok"], (addr, res)
        assert res["rule"] == "address:generic-intl"
        assert res["parts"]["postcode"] == pc
        assert res["parts"]["zip"] == pc
        assert res["parts"]["country"] == cc
        assert res["parts"]["city"] == city
        # The text itself is never rewritten beyond whitespace.
        assert res["normalized"] == " ".join(addr.split())


def test_intl_address_lane_honest_outside_the_set():
    res = parse_address("1 Rue de la Paix, 75002 Paris", country="XX")
    assert res["ok"] is False and res["normalized"] is None
    assert "outside the supported address set" in res["notes"][0]
    # No postcode anywhere -> not ok, nothing invented.
    res = parse_address("Somewhere Lane, London", country="GB")
    assert res["ok"] is False


def test_clean_row_intl_rule_and_us_default_unchanged():
    cleaned, changes = clean_row(
        {"name": "Thames Plumbing", "address": "221B Baker Street,  London NW1 6XE"},
        "vendors", country="GB")
    assert cleaned["address"] == "221B Baker Street, London NW1 6XE"
    assert cleaned["address_parts"]["country"] == "GB"
    rules = {c["rule"] for c in changes}
    assert "address:generic-intl" in rules
    cleaned, changes = clean_row(
        {"name": "Acme", "address": "9  Oak   Ave, Dallas, TX 75001"},
        "vendors")
    assert {c["rule"] for c in changes} >= {"address:usaddress-canonical"}
    assert cleaned["address"] == "9 Oak Ave, Dallas, TX 75001"
    assert cleaned["address_parts"]["zip"] == "75001"


def test_normalize_address_with_country_via_api():
    fake = FakeDB()
    _DCA, API, _ADM = _patch_all(fake)
    actor = _actor_for(fake)
    out = run(API.normalize(
        API.NormalizeIn(type="address", country="GB",
                        values=["10 Downing Street, London SW1A 2AA"]),
        actor))
    res = out["results"][0]
    assert res["ok"] and res["parts"]["postcode"] == "SW1A 2AA"


# ------------------------------------------------------- P6: nicknames
def test_nickname_dictionary_en_and_es():
    assert nicknames.are_variants("Robert", "Bob")
    assert nicknames.are_variants("Bob", "Bobby")
    assert nicknames.are_variants("José", "Pepe")     # accent-folded
    assert nicknames.are_variants("Paco", "Francisco")
    assert not nicknames.are_variants("Bob", "Bob")   # same spelling
    assert not nicknames.are_variants("Robert", "Richard")
    assert nicknames.canonical("Chuy") == "jesus"


def _person(pid, name, email="", phone="", address=""):
    return {"id": pid, "name": name, "email": email, "phone": phone,
            "address": address}


def test_nickname_boost_fires_with_contact_agreement():
    a = _person("p1", "Robert Garcia", email="rgarcia@shop.example")
    b = _person("p2", "Bob Garcia", email="rgarcia@shop.example")
    res = linkage.suggest_duplicates([a, b])
    assert ("p1", "p2") in _pairset(res)
    pair = res["pairs"][0]
    assert "nickname variants" in pair["evidence"]["why"]
    assert set(pair["evidence"]) >= {"name_similarity", "email_match",
                                     "phone_match", "address_similarity",
                                     "why"}


def test_nickname_no_boost_without_contact_or_surname():
    # Same nickname pair, NO shared contact signal, different surnames:
    # must not suggest — a nickname match alone is never evidence.
    a = _person("p1", "Robert Garcia", email="robert@a.example",
                phone="(512) 555-0111")
    b = _person("p2", "Bob Hernandez", email="bob@b.example",
                phone="(512) 555-0999")
    res = linkage.suggest_duplicates([a, b])
    assert ("p1", "p2") not in _pairset(res)
    assert ("p1", "p2") not in _pairset(res, "review_pairs")


# ------------------------------------------------- P6: phonetic blocking
def test_phonetic_block_reaches_otherwise_unblocked_pair():
    # Katherine Smyth / Catherine Smith share NO block key: first name
    # tokens differ, surnames differ as tokens, no shared email/phone,
    # zip+name keys differ. Only the phonetic surname key (SM0) joins
    # them; the shared address + near-identical names carry the score.
    a = _person("p1", "Katherine Smyth",
                address="9 Oak Ave, Dallas, TX 75001")
    b = _person("p2", "Catherine Smith",
                address="9 Oak Ave, Dallas, TX 75001")
    res = linkage.suggest_duplicates([a, b])
    assert ("p1", "p2") in _pairset(res), res


# ------------------------------------------------------ P6: niche rules
def test_niche_token_rules_normalize_trade_spellings():
    base = {"id": "v1", "name": "24 HR A/C Service Co",
            "email": "ops@ac.example", "phone": "", "address": ""}
    tagged = dict(base, niche="hvac")
    f = linkage.features(tagged)
    assert "24" not in f["name_key"].split()
    assert "a/c" not in f["name_key"].split()
    assert "ac" in f["name_key"].split()
    untagged = linkage.features(base)
    # Without a niche tag nothing is dropped or joined: the raw
    # normalizer output stands (incl. its "A/C" -> "a c" split).
    assert "24" in untagged["name_key"].split()
    assert "ac" not in untagged["name_key"].split()


def test_dba_trade_name_key_matches():
    a = {"id": "v1", "name": "Acme Holdings LLC dba Acme Plumbing",
         "email": "", "phone": "(512) 555-0111", "address": ""}
    b = {"id": "v2", "name": "Acme Plumbing LLC",
         "email": "", "phone": "512.555.0111", "address": ""}
    res = linkage.suggest_duplicates([a, b])
    assert ("v1", "v2") in _pairset(res), res


# ---------------------------------------------------- P6: review band
def test_review_band_is_separate_and_never_in_pairs():
    # Identical name, but every contact field DISAGREES: evidence too
    # weak to suggest, strong enough to be worth a human's glance.
    a = _person("p1", "Acme Supply Inc", email="a@one.example",
                phone="(512) 555-0111", address="1 First St, Dallas, TX")
    b = _person("p2", "Acme Supply Inc", email="b@two.example",
                phone="(512) 555-0999", address="9 Ninth St, Houston, TX")
    res = linkage.suggest_duplicates([a, b])
    assert ("p1", "p2") not in _pairset(res)
    assert ("p1", "p2") in _pairset(res, "review_pairs")
    pair = res["review_pairs"][0]
    assert pair["band"] == "review"
    assert 0.65 <= pair["score"] < 0.80


# --------------------------------------------------------- P2: cross-list
CROSS_A = [
    {"id": "a1", "name": "Acme Supply Inc", "email": "ops@acme.example",
     "phone": "(512) 555-0111", "address": "9 Oak Ave, Dallas, TX 75001"},
    {"id": "a2", "name": "Lone Star Plumbing", "email": "hi@lsp.example",
     "phone": "(512) 555-0142", "address": "77 Ranch Rd, Austin, TX 78701"},
    {"id": "a3", "name": "Acme Supply Inc", "email": "ops@acme.example",
     "phone": "(512) 555-0111", "address": "9 Oak Ave, Dallas, TX 75001"},
    {"id": "a4", "name": "Hill Country Electric", "email": "",
     "phone": "(830) 555-0100", "address": "5 Main St, Boerne, TX 78006"},
]
CROSS_B = [
    {"id": "b1", "name": "ACME SUPPLY, INC.", "email": "ops@acme.example",
     "phone": "512.555.0111", "address": "9 Oak Ave, Dallas, TX 75001"},
    {"id": "b2", "name": "Lone Star Plumbing LLC", "email": "",
     "phone": "(512) 555-0142", "address": "77 Ranch Rd, Austin, TX 78701"},
    {"id": "b3", "name": "Unrelated Ventures", "email": "x@uv.example",
     "phone": "(214) 555-0100", "address": "1 Elm St, Plano, TX 75023"},
    {"id": "b4", "name": "Hill Country Electrical", "email": "",
     "phone": "(830) 555-0199", "address": "500 Other Rd, Kerrville, TX"},
]
CROSS_TRUE = {("a1", "b1"), ("a3", "b1"), ("a2", "b2")}


def test_cross_list_pairs_only_across_the_boundary():
    res = linkage.suggest_duplicates_cross(CROSS_A, CROSS_B)
    got = _pairset(res)
    assert CROSS_TRUE <= got, (got, res)
    # a1 x a3 is a real duplicate pair WITHIN list A — cross mode must
    # never emit it.
    assert ("a1", "a3") not in got
    assert ("a1", "a3") not in _pairset(res, "review_pairs")
    for p in res["pairs"]:
        assert p["evidence"]["a_source"] == "records"
        assert p["evidence"]["b_source"] == "records_b"


def test_cross_list_fixture_precision_is_perfect():
    res = linkage.suggest_duplicates_cross(CROSS_A, CROSS_B)
    got = {(p["a_id"], p["b_id"]) for p in res["pairs"]}
    tp = len(got & CROSS_TRUE)
    precision = tp / len(got) if got else 1.0
    recall = tp / len(CROSS_TRUE)
    assert precision == 1.0, (got, res["pairs"])
    assert recall >= 0.8, (got, res["pairs"])


def test_cross_list_via_api_and_within_list_unchanged():
    fake = FakeDB()
    _DCA, API, _ADM = _patch_all(fake)
    actor = _actor_for(fake)
    out = run(API.dedupe_scan(
        API.DedupeIn(records=CROSS_A, records_b=CROSS_B, mode="cross"),
        actor))
    assert out["mode"] == "cross"
    assert ("a1", "b1") in {(p["a_id"], p["b_id"]) for p in out["pairs"]}
    assert "review_pairs" in out
    # Within-list response gains review_pairs but keeps its shape.
    out = run(API.dedupe_scan(API.DedupeIn(records=CROSS_A), actor))
    assert "pairs" in out and "review_pairs" in out
    assert ("a1", "a3") in {(p["a_id"], p["b_id"]) for p in out["pairs"]} \
        or ("a3", "a1") in {(p["a_id"], p["b_id"]) for p in out["pairs"]}
    # mode=cross without records_b is a 422, not a silent within scan.
    _expect_http(422, API.dedupe_scan,
                 API.DedupeIn(records=CROSS_A, mode="cross"), actor)


def test_verbatim_clean_output_cross_scans():
    """cleanup/clean output posted verbatim into a cross dedupe scan
    (the phase-3 regression composition, now across two lists)."""
    fake = FakeDB()
    _DCA, API, _ADM = _patch_all(fake)
    actor = _actor_for(fake)
    rows_a = [{"name": "Acme Supply Inc", "email": "OPS@acme.example",
               "phone": "(512) 555-0111",
               "address": "9 Oak Ave, Dallas, TX 75001"}]
    rows_b = [{"name": "ACME SUPPLY, INC.", "email": "ops@acme.example",
               "phone": "512.555.0111",
               "address": "9 Oak Ave, Dallas, TX 75001"}]
    cleaned_a = run(API.cleanup_clean(
        API.CleanupIn(kind="vendors", rows=rows_a), actor))["records"]
    cleaned_b = run(API.cleanup_clean(
        API.CleanupIn(kind="vendors", rows=rows_b), actor))["records"]
    out = run(API.dedupe_scan(
        API.DedupeIn(records=cleaned_a, records_b=cleaned_b, mode="cross"),
        actor))
    assert len(out["pairs"]) == 1, out
    assert out["pairs"][0]["evidence"]["email_match"] is True
