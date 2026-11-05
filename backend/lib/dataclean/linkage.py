"""Duplicate-entity linkage over clients and vendors.

SAFETY POSTURE (the expensive failure is a FALSE merge):
  - This module only ever produces SUGGESTIONS. Merging is a human act
    performed in routers/dedupe.py, audit-logged and reversible.
  - Precision-oriented: a pair is suggested only when independent fields
    agree (name similarity AND shared contact evidence), never on a
    fuzzy name alone. Two stores of the same chain at different
    locations (different store numbers) are actively pushed apart.
  - Entities are compared ONLY within the list handed in — the router
    hands in exactly one tenant's records, so linkage can never cross
    tenants.

Two engines, one evidence shape:
  - deterministic: blocking (shared email / phone / name token / ZIP /
    phonetic key) + weighted field scoring with RapidFuzz name
    similarity.
  - Splink (MIT; Fellegi-Sunter probabilistic linkage, DuckDB backend):
    used for larger lists when importable; its match probabilities are
    attached to the SAME evidence dict the deterministic engine fills,
    so the UI explains a suggestion identically either way. On any
    Splink failure the deterministic engine answers and says so.

Power-up additions (2026-11-03 design package; every one precision-
gated by the labeled fixtures in tests/):
  - Confidence tiers: a separated REVIEW band below the suggestion
    threshold ("possible — weaker evidence"), structurally distinct in
    every result. Review pairs are never merged into the suggested
    list, and the internal console keeps storing suggestions only.
  - Cross-list mode (suggest_duplicates_cross): pairs form only across
    the A x B boundary of two lists from the same tenant. Cross mode
    adds its own caps (the warehouse dogfood's full run — 39,332 pool
    records against an 81,871-row seed — exposed both): (a) a pair
    where either side carries no street-level address (place-level
    only, or none) may not suggest on name evidence alone — it needs
    a shared contact field, or a shared website domain with identical
    name token sets, else it caps into the review band; (b) the
    population rule is derived per list, never from the union count:
    a name-only pair suggests only when the name is rare (<= 2
    carriers) in EACH list separately, because a genuine re-listing
    explains at most two carriers per side while a third carrier on
    either side means the name denotes a population there. The same
    caps run on the batched cross-job path (dataclean_jobs), which
    previously applied the within-list union frequency verbatim.
  - Nickname booster: known nickname variants (Robert/Bob, Pepe/José)
    raise the name signal ONLY when an independent contact signal
    already agrees and the rest of the name is compatible — a nickname
    match alone can never create a suggestion.
  - Phonetic blocking: a Double Metaphone key adds candidate pairs the
    token blocks would miss (Catherine/Katherine). Blocking only —
    the key contributes nothing to any score.
  - Niche token rules + DBA trade-name keys (niche_rules.py).
  - Batched scan (suggest_duplicates_batched): candidate harvesting
    over a global block index with block-size guards, for job-scale
    lists (up to 500k records) — never a naive N x N comparison.
  - Location/evidence caps (dogfood-proven on 39,332 warehouse
    records; see the pilot record in the buildout folder): pairs at
    clashing locations (ZIP / street number / city) with no shared
    contact field, pairs whose names agree only on city tokens, and
    place-level ("vacuous") addresses carrying no street are capped
    into the review band; batched scans add population rules (a
    name-only pair suggests only when the name is rare in the list).
    Every cap demotes to review — a human still sees the pair.
  - Venue-prefix street numbers (next phase, 2026-11-04): when the
    parsed parts carry no street number, the raw-text fallback also
    reads the number off the first later comma segment that begins
    with one ("Burlington, 6537 NE LOOP 820, ..." — mall / host-store
    shapes whose parsed lane is fine but whose raw records carried
    no parts, so no location clash could fire). See
    _street_number_from_text for the guard rules.
"""
import itertools
import re

from . import nicknames
from .fuzzy import similarity
from .names import company_key, entity_key
from .niche_rules import niche_key, split_dba
from .normalize import normalize_phone
from .phonetic import phonetic_keys

SUGGEST_THRESHOLD = 0.80          # deterministic score needed to suggest
SPLINK_THRESHOLD = 0.90           # Splink match probability to suggest
REVIEW_THRESHOLD = 0.65           # deterministic review-band floor
SPLINK_REVIEW_THRESHOLD = 0.75    # Splink review-band floor
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_CITY_RE = re.compile(r"(?:^|,\s*)([A-Za-z][A-Za-z .'-]*?),\s*[A-Z]{2}\b")
_STREET_TOKEN_RE = re.compile(
    r"\b(st|street|ave|avenue|blvd|rd|road|dr|drive|ln|lane|hwy|fwy|"
    r"pkwy|parkway|loop|xing|trl|trail|ct|court|way|cir|circle|pl|"
    r"place|ter|terrace|fm|highway|freeway|sq|square|row|walk|path)\b",
    re.I)


