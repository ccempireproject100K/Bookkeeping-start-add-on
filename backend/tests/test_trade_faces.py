"""Trade faces: niche packs drive appearance + wording; the unresolved
default face is the generic product identity — never one trade's face.

Covers: per-pack visual identity (icon), the generic fallback in
lib.niches.get_pack, host-based face resolution via GET /niches/resolve,
and a neutral default chart of accounts.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import niches
from lib.coa import full_coa
from routers.edition import resolve_niche

TRADE_COUNT = 36


def test_all_trade_packs_have_icons():
    packs = niches.all_packs()
    assert len(packs) == TRADE_COUNT
    for p in packs:
        assert p.get("icon"), f"pack {p['id']} has no icon"
        assert niches.public_pack(p)["icon"] == p["icon"]
        assert niches.public_pack(p)["services"], f"pack {p['id']} has no services"


def test_generic_is_not_a_trade_pack():
    ids = [p["id"] for p in niches.all_packs()]
    assert "generic" not in ids
    assert niches.DEFAULT_NICHE == "generic"


def test_no_niche_resolves_to_generic_face():
    pack = niches.get_pack(None)
    assert pack["id"] == "generic"
    assert pack["product_name"] == "Mind at Ease Solutions"
    # Neutral terminology — no trade's words standing in for everyone's.
    assert pack["terms"]["worker"] == "Staff"
    assert pack["terms"]["customer"] == "customer"


def test_unknown_niche_resolves_to_generic_face():
    assert niches.get_pack("not-a-trade")["id"] == "generic"
    assert niches.get_pack("")["id"] == "generic"


def test_trade_packs_resolve_to_their_own_faces():
    barber = niches.get_pack("barbershop")
    assert barber["id"] == "barbershop"
    assert barber["product_name"] == "The Barber's Ledger"
    assert barber["terms"]["worker"] == "Barber"
    hvac = niches.get_pack("hvac")
    assert hvac["id"] == "hvac"
    assert hvac["icon"] == "Snowflake"


def test_resolve_endpoint_faces():
    barber = asyncio.run(resolve_niche("barber.example.com"))
    assert barber["niche_id"] == "barbershop"
    assert barber["icon"] == "Scissors"
    hvac = asyncio.run(resolve_niche("hvac.example.com"))
    assert hvac["niche_id"] == "hvac"
    assert hvac["product_name"] == "The HVAC Ledger"
    plumbing = asyncio.run(resolve_niche("plumbing.example.com"))
    assert plumbing["niche_id"] == "plumbing"
    assert plumbing["icon"] == "Wrench"


def test_resolve_endpoint_default_is_neutral():
    for host in ("www.example.com", "example.com",
                 "bookkeeping-web.onrender.com"):
        face = asyncio.run(resolve_niche(host))
        assert face["niche_id"] == "generic", host
        assert face["product_name"] == "Mind at Ease Solutions", host
        assert face["icon"] == "Calculator", host


def test_default_coa_is_trade_neutral():
    names = [r[1] for r in full_coa()]
    assert "Service Revenue" in names
    assert not any("Haircut" in n or "Chair Rental" in n for n in names)
    # ...while a barbershop workspace still gets its own trade chart.
    barber_names = [r[1] for r in full_coa("barbershop")]
    assert any("Haircut" in n for n in barber_names)
