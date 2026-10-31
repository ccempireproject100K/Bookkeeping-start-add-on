"""US address parsing via usaddress (MIT).

parse_address splits a free-form US address into components and rebuilds
a canonical single line ("123 Main St Apt 4, Austin, TX 78701"). Parse
failures (repeated labels, non-US shapes) return ok=False with the
original untouched — the caller keeps the raw address.
"""
from . import RESULT_SHAPE
from .normalize import collapse_whitespace


def parse_address(value):
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