def _street_number_from_text(address):
    """Street number from raw address text, for records whose parsed
    parts carry none (uncleaned lists, failed parses). The leading
    number wins, exactly as the original fallback read it. Failing
    that, a venue name may stand before the street segment
    ("Burlington, 6537 NE LOOP 820, ...", "Albertsons, 3525 Sycamore
    School Rd, ...", locker-host and mall shapes): the first later
    comma segment that BEGINS with a number followed by a
    street-bearing token (a street type, per _STREET_TOKEN_RE) holds
    the number. The street-token requirement is what keeps unit,
    building and care-of segments ("Suite 400", "Building 4, Floor
    2", a venue's own "#4519") and bare ZIP segments from being
    misread as street numbers, and segment 0 is never scanned (a
    "PO Box 6537" is a box, not a street number). Returns None when
    no segment qualifies — an absent number, never a guess."""
    if not address:
        return None
    m = re.match(r"\s*(\d+)", address)
    if m:
        return m.group(1)
    for seg in address.split(",")[1:]:
        m = re.match(r"\s*(\d+)\s+[A-Za-z]", seg)
        if m and _STREET_TOKEN_RE.search(seg):
            return m.group(1)
    return None


def _website_domain(entity):
    """Bare host of the record's website (scheme / path / leading
    www. stripped), or None. Read for CROSS-MODE corroboration only:
    pair_evidence never scores it (a shared domain is a shared
    identifier for chains and franchises — the crm-data-cleanup law —
    so it can corroborate a place-level pair whose names are the
    identical token set, and nothing more)."""
    raw = (entity.get("website") or entity.get("url")
           or entity.get("_website") or "").strip().lower()
    if not raw:
        return None
    raw = raw.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw or None


def features(entity):
    name = entity.get("name") or ""
    ck = company_key(name)
    phone = normalize_phone(entity.get("phone") or "")
    address = entity.get("address") or ""
    parts = entity.get("address_parts")
    if not isinstance(parts, dict):
        # The external dedupe surface stringifies record values, so a
        # dict address_parts (e.g. cleanup/clean's output posted
        # verbatim) arrives as text. An unusable value counts as
        # absent — the ZIP then falls back to the address regex —
        # rather than crashing on .get() or inventing parsed data.
        parts = {}
    niche = entity.get("niche") or entity.get("trade")
    name_key = entity.get("name_key") or entity_key(name) or ck["normalized"]
    name_key = niche_key(name_key, niche)
    # DBA: 'Legal Name LLC dba Trade Name' also answers to its trade
    # name — carry it as an alternate match key (display untouched).
    alt_key = None
    _legal, trade = split_dba(name)
    if trade:
        alt_key = niche_key(entity_key(trade) or "", niche) or None
    tokens = (name_key or "").split()
    street_number = parts.get("street_number")
    if not street_number:
        street_number = _street_number_from_text(address)
    # City as a matching key only: parsed parts when present, else the
    # ", City, ST" segment of the raw address. Never displayed, never
    # written back — it exists so two records in DIFFERENT cities can
    # be told apart even when one side's address is city-only.
    city = parts.get("city")
    if not city:
        m = _CITY_RE.search(address or "")
        city = m.group(1) if m else None
    city_key = None
    if city:
        toks = str(city).lower().replace(".", "").split()
        if toks and toks[0] in ("ft", "st", "mt"):
            toks[0] = {"ft": "fort", "st": "saint", "mt": "mount"}[toks[0]]
        city_key = " ".join(toks)
    return {
        "id": entity.get("id"),
        "name_key": name_key,
        "alt_name_key": alt_key,
        "store_number": ck["store_number"],
        "email": (entity.get("email") or "").strip().lower() or None,
        "phone_digits": phone["digits"] if phone["digits"] else None,
        "address": address,
        # A vacuous address names a place, not a location: no street
        # number and no street-name token ("US", "Austin, TX, US",
        # "Mansfield, TX 76063, US" — a ZIP does not make it a street
        # address). It is not location evidence and must never read
        # as "same address".
        "address_vacuous": bool(address) and not street_number
        and not _STREET_TOKEN_RE.search(address or ""),
        "zip": parts.get("zip") or ((_ZIP_RE.search(address) or [None, None])[1]),
        "street_number": street_number,
        "city_key": city_key,
        "phonetic_keys": phonetic_keys(tokens[0]) if tokens else [],
        "website_domain": _website_domain(entity),
        "source": entity.get("_source"),
    }


