"""Phonetic keys for BLOCKING ONLY (power-up P6).

Primary encoder: the `metaphone` package (oubiwann/metaphone,
BSD-3-Clause, pure Python) — ADOPTED in the Phase 0 component survey
(~/workspace/buildout/dataclean-powerup/PHASE0-SURVEY.md §B) for its
Double Metaphone PRIMARY + SECONDARY codes: Schmidt (XMT/SMT) and
Smith (SM0/XMT) share the XMT code, so blocking on either code lands
variant surnames in the same block. Golden vectors pinned in
tests/test_dataclean_phase_a.py (Smith, Schmidt, architect, bajador).

Fallback: if the package is not importable, a compact in-house
implementation of the same published algorithm (Lawrence Philips,
1990/2000 — original code written for this codebase) supplies the
primary code only. Either way the contract is identical:

Keys are used exclusively to *generate candidate pairs* in linkage.
They contribute nothing to any score — pair_evidence judges the pair
with the same field evidence as every other candidate. A phonetic
key can therefore never create a suggestion by itself; it can only
stop a real pair from being missed because the two spellings share
no token.
"""
import re

try:  # the Phase 0 ADOPT
    from metaphone import doublemetaphone as _dm_package
except Exception:  # pragma: no cover - exercised only without the dep
    _dm_package = None

_VOWELS = set("AEIOUY")
_NON_ALPHA = re.compile(r"[^A-Z]")


def _at(s, i):
    return s[i] if 0 <= i < len(s) else ""


def _slavo_germanic(s):
    return any(t in s for t in ("W", "K", "CZ", "WITZ"))


