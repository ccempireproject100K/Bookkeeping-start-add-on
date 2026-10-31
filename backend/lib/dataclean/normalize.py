"""Scalar normalizers: dates, phone numbers, free-text descriptions.

Each returns the package result shape (see dataclean/__init__.py):
{shape, original, normalized, ok, rule, confidence, notes}.
`normalized` is None when the value could not be normalized — callers keep
the original in that case; a normalizer NEVER invents a value.
"""
import re
from datetime import datetime

from . import RESULT_SHAPE

# ---------------------------------------------------------------- dates
# (strptime format, label) — US month-first formats come before day-first;
# day-first is only used when month-first cannot parse the string.
_DATE_FORMATS = [
    ("%Y-%m-%d", "ISO (2026-03-04)"),
    ("%Y/%m/%d", "year-first slashes"),
    ("%Y%m%d", "compact ISO"),
    ("%m/%d/%Y", "US slashes (3/4/2026)"),
    ("%m-%d-%Y", "US dashes (3-4-2026)"),
    ("%m/%d/%y", "US slashes, 2-digit year"),
    ("%m-%d-%y", "US dashes, 2-digit year"),
    ("%d %b %Y", "4 Mar 2026"),
    ("%d %B %Y", "4 March 2026"),
    ("%b %d, %Y", "Mar 4, 2026"),
    ("%B %d, %Y", "March 4, 2026"),
    ("%d/%m/%Y", "day-first slashes (4/3/2026)"),
    ("%d-%m-%Y", "day-first dashes"),
]


def _try(fmt, s):
    try:
        return datetime.strptime(s, fmt).date()
    except (ValueError, TypeError):
        return None


def normalize_date(value):
    """Normalize a date string to ISO YYYY-MM-DD.

    Ambiguous strings (03/04/2026 parses both US and day-first) resolve
    US month-first and SAY SO in notes with lowered confidence — the
    caller (and the import preview) can show that assumption to a human.
    """
    original = value
    s = (str(value) or "").strip()
    base = {"shape": RESULT_SHAPE, "original": original, "normalized": None,
            "ok": False, "rule": "date:none", "confidence": 0.0, "notes": []}
    if not s:
        base["notes"].append("empty value")
        return base
    if "T" in s and len(s) > 10:            # ISO datetime → its date part
        s = s.split("T", 1)[0]
    parsed, fmt_used = None, None
    for fmt, label in _DATE_FORMATS:
        parsed = _try(fmt, s)
        if parsed:
            fmt_used = (fmt, label)
            break
    if not parsed:
        base["notes"].append("no known date format matched")
        return base
    fmt, label = fmt_used
    notes, confidence = [], 0.95
    if fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"):
        alt_fmt = "%d/%m/%Y" if "/" in fmt else "%d-%m-%Y"
        alt = _try(alt_fmt.replace("%y", "%Y"), s) if "%y" in fmt else _try(alt_fmt, s)
        if alt and alt != parsed:
            notes.append("ambiguous date: read as US month-first; "
                         "the day-first reading would be " + alt.isoformat())
            confidence = 0.7
    if "%y" in fmt:
        notes.append("2-digit year expanded by the standard pivot rule")
    base.update({"normalized": parsed.isoformat(), "ok": True,
                 "rule": f"date:{label}", "confidence": confidence,
                 "notes": notes, "format": label})
    return base


# ---------------------------------------------------------------- phones
def normalize_phone(value, region="US"):
    """Normalize a phone number to E.164 via libphonenumber
    (phonenumbers, Apache-2.0). `normalized` is None when the number is
    not a valid number — callers keep the original."""
    import phonenumbers

    original = value
    s = (str(value) or "").strip()
    base = {"shape": RESULT_SHAPE, "original": original, "normalized": None,
            "ok": False, "rule": "phone:libphonenumber", "confidence": 0.0,
            "notes": [], "digits": re.sub(r"\D", "", s)}
    if not s:
        base["notes"].append("empty value")
        return base
    try:
        num = phonenumbers.parse(s, None if s.startswith("+") else region)
    except phonenumbers.NumberParseException as exc:
        base["notes"].append(f"could not parse: {exc}")
        return base
    if not phonenumbers.is_valid_number(num):
        base["notes"].append("not a valid phone number for its region")
        return base
    base.update({
        "normalized": phonenumbers.format_number(
            num, phonenumbers.PhoneNumberFormat.E164),
        "ok": True, "confidence": 1.0,
        "country_code": num.country_code,
    })
    return base


# ------------------------------------------------------- txn descriptions
_SUFFIX_TOKENS = {"inc", "llc", "corp", "corporation", "co", "company",
                  "ltd", "llp", "lp", "pc", "pa", "esq"}


def normalize_description(value):
    """Match-form of a bank description / vendor string: lowercase,
    punctuation collapsed to single spaces, legal-entity suffix tokens
    dropped, leading 'the' dropped. Used for RULE MATCHING and dedupe
    keys — never shown to the user as the record's name."""
    s = re.sub(r"[^a-z0-9 ]", " ", (str(value) or "").lower())
    tokens = [t for t in s.split() if t]
    while tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    if tokens and tokens[0] == "the":
        tokens.pop(0)
    return " ".join(tokens)


def collapse_whitespace(value):
    return " ".join((str(value) or "").split())