def _name_similarity(fa, fb):
    """Best name similarity across the DBA alternate keys."""
    best = similarity(fa["name_key"], fb["name_key"])
    for ka in (fa.get("alt_name_key"),):
        for kb in (fb["name_key"], fb.get("alt_name_key")):
            if ka and kb:
                best = max(best, similarity(ka, kb))
    if fb.get("alt_name_key") and fa["name_key"]:
        best = max(best, similarity(fa["name_key"], fb["alt_name_key"]))
    return best


def _nickname_pair(fa, fb, email_match, phone_match):
    """(a_token, b_token) when the two names are nickname variants of
    each other AND an independent contact signal already agrees AND
    the rest of the name is compatible (a shared remaining token —
    the surname in person keys). None otherwise: a nickname match
    alone is never evidence."""
    if not (email_match or phone_match):
        return None
    for key_a, key_b in ((fa["name_key"], fb["name_key"]),
                         (fa.get("alt_name_key"), fb["name_key"]),
                         (fa["name_key"], fb.get("alt_name_key"))):
        if not key_a or not key_b:
            continue
        ta, tb = key_a.split(), key_b.split()
        for a_tok, b_tok in nicknames.variant_pairs(ta, tb):
            rest_a = {t for t in ta if t != a_tok}
            rest_b = {t for t in tb if t != b_tok}
            if rest_a & rest_b:
                return (a_tok, b_tok)
    return None


def pair_evidence(fa, fb):
    """Field-by-field evidence + deterministic score for one pair."""
    name_sim = _name_similarity(fa, fb)
    if (fa["store_number"] and fb["store_number"]
            and fa["store_number"] != fb["store_number"]):
        name_sim = min(name_sim, 0.45)   # different stores of one chain
    email_match = bool(fa["email"] and fa["email"] == fb["email"])
    phone_match = bool(fa["phone_digits"] and len(fa["phone_digits"]) >= 7
                       and fa["phone_digits"] == fb["phone_digits"])
    nick = _nickname_pair(fa, fb, email_match, phone_match)
    if nick:
        # Booster only (see module docstring): contact already agreed;
        # the nickname explains why the name score under-read the pair.
        name_sim = max(name_sim, 0.92)
    addr_sim = similarity(fa["address"], fb["address"]) if (
        fa["address"] and fb["address"]) else 0.0
    addr_live = bool(fa["address"] and fb["address"]
                     and not fa.get("address_vacuous")
                     and not fb.get("address_vacuous"))
    # Agreement on a contact field is strong evidence FOR; disagreement
    # is only weak evidence AGAINST (businesses legitimately carry
    # several emails and phone lines) — Fellegi-Sunter in spirit.
    signals = [("name", name_sim, 0.50)]
    if fa["email"] and fb["email"]:
        signals.append(("email", 1.0 if email_match else 0.35, 0.25))
    if fa["phone_digits"] and fb["phone_digits"]:
        signals.append(("phone", 1.0 if phone_match else 0.35, 0.20))
    if addr_live:
        signals.append(("address", addr_sim, 0.05))
    total_w = sum(w for _, _, w in signals)
    score = sum(v * w for _, v, w in signals) / total_w if total_w else 0.0
    why = _why(name_sim, email_match, phone_match,
               addr_sim if addr_live else 0.0)
    if nick:
        why = (why + "; " if why else "") + (
            f"names match as known nickname variants ({nick[0]}/{nick[1]})")
    # Different locations: two records whose structured address parts
    # DISAGREE (different ZIPs, or different street numbers) — or whose
    # raw addresses are near-unrelated — with no shared contact field.
    # Same name alone is a chain at two locations far more often than
    # one record written twice (the 39k-record dogfood pilot suggested
    # ~61k such pairs on name similarity alone, because raw address
    # similarity inflates on shared state/street-type tokens) — the
    # pair belongs in the review band, where a human can still merge
    # it, never in suggestions. A shared email or phone exempts the
    # pair: contact fields follow the business when it moves.
    zip_clash = bool(fa.get("zip") and fb.get("zip")
                     and fa["zip"] != fb["zip"])
    street_clash = bool(fa.get("street_number") and fb.get("street_number")
                        and fa["street_number"] != fb["street_number"])
    city_clash = bool(fa.get("city_key") and fb.get("city_key")
                      and fa["city_key"] != fb["city_key"])
    if (zip_clash or street_clash or city_clash
            or (addr_live and addr_sim < 0.30)) \
            and not email_match and not phone_match:
        score = min(score, REVIEW_THRESHOLD + 0.14)
        why = (why + "; " if why else "") + (
            "different street addresses and no shared contact field — "
            "treated as separate locations")
    # Geographic name agreement: businesses are named after their
    # city ("Austin Oaks Hospital", "San Antonio Auto Sounds"), and
    # place records carry the bare city name ("Kennedale"). When the
    # two names agree ONLY on city tokens — or one name is nothing
    # but the city — the agreement is geography, not identity.
    if not email_match and not phone_match:
        city_tokens = set((fa.get("city_key") or "").split()) | \
            set((fb.get("city_key") or "").split())
        if city_tokens:
            ta = (fa["name_key"] or "").split()
            tb = (fb["name_key"] or "").split()
            sa = [t for t in ta if t not in city_tokens]
            sb = [t for t in tb if t not in city_tokens]
            shared = set(ta) & set(tb)
            if shared and (not sa or not sb
                           or not (set(sa) & set(sb))):
                score = min(score, REVIEW_THRESHOLD + 0.14)
                why = (why + "; " if why else "") + (
                    "names agree only on the city name — geography, "
                    "not identity")
    evidence = {
        "name_similarity": round(name_sim, 3),
        "email_match": email_match,
        "phone_match": phone_match,
        # A vacuous address (no street number, no ZIP) has no
        # similarity to anything — token-set similarity would call
        # "US" a perfect subset match for every US address, which is
        # exactly the false agreement the vacuous rule exists to kill.
        "address_similarity": round(addr_sim if addr_live else 0.0, 3),
        "why": why,
    }
    return round(score, 4), evidence