def double_metaphone_primary(word):
    """Primary Double Metaphone code (in-house fallback implementation)."""
    s = _NON_ALPHA.sub("", str(word or "").upper())
    if not s:
        return ""
    length = len(s)
    last = length - 1
    s_pad = s + "     "  # sentinel padding, as in the reference design
    primary = []
    i = 0
    # Initial-letter special cases
    if s[:2] in ("GN", "KN", "PN", "WR", "PS"):
        i = 1
    if s[0] == "X":
        primary.append("S")
        i = 1
    while i < length:
        c = s_pad[i]
        if c in _VOWELS:
            if i == 0:
                primary.append("A")
            i += 1
        elif c == "B":
            primary.append("P")
            i += 2 if s_pad[i + 1] == "B" else 1
        elif c == "C":
            nxt = s_pad[i + 1]
            if s_pad[i - 1:i + 3] == "ACH" and i > 0 and (
                    s_pad[i + 2] != "I" and s_pad[i + 2] != "E"
                    or s[max(0, i - 2):i + 4] in ("BACHER", "MACHER")):
                primary.append("K")
                i += 2
            elif s_pad[i:i + 3] == "CIA":
                primary.append("X")
                i += 3
            elif s_pad[i:i + 2] == "CC" and not (
                    i == 1 and s_pad[0] == "M"):
                if s_pad[i + 2] in "IEH" and s_pad[i + 2:i + 4] != "HU":
                    if (i == 1 and s_pad[i - 1] == "A") or s[
                            max(0, i - 1):i + 4] in (
                            "UCCEE", "UCCES"):
                        primary.append("KS")
                    else:
                        primary.append("X")
                    i += 3
                else:
                    primary.append("K")
                    i += 2
            elif s_pad[i:i + 2] in ("CK", "CG", "CQ"):
                primary.append("K")
                i += 2
            elif s_pad[i:i + 2] in ("CI", "CE", "CY"):
                primary.append(
                    "X" if s_pad[i:i + 3] in ("CIO", "CIE", "CIA") else "S")
                i += 2
            else:
                primary.append("K")
                i += 2 if nxt == "C" else 1
        elif c == "D":
            if s_pad[i:i + 2] == "DG":
                if s_pad[i + 2] in "IEY":
                    primary.append("J")
                    i += 3
                else:
                    primary.append("TK")
                    i += 2
            elif s_pad[i:i + 2] in ("DT", "DD"):
                primary.append("T")
                i += 2
            else:
                primary.append("T")
                i += 1
        elif c == "F":
            primary.append("F")
            i += 2 if s_pad[i + 1] == "F" else 1
        elif c == "G":
            nxt = s_pad[i + 1]
            if nxt == "H":
                if i > 0 and s_pad[i - 1] not in _VOWELS:
                    primary.append("K")
                    i += 2
                elif i == 0:
                    primary.append("K" if s_pad[i + 2] == "I" else "J")
                    i += 2
                elif (s_pad[i - 1] in ("B", "H", "D")
                      or s_pad[max(0, i - 3):i] in ("BHD",)):
                    i += 2  # silent
                elif i > 2 and s_pad[i - 1] in "UE":
                    i += 2
                else:
                    if i > 1 and s_pad[i - 1] in _VOWELS and \
                            not _slavo_germanic(s):
                        primary.append("K")
                    i += 2
            elif nxt == "N":
                if i == 1 and s_pad[0] in _VOWELS and not _slavo_germanic(s):
                    primary.append("KN")
                elif s_pad[i + 2:i + 4] != "EY" and s_pad[i + 1] != "Y" \
                        and not _slavo_germanic(s):
                    primary.append("N")
                else:
                    primary.append("KN")
                i += 2
            elif s_pad[i:i + 3] == "GLI" and not _slavo_germanic(s):
                primary.append("KL")
                i += 2
            elif i == 0 and (nxt == "Y" or s_pad[i + 1:i + 3] in
                             ("ES", "EP", "EB", "EL", "EY", "IB", "IL",
                              "IN", "IE", "EI", "ER")):
                primary.append("K")
                i += 2 if nxt == "G" else 1
            elif s_pad[i:i + 2] in ("ER", "IR") and s_pad[i - 1] not in "EIO" \
                    and (i == last or False):
                primary.append("K")
                i += 2 if nxt == "G" else 1
            elif nxt == "I" and s_pad[i:i + 4] not in ("GIE ",):
                primary.append("J" if s[max(0, i - 1):i + 4] not in
                               ("GILA",) else "K")
                i += 2
            else:
                primary.append("K")
                i += 2 if nxt == "G" else 1
        elif c == "H":
            if (i == 0 or s_pad[i - 1] in _VOWELS) and s_pad[i + 1] in _VOWELS:
                primary.append("H")
                i += 2
            else:
                i += 1
        elif c == "J":
            if s_pad[i:i + 4] == "JOSE" or s_pad[i:i + 4] == "JOSÉ":
                primary.append("H")
                i += 1
            else:
                primary.append("J")
                i += 2 if s_pad[i + 1] == "J" else 1
        elif c == "K":
            primary.append("K")
            i += 2 if s_pad[i + 1] == "K" else 1
        elif c == "L":
            if s_pad[i + 1] == "L":
                primary.append("L")
                i += 2
            else:
                primary.append("L")
                i += 1
        elif c == "M":
            primary.append("M")
            i += 2 if s_pad[i + 1:i + 3] == "UMB" and (
                i + 1 == last or s_pad[i + 2:i + 4] == "ER") else 1
        elif c == "N":
            primary.append("N")
            i += 2 if s_pad[i + 1] == "N" else 1
        elif c == "P":
            if s_pad[i + 1] == "H":
                primary.append("F")
                i += 2
            else:
                primary.append("P")
                i += 2 if s_pad[i + 1] in ("P", "B") else 1
        elif c == "Q":
            primary.append("K")
            i += 2 if s_pad[i + 1] == "Q" else 1
        elif c == "R":
            primary.append("R")
            i += 2 if s_pad[i + 1] == "R" else 1
        elif c == "S":
            if s_pad[i:i + 4] in ("SIO ",) or s_pad[i:i + 3] in ("SIO", "SIA"):
                primary.append("S")
                i += 3
            elif s_pad[i:i + 2] in ("SH",):
                primary.append("X")
                i += 2
            elif s_pad[i:i + 3] in ("SCH",):
                primary.append("SK")
                i += 3
            elif i == 0 and s_pad[1] in "MNLW":
                primary.append("S")
                i += 3 if s_pad[i:i + 3] == "SCH" else 1
            elif s_pad[i:i + 2] == "SC":
                if s_pad[i + 2] == "H":
                    nxt3 = s_pad[i + 3:i + 5]
                    if nxt3 in ("OO", "ER", "EN", "UY", "ED", "EM"):
                        primary.append("SK")
                    else:
                        primary.append("X")
                    i += 3
                elif s_pad[i + 2] in "IEY":
                    primary.append("S")
                    i += 3
                else:
                    primary.append("SK")
                    i += 3
            else:
                primary.append("S")
                i += 2 if s_pad[i + 1] in ("S", "Z") else 1
        elif c == "T":
            if s_pad[i:i + 4] == "TION":
                primary.append("X")
                i += 3
            elif s_pad[i:i + 3] in ("TIA", "TCH"):
                primary.append("X")
                i += 3
            elif s_pad[i:i + 2] == "TH" or s_pad[i:i + 3] == "TTH":
                if s_pad[i + 2:i + 4] in ("OM", "AM") or s[:4] in (
                        "VAN ", "VON ") or s_pad[i - 1:i + 3] == "SCHT":
                    primary.append("T")
                else:
                    primary.append("0")
                i += 2
            else:
                primary.append("T")
                i += 2 if s_pad[i + 1] in ("T", "D") else 1
        elif c == "V":
            primary.append("F")
            i += 2 if s_pad[i + 1] == "V" else 1
        elif c == "W":
            if s_pad[i:i + 2] == "WR":
                primary.append("R")
                i += 2
            elif i == 0 and s_pad[i + 1] in _VOWELS:
                primary.append("F")
                i += 1
            elif s_pad[i:i + 3] == "WH":
                primary.append("A")
                i += 1
            else:
                i += 1
        elif c == "X":
            primary.append("KS")
            i += 2 if s_pad[i + 1] in ("C", "X") else 1
        elif c == "Z":
            if s_pad[i + 1] == "H":
                primary.append("J")
                i += 2
            else:
                primary.append("S")
                i += 2 if s_pad[i + 1] == "Z" else 1
        else:  # unknown char (non-ASCII already stripped)
            i += 1
    return "".join(primary)[:6]


def phonetic_keys(token):
    """Blocking keys for one name token: [primary, secondary?] —
    distinct, non-empty. With the adopted package both codes are
    returned; the in-house fallback returns the primary only."""
    if _dm_package is not None:
        try:
            primary, secondary = _dm_package(str(token or ""))
        except Exception:
            primary, secondary = "", ""
        keys = []
        for code in (primary, secondary):
            if code and code not in keys:
                keys.append(code)
        if keys:
            return keys
    code = double_metaphone_primary(token)
    return [code] if code else []


def phonetic_key(token):
    """Single primary blocking key ('' when nothing usable)."""
    keys = phonetic_keys(token)
    return keys[0] if keys else ""
