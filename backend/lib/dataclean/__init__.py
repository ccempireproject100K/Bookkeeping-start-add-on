"""Data-cleanup / normalization library (import cleanup layer, 2026-10-30).

Pure, framework-free helpers — NO FastAPI imports in this package — so the
Data Refinery can lift it wholesale as its normalize stage later, and so a
public cleanup API can be extracted without redesign.

Every normalizer returns a versioned result dict:

    {"shape": RESULT_SHAPE, "original": ..., "normalized": ...,
     "ok": bool, "rule": "<what fired>", "confidence": 0..1,
     "notes": [<plain-language notes>]}

Nothing here mutates its input, touches a database, or merges anything.
Suggestions are suggestions; humans approve merges (routers/dedupe.py).

Licenses of the underlying libraries (re-verified from the installed
packages' own LICENSE/metadata files at build time, 2026-10-30):
phonenumbers Apache-2.0 · cleanco MIT · probablepeople MIT ·
usaddress MIT · RapidFuzz MIT · Splink MIT (DuckDB backend MIT).
"""
RESULT_SHAPE = "dataclean.result/1"

from . import addresses, cleaning, fuzzy, linkage, mess, names, normalize, schema  # noqa: E402

__all__ = ["RESULT_SHAPE", "addresses", "cleaning", "fuzzy", "linkage",
           "mess", "names", "normalize", "schema"]