def _why(name_sim, email_match, phone_match, addr_sim):
    bits = []
    if name_sim >= 0.85:
        bits.append("names are near-identical once legal suffixes and "
                    "punctuation are ignored")
    elif name_sim >= 0.6:
        bits.append("names are similar")
    if email_match:
        bits.append("same email address")
    if phone_match:
        bits.append("same phone number")
    if addr_sim >= 0.85:
        bits.append("same address")
    return "; ".join(bits) or "weak overall similarity"


def _blocks(feats):
    index = {}
    for i, f in enumerate(feats):
        keys = []
        if f["email"]:
            keys.append(("email", f["email"]))
        if f["phone_digits"] and len(f["phone_digits"]) >= 7:
            keys.append(("phone", f["phone_digits"]))
        tokens = (f["name_key"] or "").split()
        if tokens:
            keys.append(("name", tokens[0]))
        if f["zip"] and tokens:
            keys.append(("zip+name", f["zip"], tokens[0]))
        for pk in f.get("phonetic_keys") or []:
            keys.append(("phon", pk))
        alt = (f.get("alt_name_key") or "").split()
        if alt:
            keys.append(("name", alt[0]))
        for k in keys:
            index.setdefault(k, []).append(i)
    pairs = set()
    for idxs in index.values():
        for a, b in itertools.combinations(sorted(set(idxs)), 2):
            pairs.add((a, b))
    return pairs


def _gate(evidence):
    return (evidence["name_similarity"] >= 0.55
            or evidence["email_match"] or evidence["phone_match"])


def _tiered_deterministic(feats, pairs, name_freq=None):
    """Score candidate pairs once; split into suggested / review.

    name_freq (batched scans only): population frequency of each
    name_key. When BOTH records carry a hyper-common name (a chain
    brand) and nothing else agrees — no shared contact, no real
    address agreement — the shared name is a shared identifier, not
    identity (the crm-data-cleanup law), and the pair is capped into
    the review band."""
    suggested, review = [], []
    for a, b in sorted(pairs):
        score, evidence = pair_evidence(feats[a], feats[b])
        if not _gate(evidence):
            continue
        if name_freq is not None \
                and not evidence["email_match"] \
                and not evidence["phone_match"] \
                and evidence["address_similarity"] < 0.8:
            fa, fb = feats[a], feats[b]
            # A pair whose ONLY agreement is the name may suggest at
            # population scale only when the name is rare there: a
            # name carried by 3+ records is population-ambiguous (a
            # chain, a franchise, a common surname) and coincidence
            # is no longer implausible — the pair is held for review.
            # (The dogfood pilot's worst false pairs — the same
            # restaurant name in two different cities at score 1.0 —
            # are exactly this shape.)
            freq = max(name_freq.get(fa["name_key"], 0),
                       name_freq.get(fb["name_key"], 0))
            if freq > 2:
                score = min(score, REVIEW_THRESHOLD + 0.14)
                evidence = dict(evidence)
                evidence["why"] = evidence["why"] + (
                    f"; name appears {freq} times in this list with "
                    "no shared contact or address agreement — held "
                    "for review")
            elif fa.get("address_vacuous") or fb.get("address_vacuous"):
                # One side locates only to a place (city/ZIP, no
                # street). Token-set name similarity reads a subset
                # name as identical ("American Airlines" inside
                # "American Airlines Premium Lounge"), so the test
                # here is token JACCARD: when the names are not the
                # same token set, this shape is a facility of the same
                # organization (a school's field, a base's office, a
                # university's college) far more often than a
                # re-listing — held for review. Identical names and
                # real address agreement are unaffected.
                def _toks(f):
                    out = set((f["name_key"] or "").split())
                    if f.get("alt_name_key"):
                        out |= set(f["alt_name_key"].split())
                    return out
                ta, tb = _toks(fa), _toks(fb)
                jacc = len(ta & tb) / len(ta | tb) if ta and tb else 1.0
                if jacc < 0.99:
                    score = min(score, REVIEW_THRESHOLD + 0.14)
                    evidence = dict(evidence)
                    evidence["why"] = evidence["why"] + (
                        "; one record carries only a place-level "
                        "address and the names differ — held for "
                        "review")
        row = {"a_id": feats[a]["id"], "b_id": feats[b]["id"],
               "score": score, "evidence": evidence,
               "engine": "deterministic"}
        if score >= SUGGEST_THRESHOLD:
            row["band"] = "suggested"
            suggested.append(row)
        elif score >= REVIEW_THRESHOLD:
            row["band"] = "review"
            review.append(row)
    key = lambda p: -p["score"]  # noqa: E731
    return (sorted(suggested, key=key), sorted(review, key=key))


