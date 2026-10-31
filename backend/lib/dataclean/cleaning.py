"""Row-level cleaning for client/vendor/item records.

clean_row returns (cleaned_doc, changes) where every change is
explainable: {field, before, after, rule, confidence}. Display values are
only ever whitespace-collapsed or genuinely re-formatted (phone → E.164,
address → canonical line); the name shown to users is NEVER stripped of
its legal suffix — the suffix-free form lives in `name_key`, used only
for matching. Unparseable values are kept verbatim.
"""
from .addresses import parse_address
from .names import entity_key
from .normalize import collapse_whitespace, normalize_phone
from .schema import EMAIL_RE


def clean_row(doc, kind):
    out = dict(doc)
    changes = []

    def note(field, before, after, rule, confidence):
        if before != after:
            changes.append({"field": field, "before": before, "after": after,
                            "rule": rule, "confidence": confidence})

    name = doc.get("name")
    if name:
        tidy = collapse_whitespace(name)
        note("name", name, tidy, "name:whitespace", 1.0)
        out["name"] = tidy
        out["name_key"] = entity_key(tidy)

    email = doc.get("email")
    if email:
        tidy = str(email).strip().lower()
        if EMAIL_RE.match(tidy):
            note("email", email, tidy, "email:trim-and-lowercase", 1.0)
            out["email"] = tidy

    phone = doc.get("phone")
    if phone:
        res = normalize_phone(phone)
        if res["ok"]:
            note("phone", phone, res["normalized"],
                 "phone:libphonenumber-e164", res["confidence"])
            out["phone"] = res["normalized"]

    address = doc.get("address")
    if address:
        res = parse_address(address)
        if res["ok"]:
            note("address", address, res["normalized"],
                 "address:usaddress-canonical", res["confidence"])
            out["address"] = res["normalized"]
            out["address_parts"] = res["parts"]

    return out, changes
