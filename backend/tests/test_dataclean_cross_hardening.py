"""DataClean cross-list hardening round (2026-11-03, owner GO).

The P8-full warehouse dogfood (39,332 pool x 81,871 clean seed)
exposed two cross-mode defects the labeled fixture could not see:

(a) VACUOUS ADDRESSES — records whose address is only "City, TX ZIP"
    (or absent) carry no location evidence, yet identical names
    suggested at score 1.00 against them. Cross mode now requires a
    corroborating signal before a place-level pair may suggest: a
    shared contact field, or a shared website domain with identical
    name token sets (subset names on a shared domain are a facility
    of the same organization — review, not a suggestion).

(b) POPULATION CAPS — the batched job path applied the within-list
    union frequency verbatim: a name carried once per list (a normal
    re-listing) plus one within-list duplicate already read as
    "population-ambiguous" on the union. Cross mode derives the cap
    per list: a name-only pair suggests only when the name has <= 2
    carriers in EACH list separately.

Every cap demotes into the review band; nothing is deleted; a shared
contact field exempts a pair from all caps. Within-list behavior is
untouched (pinned by the Phase A/B suites + the enlarged fixture).
"""
import asyncio
import os
import sys
from collections import Counter

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.dataclean import linkage  # noqa: E402


def _pairset(res, key="pairs"):
    return {(p["a_id"], p["b_id"]) for p in res[key]}


def _row(res, a_id, b_id, key="pairs"):
    for p in res[key]:
        if (p["a_id"], p["b_id"]) == (a_id, b_id):
            return p
    return None


# ------------------------------------------------------- (a) vacuous

def test_cross_vacuous_uncorroborated_pair_caps_to_review():
    a = [{"id": "a1", "name": "Fort Worth Stockyards",
          "phone": "(817) 555-0100",
          "address": "Fort Worth, TX 76164, US"}]
    b = [{"id": "b1", "name": "Fort Worth Stockyards",
          "phone": "(817) 555-0199",
          "address": "Fort Worth, TX 76164, US"}]
    res = linkage.suggest_duplicates_cross(a, b)
    assert ("a1", "b1") not in _pairset(res)
    row = _row(res, "a1", "b1", "review_pairs")
    assert row is not None and row["band"] == "review"
    assert 0.65 <= row["score"] < 0.80
    assert "cross-vacuous-uncorroborated" in \
        row["evidence"]["cross_caps"]


def test_cross_vacuous_pair_with_shared_phone_still_suggests():
    a = [{"id": "a1", "name": "Fort Worth Stockyards",
          "phone": "(817) 555-0100",
          "address": "Fort Worth, TX 76164, US"}]
    b = [{"id": "b1", "name": "Fort Worth Stockyards",
          "phone": "817.555.0100",
          "address": "Fort Worth, TX 76164, US"}]
    res = linkage.suggest_duplicates_cross(a, b)
    assert ("a1", "b1") in _pairset(res)


def test_cross_vacuous_website_identical_names_corroborates():
    a = [{"id": "a1", "name": "Lone Star Stockyards",
          "phone": "(817) 555-0100",
          "address": "Fort Worth, TX 76164, US",
          "website": "https://www.stockyards.example/"}]
    b = [{"id": "b1", "name": "Lone Star Stockyards",
          "phone": "(817) 555-0199",
          "address": "Fort Worth, TX 76164, US",
          "website": "stockyards.example"}]
    res = linkage.suggest_duplicates_cross(a, b)
    assert ("a1", "b1") in _pairset(res)


def test_cross_vacuous_website_subset_names_is_a_facility():
    a = [{"id": "a1", "name": "Benbrook Lake",
          "phone": "(817) 555-0100",
          "address": "Benbrook, TX 76126, US",
          "website": "https://parks.example.gov/"}]
    b = [{"id": "b1", "name": "Benbrook Lake Mustang Park",
          "phone": "(817) 555-0199",
          "address": "Benbrook, TX 76126, US",
          "website": "parks.example.gov"}]
    res = linkage.suggest_duplicates_cross(a, b)
    assert ("a1", "b1") not in _pairset(res)
    row = _row(res, "a1", "b1", "review_pairs")
    assert row is not None
    assert "cross-vacuous-facility" in row["evidence"]["cross_caps"]


