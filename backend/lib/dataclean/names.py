"""Name parsing and match keys.

Display names are NEVER rewritten by this module — it derives *keys*:
  - company_key(): cleanco (MIT) strips the legal suffix ("Inc", "LLC",
    "Corp"...) and normalize_description collapses case/punctuation, so
    "The Home Depot, Inc.", "HOME DEPOT INC" and "Home Depot, LLC" share
    one key while the stored display names stay untouched.
  - parse_name(): probablepeople (MIT) tags a raw string as a Person,
    Corporation or Household and splits it into parts (surname/given
    names for people; corporation name + legal type for companies).
"""
import re

from . import RESULT_SHAPE
from .normalize import collapse_whitespace, normalize_description

_STORE_NO = re.compile(r"#\s*(\d+)")


def company_key(name):
    """Match key for a company name + extracted store number, if any."""
    from cleanco import basename

    raw = collapse_whitespace(name)
    m = _STORE_NO.search(raw)
    store_no = m.group(1) if m else None
    # Drop the "#4412" fragment first — trailing text stops cleanco from
    # seeing (and stripping) the legal suffix in front of it.
    core = _STORE_NO.sub("", raw).strip(" ,") if store_no else raw
    base = basename(core) if core else ""
    return {"shape": RESULT_SHAPE, "original": name,
            "normalized": normalize_description(base or core),
            "ok": bool(raw), "rule": "name:cleanco-basename",
            "confidence": 0.95 if base and base != core else 0.8,
            "notes": [], "store_number": store_no}


def parse_name(name):
    """probablepeople tag: Person / Corporation / Household + parts."""
    import probablepeople

    raw = collapse_whitespace(name)
    base = {"shape": RESULT_SHAPE, "original": name, "normalized": None,
            "ok": False, "rule": "name:probablepeople", "confidence": 0.0,
            "notes": [], "parts": {}, "type": None}
    if not raw:
        base["notes"].append("empty value")
        return base
    try:
        parts, kind = probablepeople.tag(raw)
    except Exception as exc:  # parser is statistical; never break a caller
        base["notes"].append(f"name parser failed: {exc}")
        base["normalized"] = company_key(raw)["normalized"]
        return base
    base["parts"] = dict(parts)
    base["type"] = kind
    if kind == "Corporation":
        base["normalized"] = company_key(raw)["normalized"]
        base["confidence"] = 0.9
    elif kind == "Person":
        surname = str(parts.get("Surname", "")).lower()
        given = str(parts.get("GivenName", "")).lower()
        base["normalized"] = f"{surname} {given}".strip()
        base["confidence"] = 0.9
    else:  # Household etc. — fall back to the company-style key
        base["normalized"] = normalize_description(raw)
        base["confidence"] = 0.6
    base["ok"] = bool(base["normalized"])
    return base


def entity_key(name):
    """One match key for a client/vendor name of unknown kind."""
    parsed = parse_name(name)
    if parsed["ok"]:
        return parsed["normalized"]
    return company_key(name)["normalized"]
