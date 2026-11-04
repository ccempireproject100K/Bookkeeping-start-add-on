"""Address parsing: US via usaddress (MIT); a conservative non-US lane.

parse_address splits a free-form US address into components and rebuilds
a canonical single line ("123 Main St Apt 4, Austin, TX 78701"). Parse
failures (repeated labels, non-US shapes) return ok=False with the
original untouched — the caller keeps the raw address.

Non-US lane (power-up P5): when a record carries a country in the
supported set, a country-aware *splitting* pass extracts the postcode
and the city segment and reports them as parts — it NEVER rewrites a
foreign address beyond whitespace collapsing (we do not know the local
canonical form, and inventing one would break the never-invent-a-value
rule). Results are labeled rule `address:generic-intl` at lower
confidence. A country outside the supported set gets the honest
ok=False "format check only" answer. Full international parsing
(libpostal-class) is deliberately NOT used — see the design package.
"""
import re

from . import RESULT_SHAPE
from .normalize import collapse_whitespace

# Postcode patterns for the supported non-US lane: the launch-market
# set named in the design (GB/CA/AU/DE/FR/ES) plus the market packs
# whose postcodes have an unambiguous shape. Countries absent from this
# table are honestly out of scope for parsing.
INTL_POSTCODE_RES = {
    "GB": re.compile(r"\b[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}\b"),
    "CA": re.compile(r"\b[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d\b"),
    "AU": re.compile(r"\b\d{4}\b"),
    "NZ": re.compile(r"\b\d{4}\b"),
    "DE": re.compile(r"\b\d{5}\b"),
    "FR": re.compile(r"\b\d{5}\b"),
    "ES": re.compile(r"\b\d{5}\b"),
    "IT": re.compile(r"\b\d{5}\b"),
    "FI": re.compile(r"\b\d{5}\b"),
    "MX": re.compile(r"\b\d{5}\b"),
    "NL": re.compile(r"\b\d{4}\s?[A-Za-z]{2}\b"),
    "BE": re.compile(r"\b\d{4}\b"),
    "AT": re.compile(r"\b\d{4}\b"),
    "CH": re.compile(r"\b\d{4}\b"),
    "NO": re.compile(r"\b\d{4}\b"),
    "DK": re.compile(r"\b\d{4}\b"),
    "PT": re.compile(r"\b\d{4}-\d{3}\b"),
    "BR": re.compile(r"\b\d{5}-\d{3}\b"),
    "PL": re.compile(r"\b\d{2}-\d{3}\b"),
    "SE": re.compile(r"\b\d{3}\s?\d{2}\b"),
    "IE": re.compile(r"\b[A-Za-z0-9]{3}\s[A-Za-z0-9]{4}\b"),
}
SUPPORTED_INTL_COUNTRIES = sorted(INTL_POSTCODE_RES)


def _parse_intl(value, country):
    """Conservative splitting for a supported non-US country."""
    raw = collapse_whitespace(value)
    base = {"shape": RESULT_SHAPE, "original": value, "normalized": None,
            "ok": False, "rule": "address:generic-intl", "confidence": 0.0,
            "notes": [], "parts": {}, "country": country}
    if not raw:
        base["notes"].append("empty value")
        return base
    rx = INTL_POSTCODE_RES[country]
    matches = list(rx.finditer(raw))
    if not matches:
        base["notes"].append(
            f"no {country} postcode found; format check only, "
            "address left as-is")
        return base
    m = matches[-1]
    postcode = collapse_whitespace(m.group(0)).upper()
    # City: the comma segment that holds the postcode, minus the
    # postcode itself; if that segment is only the postcode, the
    # previous segment. Anything shakier is left unclaimed.
    segments = [collapse_whitespace(s) for s in raw.split(",")]
    city = None
    for i, seg in enumerate(segments):
        if m.group(0) in seg or postcode in collapse_whitespace(seg).upper():
            rest = rx.sub("", seg).strip(" -")
            rest = collapse_whitespace(rest)
            # A trailing 2-3 letter all-caps token is a state/province
            # code riding with the city ("Sydney NSW 2000") — drop it.
            toks = rest.split()
            if len(toks) >= 2 and toks[-1].isupper() \
                    and toks[-1].isalpha() and len(toks[-1]) <= 3:
                rest = " ".join(toks[:-1])
            # A remainder that is ONLY a province/state code ("ON
            # M5B 1S5") is not a city — fall through to the previous
            # segment rather than claim it.
            if rest and not (len(rest) <= 3 and rest.isupper()
                             and rest.isalpha()):
                city = rest
            elif i > 0:
                city = segments[i - 1] or None
            break
    parts = {"zip": postcode, "postcode": postcode, "country": country}
    if city:
        parts["city"] = city
    base["parts"] = parts
    base["address_type"] = "int"
    # The canonical line is the collapsed original — components were
    # extracted for matching, the customer's own formatting is kept.
    base.update({"normalized": raw, "ok": True, "confidence": 0.6})
    base["notes"].append(
        f"{country} address: postcode/city extracted for matching; "
        "the address text itself is never rewritten")
    return base


def parse_address(value, country=None):
    cc = (str(country).strip().upper() or None) if country else None
    if cc in (None, "US", "USA"):
        return _parse_us(value)
    if cc in INTL_POSTCODE_RES:
        return _parse_intl(value, cc)
    base = {"shape": RESULT_SHAPE, "original": value, "normalized": None,
            "ok": False, "rule": "address:generic-intl", "confidence": 0.0,
            "notes": [f"country {cc} is outside the supported address "
                      "set; format check only, address left as-is"],
            "parts": {}, "country": cc}
    return base


def _parse_us(value):
    import usaddress

    raw = collapse_whitespace(value)
    base = {"shape": RESULT_SHAPE, "original": value, "normalized": None,
            "ok": False, "rule": "address:usaddress", "confidence": 0.0,
            "notes": [], "parts": {}}
    if not raw:
        base["notes"].append("empty value")
        return base
    try:
        parts, kind = usaddress.tag(raw)
    except usaddress.RepeatedLabelError:
        base["notes"].append("address has repeated/conflicting parts; left as-is")
        return base
    except Exception as exc:
        base["notes"].append(f"address parser failed: {exc}")
        return base
    p = dict(parts)
    out = {
        "street_number": p.get("AddressNumber"),
        "street": " ".join(x for x in [p.get("StreetNamePreType"),
                                       p.get("StreetName"),
                                       p.get("StreetNamePostType")] if x) or None,
        "unit": " ".join(x for x in [p.get("OccupancyType"),
                                     p.get("OccupancyIdentifier")] if x) or None,
        "city": p.get("PlaceName"),
        "state": p.get("StateName"),
        "zip": p.get("ZipCode"),
    }
    line1 = " ".join(x for x in [out["street_number"], out["street"],
                                 out["unit"]] if x)
    line = ", ".join(x for x in [line1, out["city"],
                                  " ".join(y for y in [out["state"],
                                                        out["zip"]] if y)] if x)
    base["parts"] = out
    base["address_type"] = kind
    if out["street"] and (out["city"] or out["zip"]):
        base.update({"normalized": line, "ok": True, "confidence": 0.9})
    else:
        base["notes"].append("could not identify a street plus city/ZIP")
    return base
