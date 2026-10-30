"""Column-type detection and the pre-import "mess report".

The mess report is the value a shop owner sees BEFORE anything imports:
how many rows, and per column how empty / unparseable / mixed the data
is. It runs on mapped importer rows (field -> value dicts) but works on
any list of dicts, which is what makes it reusable by the refinery.
"""
import re

from .normalize import normalize_date, normalize_phone
from .schema import EMAIL_RE

_AMOUNT_RE = re.compile(r"^\$?-?\d[\d,]*(\.\d{1,2})?$")
_STREET_HINT = re.compile(
    r"\b(st|street|ave|avenue|blvd|rd|road|dr|drive|ln|lane|suite|ste|apt|hwy)\b",
    re.I)


def detect_column_type(values):
    """Best-guess type for one column from its non-empty values."""
    vals = [str(v).strip() for v in values if str(v or "").strip()]
    if not vals:
        return {"type": "empty", "confidence": 1.0}
    sample = vals[:200]
    n = len(sample)
    rates = {
        "email": sum(1 for v in sample if EMAIL_RE.match(v)) / n,
        "date": sum(1 for v in sample if normalize_date(v)["ok"]) / n,
        "phone": sum(1 for v in sample if normalize_phone(v)["ok"]) / n,
        "amount": sum(1 for v in sample if _AMOUNT_RE.match(v)) / n,
    }
    if sum(1 for v in sample if "@" in v) / n >= 0.5:
        return {"type": "email", "confidence": round(rates["email"], 2),
                "rates": rates}
    best = max(rates, key=rates.get)
    if rates[best] >= 0.6:
        second = sorted(rates.values())[-2]
        return {"type": best if second < 0.35 else "mixed",
                "confidence": round(rates[best], 2), "rates": rates}
    if sum(1 for v in sample if _STREET_HINT.search(v) and
           any(ch.isdigit() for ch in v)) / n >= 0.5:
        return {"type": "address", "confidence": 0.7, "rates": rates}
    if sum(1 for v in sample if len(v.split()) <= 6) / n >= 0.7:
        return {"type": "name", "confidence": 0.5, "rates": rates}
    return {"type": "text", "confidence": 0.4, "rates": rates}


def mess_report(rows):
    """Per-column mess stats for a list of row dicts."""
    columns = []
    seen = []
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.append(k)
    for col in seen:
        values = [r.get(col) for r in rows]
        nonempty = [v for v in values if str(v or "").strip()]
        det = detect_column_type(values)
        issues = {}
        if det["type"] in ("date", "mixed"):
            bad, formats = 0, set()
            for v in nonempty:
                res = normalize_date(v)
                if res["ok"]:
                    formats.add(res.get("format", "?"))
                else:
                    bad += 1
            if bad:
                issues["unparseable_dates"] = bad
            if len(formats) > 1:
                issues["mixed_date_formats"] = sorted(formats)
        if det["type"] in ("phone", "mixed"):
            bad = sum(1 for v in nonempty if not normalize_phone(v)["ok"])
            if bad:
                issues["invalid_phones"] = bad
        if det["type"] in ("email", "mixed"):
            bad = sum(1 for v in nonempty if not EMAIL_RE.match(str(v).strip()))
            if bad:
                issues["invalid_emails"] = bad
        columns.append({
            "column": col,
            "values": len(nonempty),
            "empty": len(values) - len(nonempty),
            "empty_rate": round((len(values) - len(nonempty)) / len(values), 3)
                          if values else 0.0,
            "detected_type": det["type"],
            "type_confidence": det["confidence"],
            "issues": issues,
        })
    return {"rows": len(rows), "columns": columns}
