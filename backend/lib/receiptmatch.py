"""Receipt <-> bank-transaction matching — deterministic composite scoring.

Walk item #8 (owner-approved 2026-11-14): receipt capture already extracts
{vendor, date, amount} and already lists exact-amount candidates within
+-4 days. What was missing is the layer that decides HOW GOOD a candidate
is and flags the ones that don't add up. This module is that layer:

  composite = 0.40 * amount_score + 0.25 * date_score + 0.35 * vendor_score

Vendor similarity runs on catconfidence.pattern_key() output (the vendor
normalization built for gap #7 — 'SHELL OIL 57442 DALLAS' and a receipt
from 'Shell Oil' key the same way), scored with a fuzzy string ratio.
Fuzzy primitive: RapidFuzz (MIT) when importable — it is in
requirements.txt — with a stdlib difflib fallback so matching never hard-
depends on it. No LLM anywhere: same inputs, same verdict, every time.

Verdicts:
  * strong — score >= STRONG_SCORE, amount tier exact/rounding, and (the
    caller enforces this) exactly one candidate that strong: link quietly.
  * weak   — plausible but not provable: flag for a human eye.
  * none   — nothing plausible found.

Mismatch flags (each carries a plain-words reason, house style):
  * amount_mismatch — a candidate agrees on vendor + date but its amount
    is beyond tolerance of the receipt total.
  * date_outside_window — a candidate agrees on amount + vendor but sits
    outside the match window.
  * ambiguous — two candidates score nearly identically; picking either
    would be a guess.
  * no_twin — the receipt is old enough that the bank feed should have
    caught up, and still nothing plausible exists.
"""
from datetime import date

from lib import catconfidence

try:  # RapidFuzz (MIT) — the fast path; difflib below is the fallback.
    from rapidfuzz.fuzz import token_set_ratio as _rf_token_set_ratio

    def _fuzzy_ratio(a: str, b: str) -> float:
        return _rf_token_set_ratio(a, b) / 100.0

    FUZZY_BACKEND = "rapidfuzz"
except Exception:  # pragma: no cover - exercised only without the dep
    import difflib

    def _fuzzy_ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b).ratio()

    FUZZY_BACKEND = "difflib"


# ---- tunables (one place, named) ----
STRONG_SCORE = 0.85        # at/above: eligible to link quietly
WEAK_SCORE = 0.55          # at/above: plausible — worth a human eye
AMOUNT_WEIGHT = 0.40
DATE_WEIGHT = 0.25
VENDOR_WEIGHT = 0.35
ROUNDING_TOLERANCE_CENTS = 5     # a few cents off reads as rounding
NEAR_AMOUNT_PCT = 0.01           # within 1% reads as near (fees/tax wobble)
MISMATCH_AMOUNT_PCT = 0.02       # beyond 2% off is a disagreement, not wobble
MATCH_WINDOW_DAYS = 4            # the house window from the original query
CANDIDATE_WINDOW_DAYS = 14       # how far out we even look at a bank line
NO_TWIN_MIN_AGE_DAYS = 7         # receipt older than this with no twin = flag
AMBIGUITY_GAP = 0.05             # two scores this close = a coin flip

REASON_AMOUNT_MISMATCH = (
    "The bank charge for this vendor on this date is a different amount "
    "than the receipt — one of them may be wrong, or it's a partial charge.")
REASON_DATE_OUTSIDE = (
    "A bank charge matches the amount and vendor, but it's too far from "
    "the receipt date to be the same purchase.")
REASON_AMBIGUOUS = (
    "Two bank transactions look equally likely — picking one would be a guess.")
REASON_NO_TWIN = (
    "No bank transaction matches this receipt, and enough time has passed "
    "that the bank feed should have shown it by now.")
REASON_WEAK = "This bank transaction is the closest match, but it isn't a sure one."


def _parse_date(value):
    try:
        return date.fromisoformat((value or "")[:10])
    except (TypeError, ValueError):
        return None