def find_duplicates(entities, threshold=SUGGEST_THRESHOLD):
    feats = [features(e) for e in entities]
    suggested, _review = _tiered_deterministic(feats, _blocks(feats))
    return [p for p in suggested if p["score"] >= threshold]


def _splink_scored_pairs(entities):
    """All Splink pairs at prob >= 0.5 with deterministic evidence.
    Raises on any problem; callers fall back."""
    import pandas as pd
    import splink.comparison_library as cl
    from splink import Linker, SettingsCreator, block_on
    from splink.internals.duckdb.database_api import DuckDBAPI

    feats = [features(e) for e in entities]
    df = pd.DataFrame({
        "unique_id": list(range(len(feats))),
        "name": [f["name_key"] or "" for f in feats],
        "email": [f["email"] or "" for f in feats],
        "phone": [f["phone_digits"] or "" for f in feats],
        "zip": [f["zip"] or "" for f in feats],
    })
    api = DuckDBAPI()
    sdf = api.register(df, table_name="entities")
    settings = SettingsCreator(
        link_type="dedupe_only",
        comparisons=[cl.JaroWinklerAtThresholds("name"),
                     cl.ExactMatch("email"), cl.ExactMatch("phone"),
                     cl.ExactMatch("zip")],
        blocking_rules_to_generate_predictions=[
            block_on("email"), block_on("phone"), block_on("name")],
        retain_intermediate_calculation_columns=False,
    )
    linker = Linker(sdf, settings, log_level=30)  # WARNING: keep server logs sane
    linker.training.estimate_probability_two_random_records_match(
        [block_on("email")], recall=0.8)
    linker.training.estimate_u_using_random_sampling(max_pairs=1e6)
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("phone"))
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("name"))
    preds = linker.inference.predict(threshold_match_probability=0.5)
    pdf = preds.as_pandas_dataframe()
    cols = list(pdf.columns)
    id_l = next(c for c in cols if c.endswith("_l") and "unique" in c)
    id_r = next(c for c in cols if c.endswith("_r") and "unique" in c)
    out = []
    for _, row in pdf.iterrows():
        prob = float(row["match_probability"])
        a, b = int(row[id_l]), int(row[id_r])
        _, evidence = pair_evidence(feats[a], feats[b])
        evidence = dict(evidence)
        evidence["splink_probability"] = round(prob, 4)
        out.append({"a_id": feats[a]["id"], "b_id": feats[b]["id"],
                    "score": round(prob, 4), "evidence": evidence,
                    "engine": "splink", "_a": a, "_b": b})
    return out


def find_duplicates_splink(entities, threshold=SPLINK_THRESHOLD):
    """Splink 5 (DuckDB) probabilistic linkage. Raises on any problem;
    suggest_duplicates catches and falls back deterministically."""
    out = []
    for p in _splink_scored_pairs(entities):
        if p["score"] < threshold:
            continue
        row = {k: p[k] for k in ("a_id", "b_id", "score", "evidence",
                                 "engine")}
        row["band"] = "suggested"
        out.append(row)
    return sorted(out, key=lambda p: -p["score"])


def _tiered_splink(entities):
    suggested, review = [], []
    for p in _splink_scored_pairs(entities):
        row = {k: p[k] for k in ("a_id", "b_id", "score", "evidence",
                                 "engine")}
        if p["score"] >= SPLINK_THRESHOLD:
            row["band"] = "suggested"
            suggested.append(row)
        elif p["score"] >= SPLINK_REVIEW_THRESHOLD:
            row["band"] = "review"
            review.append(row)
    key = lambda p: -p["score"]  # noqa: E731
    return (sorted(suggested, key=key), sorted(review, key=key))


