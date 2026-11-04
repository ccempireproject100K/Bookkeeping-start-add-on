"""Venue-prefix street numbers (next phase, 2026-11-04).

The defect (named in the cross-list hardening record): a street
number behind a venue-name prefix — "Burlington, 6537 NE LOOP 820,
..." — defeated raw-text street-number extraction, so for records
carrying no parsed address_parts (uncleaned lists, failed parses) no
location clash could fire, and two different stores of one chain at
different street numbers in the same ZIP suggested at ~0.99 on name
+ address token similarity. parse_address itself was verified fine
on these shapes (usaddress strips the venue segment); the gap was
linkage.features()' fallback, which only read a number at the very
start of the string.

The fix: _street_number_from_text keeps the leading-number read and
adds the first later comma segment that begins with a number
followed by a street-bearing token. These tests pin the extraction
shapes, the guards (no unit/building/PO-Box/ZIP misreads), the pair
consequence (clash fires; same-number pairs unaffected), and that a
parsed parts value still wins over the text fallback.
"""
from lib.dataclean import linkage
from lib.dataclean.linkage import _street_number_from_text, features


def _num(address, parts=None):
    e = {"name": "X", "address": address}
    if parts is not None:
        e["address_parts"] = parts
    return features(e)["street_number"]


# ------------------------------------------------------- extraction

def test_leading_number_unchanged():
    assert _num("6537 NE Loop 820, Fort Worth, TX 76109, US") == "6537"
    assert _num("  123 Main St, Austin, TX 78701") == "123"


def test_venue_prefix_shapes():
    cases = {
        # the hardening record's named example (locker host store)
        "Burlington, 6537 NE LOOP 820, North Richland Hills, "
        "TX 76180, US": "6537",
        "Albertsons, 3525 Sycamore School Rd, Fort Worth, "
        "TX 76133, US": "3525",
        "Hulen Mall, 4800 S Hulen St #1005, Fort Worth, "
        "TX 76132, US": "4800",
        "QuikTrip, 9201 BENBROOK BLVD, BENBROOK, TX 76126, US": "9201",
        # two venue segments before the street segment
        "Amazon Locker - Mila, TIGER FARMS MARKET, 1411 SW Wilshire "
        "Blvd, Burleson, TX 76028, US": "1411",
        # a unit segment rides before the street segment
        "Shafer Plaza, Suite H, 1001 SW Wilshire Blvd, Burleson, "
        "TX 76028, US": "1001",
        # the venue's own store number does not shadow the street's
        "The UPS Store #4519, 1200 Main St, Dallas, TX 75201, US":
            "1200",
        "Walmart Shopping Center, 1055 SW Wilshire Blvd Ste E, "
        "Burleson, TX 76028, US": "1055",
    }
    for address, expected in cases.items():
        assert _num(address) == expected, address
        assert _street_number_from_text(address) == expected, address


def test_no_false_extraction():
    # A PO Box number is a box, not a street number (segment 0 is
    # never scanned).
    assert _num("PO Box 6537, Fort Worth, TX 76180, US") is None
    # A segment of digits with no street token is not a street
    # segment (a year, a bare ZIP, a suite number standing alone).
    assert _num("Vision Center, 2020, Dallas, TX 75201, US") is None
    assert _num("Some Plaza, 76180, Fort Worth, TX, US") is None
    # Unit / building segments carry no street token at the start.
    assert _num("Depot, Building 4, Floor 2, Dallas, TX, US") is None
    # Vacuous place-level addresses stay numberless.
    assert _num("Humble, TX, US") is None
    assert _num("Mansfield, TX 76063, US") is None
    assert _num("") is None
    assert _street_number_from_text(None) is None


def test_parsed_parts_still_win():
    # When the parse produced a number, the fallback is not consulted
    # (the parse lane was verified correct on venue shapes).
    assert _num("Burlington, 6537 NE LOOP 820, North Richland Hills, "
                "TX 76180, US",
                parts={"street_number": "9999"}) == "9999"
    # Parts present but numberless: the fallback applies.
    assert _num("Burlington, 6537 NE LOOP 820, North Richland Hills, "
                "TX 76180, US", parts={"city": "North Richland Hills",
                                        "zip": "76180"}) == "6537"


# --------------------------------------------------- pair behaviour

def _pair(a_addr, b_addr, name="QuikTrip", **kw):
    a = {"id": "a", "name": name, "address": a_addr,
         "phone": "", "email": ""}
    b = {"id": "b", "name": name, "address": b_addr,
         "phone": "", "email": ""}
    a.update(kw.get("a_extra", {}))
    b.update(kw.get("b_extra", {}))
    return linkage.suggest_duplicates_cross([a], [b])


def test_venue_twin_different_numbers_caps_to_review():
    # Same chain, same street, same ZIP — different street numbers,
    # one side venue-prefixed. Before the fix this suggested at
    # ~0.99 ("same address"); the location clash must now fire.
    res = _pair("QuikTrip, 9201 BENBROOK BLVD, BENBROOK, TX 76126, US",
                "9202 BENBROOK BLVD, BENBROOK, TX 76126, US")
    assert res["pairs"] == []
    assert len(res["review_pairs"]) == 1
    row = res["review_pairs"][0]
    assert "different street addresses" in row["evidence"]["why"]


def test_venue_twin_same_number_still_suggests():
    # The locker-host shape itself: identical name, identical
    # recovered number, shared website — no clash may fire and the
    # pair keeps suggesting.
    res = _pair(
        "Burlington, 6537 NE LOOP 820, North Richland Hills, "
        "TX 76180, US",
        "6537 NE LOOP 820, North Richland Hills, TX 76180, US",
        name="Burlington",
        a_extra={"website": "https://www.burlington.com"},
        b_extra={"website": "https://www.burlington.com"})
    assert len(res["pairs"]) == 1
    assert res["review_pairs"] == []


def test_within_list_venue_clash_also_fires():
    # features() is shared: the within-list deterministic path sees
    # the same recovered number (suggest_duplicates over one list).
    a = {"id": "a", "name": "QuikTrip",
         "address": "QuikTrip, 9201 BENBROOK BLVD, BENBROOK, "
                    "TX 76126, US", "phone": "", "email": ""}
    b = {"id": "b", "name": "QuikTrip",
         "address": "9202 BENBROOK BLVD, BENBROOK, TX 76126, US",
         "phone": "", "email": ""}
    res = linkage.suggest_duplicates([a, b])
    assert res["pairs"] == []
    assert len(res["review_pairs"]) == 1
