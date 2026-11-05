"""Per-niche match-key rules (power-up P6), expressed as DATA.

Trade knowledge applied to *match keys only* — never to display
values. Two kinds of rules, both conservative:

1. NON-DISTINGUISHING service tokens: "24 HR", "24 Hour", "Emergency"
   style tokens appear and disappear from a shop's name between data
   sources without the business changing ("24 HR Plumbing" in one
   export, "Plumbing" in the next). Dropping them from the key stops
   token-set similarity from being diluted by marketing words. Only
   applied when the record carries a niche/trade tag — a person named
   "24 Hour" loses nothing either way, but we do not guess.

2. CANONICAL token variants per niche: spelling variants of the same
   trade word map to one canonical token ("a/c" and "airconditioning"
   both -> "ac" for HVAC). Only true spelling/format variants live
   here — never synonyms that could be different businesses.

The tables are keyed by the 36 niche pack ids in backend/niches/.
Adding a niche's variants is a data edit in this file, not a code
branch — that is the design's requirement.
"""
import re

# Tokens that say WHEN/HOW a trade shop serves, not WHO it is.
_NON_DISTINGUISHING = {
    "24hr", "24hour", "24hours", "24", "hr", "hrs", "hour", "hours",
    "emergency", "emerg", "247", "24/7",
}

# niche pack id -> {variant token: canonical token}
NICHE_TOKEN_CANON = {
    "hvac": {"a/c": "ac", "airconditioning": "ac",
             "air-conditioning": "ac", "h.v.a.c": "hvac"},
    "plumbing": {"plbg": "plumbing", "plumb": "plumbing"},
    "electrical": {"elec": "electric", "elect": "electric"},
    "roofing": {"roof": "roofing"},
    "landscaping": {"landscape": "landscaping", "lndscp": "landscaping"},
    "cleaning": {"cleaners": "cleaning", "janitorial": "cleaning"},
    "auto_repair": {"auto": "auto", "automotive": "auto"},
    "garage_door": {"garagedoor": "garage door"},
    "pest_control": {"exterminating": "pest", "exterminator": "pest"},
    "tree_service": {"trees": "tree"},
    "pool_service": {"pools": "pool"},
    "windows_doors": {"window": "windows", "door": "doors"},
    "decks_outdoor": {"deck": "decks"},
    "siding_gutters": {"gutter": "gutters", "siding": "siding"},
    "whole_home_generators": {"generator": "generators",
                              "gen": "generators"},
    "kitchen_bath": {"kitchens": "kitchen", "baths": "bath",
                     "remodeling": "remodel"},
    "flooring": {"floors": "flooring", "floor": "flooring"},
    "painting": {"painters": "painting", "painter": "painting"},
    "handyman": {"handymen": "handyman"},
    "locksmith": {"locksmiths": "locksmith"},
    "septic_well": {"septic": "septic", "wells": "well"},
    "solar": {"photovoltaic": "solar", "pv": "solar"},
    "appliance_repair": {"appliances": "appliance"},
    "real_estate": {"realty": "real estate", "realtors": "real estate"},
    "credit_repair": {"credit": "credit"},
    "barbershop": {"barber": "barbershop", "barbers": "barbershop"},
    "home_inspection": {"inspections": "inspection",
                        "inspectors": "inspection"},
    "foundation_repair": {"foundations": "foundation"},
    "junk_removal": {"hauling": "junk", "junk": "junk"},
    "irrigation": {"sprinklers": "irrigation", "sprinkler": "irrigation"},
    "fencing": {"fences": "fencing", "fence": "fencing"},
    "smart_home": {"smarthome": "smart home"},
    "restoration": {"restorations": "restoration"},
    "gig_work": {},
    "freelancer": {},
    "small_business_owner": {},
}

# Adjacent-token joins: the name normalizer splits on punctuation, so
# "A/C" reaches the niche layer as the token pair ("a", "c"). Joins run
# before the canonical map so the pair can canonicalize like the
# un-split spelling would.
NICHE_TOKEN_JOINS = {
    "hvac": {("a", "c"): "ac"},
}

_DBA_RE = re.compile(r"\s+(?:dba|d/b/a)\s+", re.I)


def split_dba(name):
    """Split 'Legal Name LLC dba Trade Name' -> (legal, trade|None)."""
    parts = _DBA_RE.split(str(name or ""), maxsplit=1)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return parts[0].strip(), parts[1].strip()
    return (str(name or "").strip(), None)


def niche_key(name_key, niche):
    """Apply a niche's token rules to an already-normalized name key.

    Returns the key unchanged when no niche is known or the niche has
    no table — the niche layer can only ever *normalize variants*, and
    only for records that declare their trade.
    """
    if not name_key or not niche:
        return name_key
    niche = str(niche).strip().lower()
    canon = NICHE_TOKEN_CANON.get(niche)
    tokens = [t for t in str(name_key).split()
              if t not in _NON_DISTINGUISHING]
    joins = NICHE_TOKEN_JOINS.get(niche)
    if joins:
        joined, i = [], 0
        while i < len(tokens):
            pair = (tokens[i], tokens[i + 1]) if i + 1 < len(tokens) else None
            if pair in joins:
                joined.append(joins[pair])
                i += 2
            else:
                joined.append(tokens[i])
                i += 1
        tokens = joined
    if canon:
        mapped = []
        for t in tokens:
            c = canon.get(t, t)
            mapped.extend(c.split())
        tokens = mapped
    return " ".join(tokens)