def amount_tier(receipt_cents: int, txn_cents: int) -> str:
    """exact | rounding | near | off — compared on absolute values
    (bank outflows are negative, receipt totals positive)."""
    diff = abs(abs(receipt_cents or 0) - abs(txn_cents or 0))
    if diff == 0:
        return "exact"
    if diff <= ROUNDING_TOLERANCE_CENTS:
        return "rounding"
    base = max(abs(receipt_cents or 0), 1)
    if diff <= base * NEAR_AMOUNT_PCT:
        return "near"
    return "off"


def amount_score(receipt_cents: int, txn_cents: int) -> float:
    tier = amount_tier(receipt_cents, txn_cents)
    if tier == "exact":
        return 1.0
    if tier == "rounding":
        return 0.92
    if tier == "near":
        return 0.80
    diff = abs(abs(receipt_cents or 0) - abs(txn_cents or 0))
    base = max(abs(receipt_cents or 0), 1)
    pct = diff / base
    if pct <= 0.05:
        return 0.45
    if pct <= 0.10:
        return 0.25
    return 0.0


def date_score(receipt_date: str, txn_date: str) -> float:
    a, b = _parse_date(receipt_date), _parse_date(txn_date)
    if a is None or b is None:
        return 0.0
    days = abs((a - b).days)
    table = {0: 1.0, 1: 0.95, 2: 0.88, 3: 0.80, 4: 0.72}
    if days in table:
        return table[days]
    if days <= 7:
        return 0.50
    if days <= 10:
        return 0.30
    if days <= CANDIDATE_WINDOW_DAYS:
        return 0.15
    return 0.0


def date_days_apart(receipt_date: str, txn_date: str):
    a, b = _parse_date(receipt_date), _parse_date(txn_date)
    if a is None or b is None:
        return None
    return abs((a - b).days)


def vendor_similarity(receipt_vendor: str, txn_description: str) -> float:
    """0..1 similarity between a receipt's vendor string and a bank line's
    description, compared on pattern_key() normalizations so store and
    reference numbers don't pollute the comparison."""
    rk = catconfidence.pattern_key(receipt_vendor or "")
    tk = catconfidence.pattern_key(txn_description or "")
    if not rk or not tk:
        return 0.0
    if rk == tk:
        return 1.0
    r_toks, t_toks = set(rk.split()), set(tk.split())
    if r_toks and (r_toks <= t_toks or t_toks <= r_toks):
        return 0.9
    best = _fuzzy_ratio(rk, tk)
    # Bank lines often trail extra words after the vendor ('GRAINGR
    # INDUSTRIAL'), so also grade the best single-token pairing.
    for rt in r_toks:
        for tt in t_toks:
            if len(rt) >= 4 and len(tt) >= 4:
                best = max(best, _fuzzy_ratio(rt, tt))
    return round(best, 4)


def score_candidate(receipt: dict, txn: dict) -> dict:
    """Score one bank transaction against one receipt's extracted fields.
    receipt: the extracted dict ({vendor, date, amount(dollars)}).
    txn: a bank_transactions doc ({txn_date, description, amount_cents}).
    Returns score + components + a plain-words reason for the score."""
    from lib.base import to_cents

    r_cents = to_cents(receipt.get("amount") or 0)
    t_cents = txn.get("amount_cents") or 0
    a = amount_score(r_cents, t_cents)
    d = date_score(receipt.get("date") or "", txn.get("txn_date") or "")
    v = vendor_similarity(receipt.get("vendor") or "",
                          txn.get("description") or "")
    score = round(AMOUNT_WEIGHT * a + DATE_WEIGHT * d + VENDOR_WEIGHT * v, 4)
    tier = amount_tier(r_cents, t_cents)
    days = date_days_apart(receipt.get("date") or "", txn.get("txn_date") or "")
    return {
        "txn_id": txn.get("id"),
        "amount_cents": t_cents,
        "score": score,
        "amount_score": round(a, 4),
        "date_score": round(d, 4),
        "vendor_score": round(v, 4),
        "amount_tier": tier,
        "date_days": days,
        "reason": _candidate_reason(tier, days, v, score),
    }


