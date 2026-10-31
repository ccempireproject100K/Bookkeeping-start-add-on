"""Subdomain wiring for the canonical 36 niches.

The owner's structure (2026-10-31): each niche is served on
<niche>.bookkeeping.mindateasesolutions.com, where <niche> is the
pack's canonical slug (id, hyphenated — underscores are not legal in a
DNS label). The apex and the bare "bookkeeping" host serve the generic
trade-neutral face; an unknown niche label also lands on the generic
face, never an error.

Also pins the 36-pack universe (33 trades + credit_repair +
small_business_owner + gig_work, the 2026-10-30 taxonomy top-up) and
the CORS allowlist shapes the subdomain front needs.

No live MongoDB / server needed: exercises the real YAML loaders, the
host resolver, the public router functions, and the pure CORS helpers.

Run from the backend directory with:
    python -m pytest tests/test_niche_subdomain_wiring.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import niches
from lib.cors import cors_origin_regex, cors_origins
from lib.niches import niche_from_host
from routers.edition import resolve_niche

BOOKKEEPING_DOMAIN = "bookkeeping.mindateasesolutions.com"

CANONICAL_36 = {
    "hvac", "plumbing", "electrical", "roofing", "appliance_repair",
    "garage_door", "pest_control", "landscaping", "pool_service",
    "painting", "handyman", "solar", "cleaning", "tree_service",
    "fencing", "restoration", "auto_repair", "barbershop",
    "real_estate", "freelancer", "locksmith", "septic_well",
    "kitchen_bath", "windows_doors", "flooring", "foundation_repair",
    "whole_home_generators", "smart_home", "junk_removal",
    "decks_outdoor", "irrigation", "siding_gutters", "home_inspection",
    # the 2026-10-30 top-up
    "credit_repair", "small_business_owner", "gig_work",
}


def _slug(pack_id: str) -> str:
    return pack_id.replace("_", "-")


# ---------- the 36-pack universe ----------

def test_pack_universe_is_the_canonical_36():
    packs = niches.all_packs()
    assert len(packs) == 36
    assert {p["id"] for p in packs} == CANONICAL_36


def test_top_up_packs_carry_full_faces():
    for pid, trade in (("credit_repair", "Credit Repair"),
                       ("small_business_owner", "Small Business Owner"),
                       ("gig_work", "Gig Work")):
        pack = niches.get_pack(pid)
        assert pack["id"] == pid
        assert pack["trade"] == trade
        # canonical slug is the primary (first) subdomain
        assert pack["subdomains"][0] == _slug(pid)
        assert pack["revenue_accounts"] and pack["trade_expense_accounts"]
        assert pack["services"] and pack["checklist"] and pack["kpis"]


# ---------- resolver: the ruled host structure ----------

def test_every_pack_resolves_from_its_slug_subdomain():
    """<slug>.bookkeeping.mindateasesolutions.com -> that pack's face,
    for all 36 — this is the exact URL shape marketing will use."""
    for pack in niches.all_packs():
        host = f"{_slug(pack['id'])}.{BOOKKEEPING_DOMAIN}"
        face = asyncio.run(resolve_niche(host))
        assert face["niche_id"] == pack["id"], host
        assert face["product_name"] == pack["product_name"], host


def test_marketing_aliases_resolve_in_the_structure():
    cases = {
        f"plumber.{BOOKKEEPING_DOMAIN}": "plumbing",
        f"electrician.{BOOKKEEPING_DOMAIN}": "electrical",
        f"pest.{BOOKKEEPING_DOMAIN}": "pest_control",
        f"credit.{BOOKKEEPING_DOMAIN}": "credit_repair",
        f"gig.{BOOKKEEPING_DOMAIN}": "gig_work",
        f"smallbusiness.{BOOKKEEPING_DOMAIN}": "small_business_owner",
    }
    for host, pid in cases.items():
        assert niche_from_host(host) == pid, host


def test_legacy_flat_hosts_still_resolve():
    # niche.<domain> (the original single-level shape) is unchanged.
    assert niche_from_host("hvac.example.com") == "hvac"
    assert niche_from_host("barber.example.com") == "barbershop"
    assert niche_from_host("pestcontrol.example.com") == "pest_control"


def test_apex_www_and_bookkeeping_home_are_generic():
    for host in ("mindateasesolutions.com",
                 "www.mindateasesolutions.com",
                 BOOKKEEPING_DOMAIN,
                 f"www.{BOOKKEEPING_DOMAIN}",
                 "bookkeeping-web.onrender.com",
                 "localhost"):
        face = asyncio.run(resolve_niche(host))
        assert face["niche_id"] == "generic", host
        assert face["product_name"] == "Mind at Ease Solutions", host


def test_unknown_niche_label_is_generic_never_an_error():
    for host in (f"nosuchtrade.{BOOKKEEPING_DOMAIN}",
                 f"plumbers.{BOOKKEEPING_DOMAIN}",
                 f"hvac2.{BOOKKEEPING_DOMAIN}"):
        face = asyncio.run(resolve_niche(host))
        assert face["niche_id"] == "generic", host
    assert niche_from_host(None) is None
    assert niche_from_host("") is None


def test_host_parsing_robustness():
    # case-insensitive, port-tolerant
    assert niche_from_host(f"HVAC.{BOOKKEEPING_DOMAIN}") == "hvac"
    assert niche_from_host(f"hvac.{BOOKKEEPING_DOMAIN}:443") == "hvac"
    assert niche_from_host("Credit-Repair." + BOOKKEEPING_DOMAIN) \
        == "credit_repair"


def test_market_skin_resolver_accepts_the_top_up_trades():
    from lib.market_packs import resolve_market_pack
    for pid in ("credit_repair", "small_business_owner", "gig_work"):
        skin = resolve_market_pack(pid, "US")
        assert skin is not None, pid
        assert skin["trade"]["id"] == pid


# ---------- CORS shapes for the subdomain front ----------

def test_cors_origins_default_and_list():
    assert cors_origins({}) == ["http://localhost:3000"]
    assert cors_origins({"FRONTEND_URL": "https://a.example"}) == \
        ["https://a.example"]
    assert cors_origins(
        {"FRONTEND_URL": "https://a.example, https://b.example"}) == \
        ["https://a.example", "https://b.example"]
    # blank entries never leak into the allowlist
    assert cors_origins({"FRONTEND_URL": " , "}) == ["http://localhost:3000"]


def test_cors_origin_regex_passthrough_and_safety():
    assert cors_origin_regex({}) is None
    pat = r"https://([a-z0-9-]+\.)?bookkeeping\.mindateasesolutions\.com"
    assert cors_origin_regex({"FRONTEND_ORIGIN_REGEX": pat}) == pat
    # a broken regex degrades to None instead of crashing boot
    assert cors_origin_regex({"FRONTEND_ORIGIN_REGEX": "([bad"}) is None
