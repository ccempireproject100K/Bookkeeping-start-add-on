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
# Tenant-configurable reading policy for numeric dates (power-up P5).
# A policy never changes what an UNAMBIGUOUS date means; it only decides
# how an ambiguous numeric string (03/04/2026) is read — and the reading
# is always disclosed in notes with lowered confidence, under every
# policy. `iso_only` refuses ambiguous numerics outright.
DATE_POLICIES = ("us_month_first", "day_first", "iso_only")
DEFAULT_DATE_POLICY = "us_month_first"

# (strptime format, label) grouped by family. Unambiguous formats (ISO,
# year-first, month-name) are policy-independent and always tried first.
_FORMATS_UNAMBIGUOUS = [
    ("%Y-%m-%d", "ISO (2026-03-04)"),
    ("%Y/%m/%d", "year-first slashes"),
    ("%Y%m%d", "compact ISO"),
    ("%d %b %Y", "4 Mar 2026"),
    ("%d %B %Y", "4 March 2026"),
    ("%b %d, %Y", "Mar 4, 2026"),
    ("%B %d, %Y", "March 4, 2026"),
]
_FORMATS_US = [
    ("%m/%d/%Y", "US slashes (3/4/2026)"),
    ("%m-%d-%Y", "US dashes (3-4-2026)"),
    ("%m/%d/%y", "US slashes, 2-digit year"),
    ("%m-%d-%y", "US dashes, 2-digit year"),
]
_FORMATS_DF = [
    ("%d/%m/%Y", "day-first slashes (4/3/2026)"),
    ("%d-%m-%Y", "day-first dashes"),
]
# The historical single-list order (US before day-first) — the default
# policy reproduces it exactly.
_DATE_FORMATS = (_FORMATS_UNAMBIGUOUS[:3] + _FORMATS_US
                 + _FORMATS_UNAMBIGUOUS[3:] + _FORMATS_DF)
_US_FMT_SET = {f for f, _ in _FORMATS_US}
_DF_FMT_SET = {f for f, _ in _FORMATS_DF}


def _formats_for(policy):
    if policy == "day_first":
        return _FORMATS_UNAMBIGUOUS + _FORMATS_DF + _FORMATS_US
    if policy == "iso_only":
        return list(_FORMATS_UNAMBIGUOUS)
    return list(_DATE_FORMATS)


def _counterpart(fmt):
    """The other-policy reading of a numeric format string, or None."""
    if fmt in _US_FMT_SET:
        return fmt.replace("%m", "@").replace("%d", "%m").replace("@", "%d")
    if fmt in _DF_FMT_SET:
        return fmt.replace("%d", "@").replace("%m", "%d").replace("@", "%m")
    return None


def _try(fmt, s):
    try:
        return datetime.strptime(s, fmt).date()
    except (ValueError, TypeError):
        return None


def normalize_date(value, policy=DEFAULT_DATE_POLICY):
    """Normalize a date string to ISO YYYY-MM-DD under a date policy.

    Ambiguous strings (03/04/2026 parses both US and day-first) resolve
    per the tenant's policy and SAY SO in notes with lowered
    confidence — the caller (and the import preview) can show that
    assumption to a human. Under `iso_only` an ambiguous numeric string
    is refused (ok=False) rather than guessed.
    """
    if policy not in DATE_POLICIES:
        policy = DEFAULT_DATE_POLICY
    original = value
    s = (str(value) or "").strip()
    base = {"shape": RESULT_SHAPE, "original": original, "normalized": None,
            "ok": False, "rule": "date:none", "confidence": 0.0, "notes": [],
            "date_policy": policy}
    if not s:
        base["notes"].append("empty value")
        return base
    if "T" in s and len(s) > 10:            # ISO datetime → its date part
        s = s.split("T", 1)[0]
    parsed, fmt_used = None, None
    for fmt, label in _formats_for(policy):
        parsed = _try(fmt, s)
        if parsed:
            fmt_used = (fmt, label)
            break
    if not parsed:
        if policy == "iso_only" and any(
                _try(f, s) for f, _ in _FORMATS_US + _FORMATS_DF):
            base["notes"].append(
                "ambiguous numeric date refused under the iso_only "
                "policy; supply ISO format or relax the policy")
        else:
            base["notes"].append("no known date format matched")
        return base
    fmt, label = fmt_used
    notes, confidence = [], 0.95
    alt_fmt = _counterpart(fmt)
    if alt_fmt:
        alt_probe = alt_fmt.replace("%y", "%Y") if "%y" in alt_fmt else alt_fmt
        alt = _try(alt_probe, s)
        if alt and alt != parsed:
            if fmt in _US_FMT_SET:
                notes.append("ambiguous date: read as US month-first; "
                             "the day-first reading would be "
                             + alt.isoformat())
            else:
                notes.append("ambiguous date: read as day-first; "
                             "the US month-first reading would be "
                             + alt.isoformat())
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