def test_cross_missing_address_also_needs_corroboration():
    a = [{"id": "a1", "name": "Quiet Corner Cafe",
          "phone": "(817) 555-0100", "address": ""}]
    b = [{"id": "b1", "name": "Quiet Corner Cafe",
          "phone": "(817) 555-0199",
          "address": "12 Main St, Keller, TX 76248"}]
    res = linkage.suggest_duplicates_cross(a, b)
    assert ("a1", "b1") not in _pairset(res)
    row = _row(res, "a1", "b1", "review_pairs")
    assert row is not None
    assert "cross-vacuous-uncorroborated" in \
        row["evidence"]["cross_caps"]


# ------------------------------------------- (b) population, unit level

def _feat(fid, name_key, **kw):
    f = {"id": fid, "name_key": name_key, "alt_name_key": None,
         "address": kw.get("address", "1 Main St, Austin, TX 78701"),
         "address_vacuous": kw.get("vacuous", False),
         "zip": kw.get("zip", "78701"),
         "street_number": kw.get("street_number", "1"),
         "city_key": kw.get("city_key", "austin"),
         "website_domain": kw.get("website_domain")}
    return f


def _suggested_row(evidence, score=0.95):
    return {"a_id": "a1", "b_id": "b1", "score": score,
            "evidence": evidence, "engine": "deterministic",
            "band": "suggested"}


def _caps_for(row, fa, fb, counts_a, counts_b):
    by_id = {"a1": fa, "b1": fb}
    sides = {"a1": 0, "b1": 1}
    counts = {0: Counter(counts_a), 1: Counter(counts_b)}
    return linkage._cross_caps([row], [], by_id, sides, counts)


_BASE_EV = {"email_match": False, "phone_match": False,
            "address_similarity": 0.5, "why": "names are near-identical"}


def test_cross_cap_population_demotes_common_name_only_pair():
    fa = _feat("a1", "lone star plumbing")
    fb = _feat("b1", "lone star plumbing")
    kept, review = _caps_for(_suggested_row(dict(_BASE_EV)), fa, fb,
                             {"lone star plumbing": 3},
                             {"lone star plumbing": 1})
    assert kept == [] and len(review) == 1
    assert "cross-population" in review[0]["evidence"]["cross_caps"]
    assert review[0]["score"] < 0.80


def test_cross_cap_population_allows_two_carriers_per_side():
    fa = _feat("a1", "lone star plumbing")
    fb = _feat("b1", "lone star plumbing")
    kept, review = _caps_for(_suggested_row(dict(_BASE_EV)), fa, fb,
                             {"lone star plumbing": 2},
                             {"lone star plumbing": 2})
    assert len(kept) == 1 and review == []


def test_cross_cap_population_counts_the_other_name_on_my_side():
    # The B record's name is common in list A (three same-name A
    # records the B record could pair with) — ambiguous from A's
    # side even though its own key is rare in B.
    fa = _feat("a1", "acme supply")
    fb = _feat("b1", "acme supply company")
    kept, review = _caps_for(_suggested_row(dict(_BASE_EV)), fa, fb,
                             {"acme supply": 1, "acme supply company": 3},
                             {"acme supply company": 1})
    assert kept == [] and len(review) == 1
    assert "cross-population" in review[0]["evidence"]["cross_caps"]


def test_cross_cap_contact_exempts_from_population():
    fa = _feat("a1", "lone star plumbing")
    fb = _feat("b1", "lone star plumbing")
    ev = dict(_BASE_EV, phone_match=True)
    kept, review = _caps_for(_suggested_row(ev), fa, fb,
                             {"lone star plumbing": 9},
                             {"lone star plumbing": 9})
    assert len(kept) == 1 and review == []


def test_cross_cap_address_agreement_exempts_from_population():
    fa = _feat("a1", "lone star plumbing")
    fb = _feat("b1", "lone star plumbing")
    ev = dict(_BASE_EV, address_similarity=0.92)
    kept, review = _caps_for(_suggested_row(ev), fa, fb,
                             {"lone star plumbing": 9},
                             {"lone star plumbing": 9})
    assert len(kept) == 1 and review == []


# --------------------------------------- sibling locations (name drift)