def suggest_duplicates(entities):
    """Pick the engine: Splink for lists big enough to train on,
    deterministic otherwise (and on any Splink failure). Returns
    {"engine", "pairs" (suggested band), "review_pairs" (review band),
    optionally "engine_note"}."""
    if len(entities) >= 12:
        try:
            suggested, review = _tiered_splink(entities)
            return {"engine": "splink", "pairs": suggested,
                    "review_pairs": review}
        except Exception as exc:
            feats = [features(e) for e in entities]
            suggested, review = _tiered_deterministic(feats, _blocks(feats))
            return {"engine": "deterministic-fallback",
                    "engine_note": f"Splink unavailable: {exc}",
                    "pairs": suggested, "review_pairs": review}
    feats = [features(e) for e in entities]
    suggested, review = _tiered_deterministic(feats, _blocks(feats))
    return {"engine": "deterministic", "pairs": suggested,
            "review_pairs": review}


CROSS_CAP_SCORE = REVIEW_THRESHOLD + 0.14
# Demoted cross pairs land here: above both review floors (0.65
# deterministic / 0.75 Splink), below both suggestion thresholds
# (0.80 / 0.90) — the same landing the within-list caps use.


def _loc_missing(f):
    """No street-level location on this record: no address at all,
    or a place-level ("vacuous") one — city/ZIP only, no street."""
    return not f.get("address") or bool(f.get("address_vacuous"))


def _name_token_set(f):
    out = set((f.get("name_key") or "").split())
    if f.get("alt_name_key"):
        out |= set(f["alt_name_key"].split())
    return out


def _cross_caps(suggested, review, feats_by_id, side_by_id,
                side_counts):
    """Cross-mode precision caps, applied to the SUGGESTED band
    whichever engine scored it (the deterministic caps live inside
    pair_evidence, which the Splink path bypasses; the population
    rule needs per-list counts the within-list tiering never sees
    in cross mode). Every cap demotes into the review band with the
    reason appended to the pair's why — nothing is deleted, and a
    shared contact field (email/phone) exempts a pair from all of
    them: contact follows the business when it moves.

    side_counts: {side: Counter of name_key over that side's list}.
    The population threshold derives per list (see module docstring):
    > 2 carriers of either record's name in either list makes the
    name population-ambiguous for a name-only pair."""
    kept, demoted = [], []
    for row in suggested:
        fa = feats_by_id.get(row["a_id"])
        fb = feats_by_id.get(row["b_id"])
        if fa is None or fb is None:
            kept.append(row)   # unmappable id — leave as scored
            continue
        ev = row["evidence"]
        contact = bool(ev.get("email_match") or ev.get("phone_match"))
        rules, notes = [], []
        if not contact:
            zip_clash = bool(fa.get("zip") and fb.get("zip")
                             and fa["zip"] != fb["zip"])
            street_clash = bool(fa.get("street_number")
                                and fb.get("street_number")
                                and fa["street_number"]
                                != fb["street_number"])
            city_clash = bool(fa.get("city_key") and fb.get("city_key")
                              and fa["city_key"] != fb["city_key"])
            if zip_clash or street_clash or city_clash:
                rules.append("cross-location-clash")
                notes.append("different street addresses and no "
                             "shared contact field — treated as "
                             "separate locations")
            city_tokens = set((fa.get("city_key") or "").split()) | \
                set((fb.get("city_key") or "").split())
            if city_tokens:
                ta = (fa["name_key"] or "").split()
                tb = (fb["name_key"] or "").split()
                sa = [t for t in ta if t not in city_tokens]
                sb = [t for t in tb if t not in city_tokens]
                shared = set(ta) & set(tb)
                if shared and (not sa or not sb
                               or not (set(sa) & set(sb))):
                    rules.append("cross-geo-name")
                    notes.append("names agree only on the city name "
                                 "— geography, not identity")
            if _loc_missing(fa) or _loc_missing(fb):
                web = bool(fa.get("website_domain")
                           and fa["website_domain"]
                           == fb.get("website_domain"))
                if web:
                    ta, tb = _name_token_set(fa), _name_token_set(fb)
                    jacc = (len(ta & tb) / len(ta | tb)) if ta and tb \
                        else 1.0
                    if jacc < 0.99:
                        rules.append("cross-vacuous-facility")
                        notes.append("one record carries only a "
                                     "place-level address and the "
                                     "names differ — a facility of "
                                     "the same organization, held "
                                     "for review")
                    # Identical name token sets + a shared website
                    # domain corroborate the pair (the hardening
                    # spec's website-signal exemption).
                else:
                    rules.append("cross-vacuous-uncorroborated")
                    notes.append("one record carries only a "
                                 "place-level address and no contact "
                                 "or website signal agrees — held "
                                 "for review")
            ta_set, tb_set = _name_token_set(fa), _name_token_set(fb)
            if ta_set and tb_set \
                    and len(ta_set & tb_set) / len(ta_set | tb_set) \
                    < 0.99 \
                    and ev.get("address_similarity", 0) < 0.9:
                # Sibling locations of one brand (the dogfood's
                # Amazon Locker class): the names genuinely differ,
                # nothing agrees — brand kinship is not identity.
                # The address exemption bar is 0.9, not the
                # population rule's 0.8: address token similarity
                # counts the shared street number / city / ZIP, so
                # two different streets in one ZIP already score
                # ~0.83. Same-address pairs (DBA / franchisee
                # listings, similarity ~1.0) stay exempt.
                rules.append("cross-name-drift")
                notes.append("names differ beyond punctuation "
                             "with no shared contact or address "
                             "agreement — sibling locations of "
                             "one brand, held for review")
            if ev.get("address_similarity", 0) < 0.8 \
                    and fa.get("name_key") and fb.get("name_key"):
                sa = side_by_id.get(row["a_id"])
                sb = side_by_id.get(row["b_id"])
                amb = max(
                    side_counts.get(sa, {}).get(fa["name_key"], 0),
                    side_counts.get(sb, {}).get(fb["name_key"], 0),
                    side_counts.get(sa, {}).get(fb["name_key"], 0),
                    side_counts.get(sb, {}).get(fa["name_key"], 0))
                if amb > 2:
                    rules.append("cross-population")
                    notes.append(
                        f"name appears {amb} times in one list "
                        "with no shared contact or address "
                        "agreement — population-ambiguous, held "
                        "for review")
        if not rules:
            kept.append(row)
            continue
        new_ev = dict(ev)
        new_ev["why"] = (ev.get("why") + "; " if ev.get("why")
                         else "") + "; ".join(notes)
        new_ev["cross_caps"] = rules
        demoted.append({**row, "score": min(row["score"],
                                            CROSS_CAP_SCORE),
                        "evidence": new_ev, "band": "review"})
    key = lambda p: -p["score"]  # noqa: E731
    return (sorted(kept, key=key),
            sorted(list(review) + demoted, key=key))


