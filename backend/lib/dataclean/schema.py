"""Pre-import schema validation with plain-language errors.

CHOICE (recorded per the build brief): a thin IN-HOUSE validator instead
of Pandera. The rows here are plain dicts streamed from CSV, not
DataFrames, and the product rule is that a shop owner must understand
every error without a manual — Pandera's error text is engineer-speak.
The validator keeps the same discipline (declare the expected shape,
report every violation) with owner-grade wording. Pandera (MIT) remains
the right tool later for DataFrame-shaped refinery work.

Two severities:
  - blocking: the FILE is wrong (a whole column is not what it claims to
    be, or no row has a name). run_import refuses unless the user
    explicitly overrides — and overrides are audit-logged by the router.
  - row: individual rows are wrong; they are reported and skipped by the
    importer exactly as before. Phone problems are row-level WARNINGS:
    the row imports with its raw phone kept, never silently rewritten.
"""
import re

from .normalize import normalize_phone

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_KINDS = ("customers", "vendors", "items")


def _frac(bad, total):
    return (bad / total) if total else 0.0


def validate_rows(rows, kind):
    """rows: mapped importer rows (dicts keyed by target field)."""
    assert kind in _KINDS, kind
    blocking, row_errors = [], []

    names = [str(r.get("name") or "").strip() for r in rows]
    if rows and not any(names):
        blocking.append(
            "No row in this file has a name. Check that the name column "
            "is matched to Name before importing.")
    for i, nm in enumerate(names):
        if rows and not nm:
            row_errors.append({"row": i + 1, "field": "name",
                               "severity": "error",
                               "message": "This row has no name and will be skipped."})

    emails = [(i, str(r.get("email") or "").strip())
              for i, r in enumerate(rows) if str(r.get("email") or "").strip()]
    bad_emails = [(i, e) for i, e in emails if not EMAIL_RE.match(e)]
    if len(emails) >= 3 and _frac(len(bad_emails), len(emails)) > 0.5:
        blocking.append(
            f"The Email column does not look like email addresses: "
            f"{len(bad_emails)} of {len(emails)} values have no valid "
            f"address shape. Check the column matching.")
    else:
        for i, e in bad_emails:
            row_errors.append({"row": i + 1, "field": "email",
                               "severity": "error",
                               "message": f"'{e}' is not a valid email address; "
                                          "the row imports without it only if "
                                          "you map it away, otherwise it is kept as typed."})

    for i, r in enumerate(rows):
        ph = str(r.get("phone") or "").strip()
        if ph and not normalize_phone(ph)["ok"]:
            row_errors.append({"row": i + 1, "field": "phone",
                               "severity": "warning",
                               "message": f"'{ph}' is not a recognizable phone "
                                          "number; it will be kept exactly as typed."})

    if kind == "items":
        prices = [(i, r.get("price")) for i, r in enumerate(rows)
                  if r.get("price") not in (None, "")]
        bad_prices = []
        for i, p in prices:
            try:
                float(str(p).replace("$", "").replace(",", ""))
            except (TypeError, ValueError):
                bad_prices.append((i, p))
        if len(prices) >= 3 and _frac(len(bad_prices), len(prices)) > 0.5:
            blocking.append(
                f"The Price column does not look like prices: "
                f"{len(bad_prices)} of {len(prices)} values are not numbers. "
                f"Check the column matching.")

    return {"blocking": blocking, "row_errors": row_errors,
            "valid": not blocking}
