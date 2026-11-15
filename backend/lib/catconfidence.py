"""Categorization confidence — a deterministic second look at the bank rules.

The bank rules engine (routers/banking.py) matches a transaction description
against the tenant's substring rules and suggests a category. It never said
how SURE it was. This module scores that suggestion:

  * a pattern the user taught us (a corrected review item they asked us to
    remember) -> confident
  * exactly one clean rule match                            -> confident
  * no rule matches at all                                   -> UNSURE
  * rules match but point at different categories            -> UNSURE
  * the only matches are loose ones (a very short needle, or a
    needle that lands in the middle of a longer word)        -> UNSURE

The bar is deliberately high: only genuine doubt is flagged, so the review
queue stays short and gets shorter as corrections are remembered. No LLM is
involved — same inputs, same verdict, every time.
"""
import re

# Words that carry no vendor identity; dropped before keying a pattern.
_STOPWORDS = {
    "the", "a", "an", "llc", "inc", "corp", "co", "ltd",
    "payment", "pmt", "ach", "debit", "credit", "purchase", "pos",
    "checkcard", "chkcard", "visa", "mastercard", "card", "ref",
    "txn", "transaction",
}


def pattern_key(description: str) -> str:
    """Stable vendor key for a bank description: 'SHELL OIL 57442 DALLAS'
    and 'SHELL OIL 99813' both key to 'shell oil', so one correction
    covers the next visit to the same vendor. The first number-bearing
    token (store #, reference #) ends the vendor part — anything after
    it is location/noise."""
    kept = []
    for tok in re.findall(r"[a-z0-9]+", (description or "").lower()):
        if any(ch.isdigit() for ch in tok):
            if kept:
                break
            continue
        if len(tok) >= 2 and tok not in _STOPWORDS:
            kept.append(tok)
        if len(kept) >= 4:
            break
    return " ".join(kept)


def _match_quality(desc: str, needle: str) -> str:
    """'clean' when the needle matches as its own word (or phrase) in the
    description, 'fuzzy' when it is very short or lands mid-word."""
    needle = (needle or "").lower().strip()
    if len(needle) < 4:
        return "fuzzy"
    idx = desc.find(needle)
    if idx < 0:
        return "fuzzy"
    before = desc[idx - 1] if idx > 0 else ""
    end = idx + len(needle)
    after = desc[end] if end < len(desc) else ""
    if before.isalnum() or after.isalnum():
        return "fuzzy"
    return "clean"


REASON_NO_MATCH = "We've never seen this one before — no rule matches it."
REASON_CONFLICT = "Two of your rules match this, and they point to different categories."
REASON_FUZZY = "The only match is a loose one, so we can't be sure it's right."


def evaluate(description, rule_list, learned_by_pattern=None, learning_enabled=True):
    """Score one description.

    rule_list: the tenant's bank rules (dicts with 'contains'/'category_code'),
    in the order the rules engine would try them.
    learned_by_pattern: {pattern_key: learned doc} for this tenant.
    Returns a dict: confident (bool), category_code (the confident pick, or
    None), guess_code (best guess when unsure, or None), source
    ('learned' | 'rule' | None), reason (plain words when unsure), score.
    """
    desc = (description or "").lower()

    if learning_enabled and learned_by_pattern:
        learned = learned_by_pattern.get(pattern_key(description))
        if learned:
            return {"confident": True, "category_code": learned["category_code"],
                    "guess_code": learned["category_code"], "source": "learned",
                    "reason": None, "score": 0.97}

    matches = []  # (rule, quality) in rule order
    for r in rule_list or []:
        needle = (r.get("contains") or "").lower()
        if needle and needle in desc:
            matches.append((r, _match_quality(desc, needle)))

    if not matches:
        return {"confident": False, "category_code": None, "guess_code": None,
                "source": None, "reason": REASON_NO_MATCH, "score": 0.1}

    codes = {r["category_code"] for r, _ in matches}
    first_code = matches[0][0]["category_code"]
    if len(codes) > 1:
        return {"confident": False, "category_code": None, "guess_code": first_code,
                "source": None, "reason": REASON_CONFLICT, "score": 0.3}

    if any(q == "clean" for _, q in matches):
        return {"confident": True, "category_code": first_code,
                "guess_code": first_code, "source": "rule",
                "reason": None, "score": 0.9}

    return {"confident": False, "category_code": None, "guess_code": first_code,
            "source": None, "reason": REASON_FUZZY, "score": 0.4}