def _cross_cap_context(feats, n_a):
    """(feats_by_id, side_by_id, side_counts) for _cross_caps.

    An id carried by more than one record in the union is left out
    of feats_by_id: its pairs pass through uncapped rather than
    risk a cap computed against the wrong record."""
    from collections import Counter
    side_counts = {0: Counter(), 1: Counter()}
    seen, dupes = {}, set()
    for i, f in enumerate(feats):
        side = 0 if i < n_a else 1
        if f.get("name_key"):
            side_counts[side][f["name_key"]] += 1
        if f.get("id") is None:
            continue
        if f["id"] in seen:
            dupes.add(f["id"])
        seen[f["id"]] = (side, f)
    feats_by_id = {i: f for i, (s, f) in seen.items() if i not in dupes}
    side_by_id = {i: s for i, (s, f) in seen.items() if i not in dupes}
    return feats_by_id, side_by_id, side_counts


def suggest_duplicates_cross(entities_a, entities_b,
                             label_a="records", label_b="records_b"):
    """Cross-list linkage (P2): pairs form ONLY across the A x B
    boundary. Scoring, thresholds and evidence are the within-list
    ones, so the precision posture carries over by construction.
    Evidence gains a_source / b_source labels naming the two lists."""
    a = [dict(e, _source=label_a) for e in entities_a]
    b = [dict(e, _source=label_b) for e in entities_b]
    union = a + b
    n_a = len(a)
    feats = [features(e) for e in union]
    cap_ctx = _cross_cap_context(feats, n_a)

    def finish(rows):
        out = []
        for row in rows:
            ev = dict(row["evidence"])
            ev["a_source"] = label_a
            ev["b_source"] = label_b
            out.append({**row, "evidence": ev})
        return out

    if len(union) >= 12:
        try:
            by_index = {i: e for i, e in enumerate(union)}
            scored = _splink_scored_pairs(union)
            suggested, review = [], []
            for p in scored:
                ia, ib = p["_a"], p["_b"]
                if (ia < n_a) == (ib < n_a):
                    continue  # same side — not a cross pair
                lo, hi = (ia, ib) if ia < n_a else (ib, ia)
                row = {"a_id": by_index[lo].get("id"),
                       "b_id": by_index[hi].get("id"),
                       "score": p["score"], "evidence": p["evidence"],
                       "engine": "splink"}
                if p["score"] >= SPLINK_THRESHOLD:
                    row["band"] = "suggested"
                    suggested.append(row)
                elif p["score"] >= SPLINK_REVIEW_THRESHOLD:
                    row["band"] = "review"
                    review.append(row)
            suggested, review = _cross_caps(suggested, review,
                                            *cap_ctx)
            key = lambda p: -p["score"]  # noqa: E731
            return {"engine": "splink", "mode": "cross",
                    "pairs": finish(sorted(suggested, key=key)),
                    "review_pairs": finish(sorted(review, key=key))}
        except Exception as exc:
            cand = {(i, j) for (i, j) in _blocks(feats)
                    if (i < n_a) != (j < n_a)}
            suggested, review = _tiered_deterministic(feats, cand)
            suggested, review = _cross_caps(suggested, review,
                                            *cap_ctx)
            # Candidate pairs are index-ordered (i < j) and filtered to
            # the A x B boundary, so a_id is always the A-side record.
            return {"engine": "deterministic-fallback", "mode": "cross",
                    "engine_note": f"Splink unavailable: {exc}",
                    "pairs": finish(suggested),
                    "review_pairs": finish(review)}
    cand = {(i, j) for (i, j) in _blocks(feats) if (i < n_a) != (j < n_a)}
    suggested, review = _tiered_deterministic(feats, cand)
    suggested, review = _cross_caps(suggested, review, *cap_ctx)
    return {"engine": "deterministic", "mode": "cross",
            "pairs": finish(suggested), "review_pairs": finish(review)}