def _candidate_reason(tier: str, days, vendor: float, score: float) -> str:
    bits = []
    bits.append({"exact": "same amount",
                 "rounding": "amount matches within a few cents",
                 "near": "amount is close but not exact",
                 "off": "amount differs"}[tier])
    if days is None:
        bits.append("no date to compare")
    elif days == 0:
        bits.append("same day")
    elif days == 1:
        bits.append("one day apart")
    else:
        bits.append(f"{days} days apart")
    if vendor >= 0.9:
        bits.append("vendor matches")
    elif vendor >= 0.6:
        bits.append("vendor looks similar")
    elif vendor > 0:
        bits.append("vendor looks different")
    else:
        bits.append("no vendor on the receipt to compare")
    return "Match: " + ", ".join(bits) + f" ({round(score * 100)}%)."


def classify(receipt: dict, scored: list, today=None) -> dict:
    """Turn a scored candidate list into a verdict + mismatch flags.

    receipt: extracted dict. scored: score_candidate() dicts (any order).
    today: date for the no-twin age check (defaults to the real today so
    tests can pin it).
    Returns {verdict, best (scored dict or None), flags: [{type, reason,
    txn_id}]}."""
    today = today or date.today()
    scored = sorted(scored or [], key=lambda s: s["score"], reverse=True)
    best = scored[0] if scored else None
    flags = []

    # Amount disagreement: vendor + date agree, amount beyond tolerance.
    for s in scored:
        # tier "off" already means beyond the 1% near-band; the extra
        # MISMATCH check keeps a 1-2% wobble from reading as a disagreement.
        if (s["amount_tier"] == "off"
                and s["vendor_score"] >= 0.6
                and s["date_days"] is not None
                and s["date_days"] <= MATCH_WINDOW_DAYS
                and _beyond_mismatch_pct(receipt, s)):
            flags.append({"type": "amount_mismatch",
                          "reason": REASON_AMOUNT_MISMATCH,
                          "txn_id": s["txn_id"]})
            break

    # Date outside window: amount + vendor agree, date too far away.
    for s in scored:
        if (s["amount_tier"] in ("exact", "rounding")
                and s["vendor_score"] >= 0.6
                and s["date_days"] is not None
                and s["date_days"] > MATCH_WINDOW_DAYS):
            flags.append({"type": "date_outside_window",
                          "reason": REASON_DATE_OUTSIDE,
                          "txn_id": s["txn_id"]})
            break

    strong = [s for s in scored
              if s["score"] >= STRONG_SCORE
              and s["amount_tier"] in ("exact", "rounding")]
    if len(strong) >= 2 and (strong[0]["score"] - strong[1]["score"]) <= AMBIGUITY_GAP:
        flags.append({"type": "ambiguous", "reason": REASON_AMBIGUOUS,
                      "txn_id": strong[0]["txn_id"]})
        return {"verdict": "weak", "best": strong[0], "flags": flags}

    if len(strong) == 1 and best is strong[0]:
        return {"verdict": "strong", "best": best, "flags": flags}

    if best and best["score"] >= WEAK_SCORE:
        if not flags:
            flags.append({"type": "weak_match", "reason": REASON_WEAK,
                          "txn_id": best["txn_id"]})
        return {"verdict": "weak", "best": best, "flags": flags}

    # Nothing plausible: the no-twin flag only once the feed has had time.
    rdate = _parse_date(receipt.get("date") or "")
    if rdate is not None and (today - rdate).days >= NO_TWIN_MIN_AGE_DAYS:
        if not any(f["type"] in ("amount_mismatch", "date_outside_window")
                   for f in flags):
            flags.append({"type": "no_twin", "reason": REASON_NO_TWIN,
                          "txn_id": None})
    return {"verdict": "none", "best": best, "flags": flags}


def _beyond_mismatch_pct(receipt: dict, scored: dict) -> bool:
    from lib.base import to_cents

    r = abs(to_cents(receipt.get("amount") or 0))
    if not r:
        return False
    return abs(abs(scored.get("amount_cents") or 0) - r) / r > MISMATCH_AMOUNT_PCT
