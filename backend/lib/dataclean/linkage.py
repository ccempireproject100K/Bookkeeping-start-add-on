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
  - deterministic: blocking (shared email / phone / name token / ZIP) +
    weighted field scoring with RapidFuzz name similarity.
  - Splink (MIT; Fellegi-Sunter probabilistic linkage, DuckDB backend):
    used for larger lists when importable; its match probabilities are
    attached to the SAME evidence dict the deterministic engine fills,
    so the UI explains a suggestion identically either way. On any
    Splink failure the deterministic engine answers and says so.
"""
import itertools
import re

from .fuzzy import similarity
from .names import company_key, entity_key
from .normalize import normalize_phone

SUGGEST_THRESHOLD = 0.80          # deterministic score needed to suggest
SPLINK_THRESHOLD = 0.90           # Splink match probability to suggest
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def features(entity):
    name = entity.get("name") or ""
    ck = company_key(name)
    phone = normalize_phone(entity.get("phone") or "")
    address = entity.get("address") or ""
    parts = entity.get("address_parts") or {}
    return {
        "id": entity.get("id"),
        "name_key": entity.get("name_key") or entity_key(name) or ck["normalized"],
        "store_number": ck["store_number"],
        "email": (entity.get("email") or "").strip().lower() or None,
        "phone_digits": phone["digits"] if phone["digits"] else None,
        "address": address,
        "zip": parts.get("zip") or ((_ZIP_RE.search(address) or [None, None])[1]),
    }


def pair_evidence(fa, fb):
    """Field-by-field evidence + deterministic score for one pair."""
    name_sim = similarity(fa["name_key"], fb["name_key"])
    if (fa["store_number"] and fb["store_number"]
            and fa["store_number"] != fb["store_number"]):
        name_sim = min(name_sim, 0.45)   # different stores of one chain
    email_match = bool(fa["email"] and fa["email"] == fb["email"])
    phone_match = bool(fa["phone_digits"] and len(fa["phone_digits"]) >= 7
                       and fa["phone_digits"] == fb["phone_digits"])
    addr_sim = similarity(fa["address"], fb["address"]) if (
        fa["address"] and fb["address"]) else 0.0
    # Agreement on a contact field is strong evidence FOR; disagreement
    # is only weak evidence AGAINST (businesses legitimately carry
    # several emails and phone lines) — Fellegi-Sunter in spirit.
    signals = [("name", name_sim, 0.50)]
    if fa["email"] and fb["email"]:
        signals.append(("email", 1.0 if email_match else 0.35, 0.25))
    if fa["phone_digits"] and fb["phone_digits"]:
        signals.append(("phone", 1.0 if phone_match else 0.35, 0.20))
    if fa["address"] and fb["address"]:
        signals.append(("address", addr_sim, 0.05))
    total_w = sum(w for _, _, w in signals)
    score = sum(v * w for _, v, w in signals) / total_w if total_w else 0.0
    evidence = {
        "name_similarity": round(name_sim, 3),
        "email_match": email_match,
        "phone_match": phone_match,
        "address_similarity": round(addr_sim, 3),
        "why": _why(name_sim, email_match, phone_match, addr_sim),
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
        for k in keys:
            index.setdefault(k, []).append(i)
    pairs = set()
    for idxs in index.values():
        for a, b in itertools.combinations(sorted(set(idxs)), 2):
            pairs.add((a, b))
    return pairs


def find_duplicates(entities, threshold=SUGGEST_THRESHOLD):
    feats = [features(e) for e in entities]
    out = []
    for a, b in sorted(_blocks(feats)):
        score, evidence = pair_evidence(feats[a], feats[b])
        if score >= threshold and (evidence["name_similarity"] >= 0.55
                                   or evidence["email_match"]
                                   or evidence["phone_match"]):
            out.append({"a_id": feats[a]["id"], "b_id": feats[b]["id"],
                        "score": score, "evidence": evidence,
                        "engine": "deterministic"})
    return sorted(out, key=lambda p: -p["score"])


def find_duplicates_splink(entities, threshold=SPLINK_THRESHOLD):
    """Splink 5 (DuckDB) probabilistic linkage. Raises on any problem;
    suggest_duplicates catches and falls back deterministically."""
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
        if prob < threshold:
            continue
        a, b = int(row[id_l]), int(row[id_r])
        _, evidence = pair_evidence(feats[a], feats[b])
        evidence["splink_probability"] = round(prob, 4)
        out.append({"a_id": feats[a]["id"], "b_id": feats[b]["id"],
                    "score": round(prob, 4), "evidence": evidence,
                    "engine": "splink"})
    return sorted(out, key=lambda p: -p["score"])


def suggest_duplicates(entities):
    """Pick the engine: Splink for lists big enough to train on,
    deterministic otherwise (and on any Splink failure)."""
    if len(entities) >= 12:
        try:
            return {"engine": "splink", "pairs": find_duplicates_splink(entities)}
        except Exception as exc:
            pairs = find_duplicates(entities)
            return {"engine": "deterministic-fallback",
                    "engine_note": f"Splink unavailable: {exc}",
                    "pairs": pairs}
    return {"engine": "deterministic", "pairs": find_duplicates(entities)}