# ------------------------------------------------------------ batched

_MAX_TOKEN_BLOCK = 250      # name/phonetic blocks larger than this are
_MAX_HARD_BLOCK = 5000      # skipped (and counted), never scored pairwise


def _guarded_blocks(feats):
    """Global block index with size guards -> (candidate pairs, stats).

    Candidate harvesting: blocks are built over the whole list (keys
    are cheap), but a block that would explode pairwise scoring — a
    common first name token across 500k rows — is skipped and counted
    in stats instead of silently eating the job. Email/phone blocks
    are selective by nature and get the higher hard cap."""
    index = {}
    for i, f in enumerate(feats):
        keys = []
        if f["email"]:
            keys.append(("email", f["email"]))
        if f["phone_digits"] and len(f["phone_digits"]) >= 7:
            keys.append(("phone", f["phone_digits"]))
        tokens = (f["name_key"] or "").split()
        if tokens:
            keys.append(("name", tokens[0]))
        if f["zip"] and tokens:
            keys.append(("zip+name", f["zip"], tokens[0]))
        for pk in f.get("phonetic_keys") or []:
            keys.append(("phon", pk))
        alt = (f.get("alt_name_key") or "").split()
        if alt:
            keys.append(("name", alt[0]))
        for k in keys:
            index.setdefault(k, []).append(i)
    pairs, skipped = set(), 0
    for key, idxs in index.items():
        cap = _MAX_TOKEN_BLOCK if key[0] in ("name", "phon") \
            else _MAX_HARD_BLOCK
        uniq = sorted(set(idxs))
        if len(uniq) > cap:
            skipped += 1
            continue
        for a, b in itertools.combinations(uniq, 2):
            pairs.add((a, b))
    return pairs, {"blocks": len(index), "blocks_skipped": skipped,
                   "candidates": len(pairs)}


def suggest_duplicates_batched(entities):
    """Job-scale linkage (P1): candidate harvesting + one scoring pass.

    Engine choice mirrors suggest_duplicates over the candidate-
    involved subset: Splink when that subset is big enough to train
    on, deterministic otherwise (and on any Splink failure)."""
    feats = [features(e) for e in entities]
    cand, stats = _guarded_blocks(feats)
    from collections import Counter
    name_freq = Counter()
    for f in feats:
        if f["name_key"]:
            name_freq[f["name_key"]] += 1
            name_freq[f["name_key"].split()[0]] += 1
    suggested, review = _tiered_deterministic(feats, cand, name_freq)
    involved_idx = set()
    by_id = {}
    for row in suggested + review:
        involved_idx.add(row["a_id"])
        involved_idx.add(row["b_id"])
    for f in feats:
        by_id[f["id"]] = f
    stats["entities"] = len(entities)
    if len(involved_idx) >= 12 and len(involved_idx) <= 20000:
        subset = [e for e in entities if e.get("id") in involved_idx]
        try:
            s2, r2 = _tiered_splink(subset)
            keep = {(p["a_id"], p["b_id"]) for p in suggested + review}
            keep |= {(p["b_id"], p["a_id"]) for p in suggested + review}
            s2 = [p for p in s2
                  if (p["a_id"], p["b_id"]) in keep]
            r2 = [p for p in r2
                  if (p["a_id"], p["b_id"]) in keep]
            return {"engine": "splink", "pairs": s2, "review_pairs": r2,
                    "stats": stats}
        except Exception as exc:
            return {"engine": "deterministic-fallback",
                    "engine_note": f"Splink unavailable: {exc}",
                    "pairs": suggested, "review_pairs": review,
                    "stats": stats}
    return {"engine": "deterministic", "pairs": suggested,
            "review_pairs": review, "stats": stats}
