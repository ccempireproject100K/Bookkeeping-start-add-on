"""Fuzzy string similarity via RapidFuzz (MIT).

Scores are 0..1. token_set_ratio is order- and duplication-insensitive,
which is the right shape for business names ("Depot Home" == "Home
Depot"); plain ratio is kept for short strings where order matters.
"""
from rapidfuzz import fuzz


def similarity(a, b):
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(str(a), str(b)) / 100.0


def strict_similarity(a, b):
    if not a or not b:
        return 0.0
    return fuzz.ratio(str(a), str(b)) / 100.0