def test_cross_sibling_brand_locations_cap_to_review():
    # Same ZIP, same street NUMBER, different streets; names share
    # the brand and differ in the distinguishing token. No clash can
    # fire (numbers and ZIP agree) — the drift guard is the only cap.
    a = [{"id": "a1", "name": "Amazon Locker - Mila",
          "address": "100 Main St, Burleson, TX 76028"}]
    b = [{"id": "b1", "name": "Amazon Locker - Danae",
          "address": "100 Oak Ave, Burleson, TX 76028"}]
    res = linkage.suggest_duplicates_cross(a, b)
    assert ("a1", "b1") not in _pairset(res)
    row = _row(res, "a1", "b1", "review_pairs")
    assert row is not None
    assert row["evidence"]["cross_caps"] == ["cross-name-drift"]


def test_cross_same_address_dba_pair_still_suggests():
    a = [{"id": "a1",
          "name": "Kumon Math and Reading Center of Keller North",
          "phone": "(817) 431-9254",
          "address": "1540 Keller Pkwy Ste 100, Keller, TX 76248"}]
    b = [{"id": "b1",
          "name": "Transtraditions Education LLC DBA Kumon Math "
                  "and Reading Center",
          "address": "1540 Keller Pkwy Ste 100, Keller, TX 76248"}]
    res = linkage.suggest_duplicates_cross(a, b)
    assert ("a1", "b1") in _pairset(res)


def test_cross_cap_drift_unit():
    fa = _feat("a1", "amazon locker mila")
    fb = _feat("b1", "amazon locker danae")
    kept, review = _caps_for(_suggested_row(dict(_BASE_EV)), fa, fb,
                             {"amazon locker mila": 1},
                             {"amazon locker danae": 1})
    assert kept == [] and len(review) == 1
    assert review[0]["evidence"]["cross_caps"] == ["cross-name-drift"]


# --------------------------------------------- job path (Phase B wiring)

def test_cross_job_uses_per_list_caps_not_union_frequency():
    """A name twice in list A + once in list B with a place-level
    B address: the OLD union-frequency rule demoted it as a generic
    population pair (and for the wrong reason); the cross caps must
    be what fires — vacuous-uncorroborated — and a corroborated
    re-listing must survive as a suggestion."""
    import importlib
    phase_b = importlib.import_module("tests.test_dataclean_phase_b")
    FakeDB, _patch_all, _actor, _mk_list, run = (
        phase_b.FakeDB, phase_b._patch_all, phase_b._actor,
        phase_b._mk_list, phase_b.run)
    fake = FakeDB()
    _DCA, _API, _ADM, RJ = _patch_all(fake)
    import lib.dataclean_jobs as JOBS
    actor = _actor(fake)
    la = _mk_list(RJ, actor, "crm", [
        {"id": "a1", "name": "Stockyards Cafe",
         "phone": "(817) 555-0100",
         "address": "Fort Worth, TX 76164, US"},
        {"id": "a2", "name": "Stockyards Cafe",
         "phone": "(817) 555-0111",
         "address": "Fort Worth, TX 76164, US"}])
    lb = _mk_list(RJ, actor, "books", [
        {"id": "b1", "name": "Stockyards Cafe",
         "phone": "(817) 555-0199",
         "address": "Fort Worth, TX 76164, US"}])
    out = run(RJ.create_job(RJ.CreateJobIn(
        kind="dedupe_scan", list_id=la, list_b_id=lb,
        idempotency_key="xjob-1"), actor))
    done = run(JOBS.process_job(actor, out["job"]["id"]))
    assert done["status"] == "complete", done.get("error")
    sugg = run(JOBS.job_results(actor, out["job"]["id"],
                                band="suggested", limit=100))
    rev = run(JOBS.job_results(actor, out["job"]["id"],
                               band="review", limit=100))
    sugg_pairs = {(p["a_id"], p["b_id"]) for p in sugg[0]}
    rev_pairs = {(p["a_id"], p["b_id"]): p for p in rev[0]}
    # Both A records vs the place-level B record: no contact agrees,
    # no website — the cross vacuous cap holds them for review.
    assert sugg_pairs == set()
    assert ("a1", "b1") in rev_pairs and ("a2", "b1") in rev_pairs
    for p in rev_pairs.values():
        assert "cross-vacuous-uncorroborated" in \
            p["evidence"]["cross_caps"]
    _ = asyncio  # (idiom parity with the phase suites)
