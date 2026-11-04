"""Row-level cleaning for client/vendor/item records.

clean_row returns (cleaned_doc, changes) where every change is
explainable: {field, before, after, rule, confidence}. Display values are
only ever whitespace-collapsed or genuinely re-formatted (phone → E.164,
address → canonical line); the name shown to users is NEVER stripped of
its legal suffix — the suffix-free form lives in `name_key`, used only
for matching. Unparseable values are kept verbatim.

Rule filtering (Phase 2): a caller may pass `disabled_rules` — the rules
a saved import profile has learned to hold back. clean_row_full then
returns (cleaned_doc, applied_changes, held_changes): held changes are
computed and reported exactly like applied ones, but the cleaned doc
keeps the original value. Nothing about the default (no disabled rules)
changes: every rule applies, held is empty.
"""
from .addresses import parse_address
from .names import entity_key
from .normalize import collapse_whitespace, normalize_phone
from .schema import EMAIL_RE

# Every rule clean_row can fire, in firing order. Saved import profiles
# store per-rule decisions against these names; unknown names are
# rejected at the API boundary, never silently learned.
KNOWN_RULES = [
    "name:whitespace",
    "email:trim-and-lowercase",
    "phone:libphonenumber-e164",
    "address:usaddress-canonical",
    "address:generic-intl",
]


def _clean(doc, kind, disabled_rules, country=None):
    out = dict(doc)
    changes = []
    held = []
    disabled = set(disabled_rules or ())

    def attempt(field, before, after, rule, confidence, extra=None):
        """Record a candidate change: applied unless its rule is held."""
        if before == after:
            return
        ch = {"field": field, "before": before, "after": after,
              "rule": rule, "confidence": confidence}
        if rule in disabled:
            held.append(ch)
            return
        changes.append(ch)
        out[field] = after
        if extra:
            out.update(extra)

    name = doc.get("name")
    if name:
        tidy = collapse_whitespace(name)
        # name_key derives from the tidied name either way (it is a
        # matching key, not a display value), so holding the whitespace
        # rule never weakens dedupe matching.
        out["name_key"] = entity_key(tidy)
        attempt("name", name, tidy, "name:whitespace", 1.0)

    email = doc.get("email")
    if email:
        tidy = str(email).strip().lower()
        if EMAIL_RE.match(tidy):
            attempt("email", email, tidy, "email:trim-and-lowercase", 1.0)

    phone = doc.get("phone")
    if phone:
        res = normalize_phone(phone)
        if res["ok"]:
            attempt("phone", phone, res["normalized"],
                    "phone:libphonenumber-e164", res["confidence"])

    address = doc.get("address")
    if address:
        intl = bool(country) and str(country).strip().upper() not in (
            "US", "USA")
        rule = "address:generic-intl" if intl else "address:usaddress-canonical"
        res = parse_address(address, country=country)
        if res["ok"]:
            # address_parts is a parse product (set whenever the parse
            # succeeds and the rule is live, as before); only the
            # display-line rewrite is the rule's "change". For the intl
            # lane the "rewrite" is whitespace-collapsing only — foreign
            # address text is never re-formed.
            if rule not in disabled:
                out["address_parts"] = res["parts"]
            attempt("address", address, res["normalized"],
                    rule, res["confidence"])

    return out, changes, held


def clean_row(doc, kind, country=None):
    out, changes, _held = _clean(doc, kind, (), country)
    return out, changes


def clean_row_full(doc, kind, disabled_rules=(), country=None):
    """(cleaned, applied_changes, held_changes) — held changes are the
    ones a profile/run toggle suppressed: reported, never applied."""
    return _clean(doc, kind, disabled_rules, country)
