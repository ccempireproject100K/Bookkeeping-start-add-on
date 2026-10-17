"""SPA contract for the public trade pages.

The frontend's /industries index and /industries/:nicheId pages are
rendered entirely from GET /niches: the index maps the array, the
detail page does `packs.find(p => p.id === slug)` — the exact lookup
simulated here for every one of the 33 packs. The landing pages also
render each pack's icon through the FaceIcon component, whose registry
is a fixed set of Lucide names in the frontend; a pack icon outside
that registry silently degrades to the generic fallback icon, so the
two sides are pinned to each other here.

These tests exercise the same route functions the API serves
(routers.edition.list_niches / resolve_niche), not re-implementations.
"""
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from lib import niches
from routers.edition import list_niches, resolve_niche

REPO_ROOT = Path(__file__).resolve().parents[2]
FACE_ICON = REPO_ROOT / "frontend" / "src" / "components" / "FaceIcon.jsx"

TRADE_COUNT = 33
# Fields the SPA actually reads (pages/Industries.jsx + TradeLanding.jsx).
REQUIRED_TERMS = ["worker", "worker_plural", "shop", "job", "customer"]


def _spa_pack_list():
    return asyncio.run(list_niches())


def test_niche_list_matches_the_pack_universe():
    packs = _spa_pack_list()
    assert len(packs) == TRADE_COUNT
    assert [p["id"] for p in packs] == [p["id"] for p in niches.all_packs()]


def test_every_pack_carries_the_fields_the_spa_renders():
    for p in _spa_pack_list():
        for key in ("id", "trade", "product_name", "tagline", "icon"):
            assert isinstance(p.get(key), str) and p[key], f"{p.get('id')}: bad {key}"
        assert p["colors"]["accent"].startswith("#"), p["id"]
        for term in REQUIRED_TERMS:
            assert p["terms"].get(term), f"{p['id']}: missing terms.{term}"
        assert isinstance(p["services"], list) and p["services"], p["id"]
        assert all(isinstance(s, str) for s in p["services"]), p["id"]
        assert isinstance(p["checklist"], list) and p["checklist"], p["id"]
        for item in p["checklist"]:
            assert item.get("id") and item.get("label"), f"{p['id']}: bad checklist item"


def test_every_pack_id_resolves_through_the_spas_lookup():
    """Simulate TradeLanding: list the packs, find by route slug."""
    packs = _spa_pack_list()
    for pack in niches.all_packs():
        found = next((p for p in packs if p["id"] == pack["id"]), None)
        assert found is not None, f"SPA lookup would miss pack {pack['id']}"
        assert found["product_name"] == pack["product_name"]


def test_every_pack_resolves_by_its_own_subdomain():
    """The login face + subdomain routing use GET /niches/resolve."""
    for pack in niches.all_packs():
        subdomains = pack.get("subdomains") or []
        assert subdomains, f"pack {pack['id']} has no subdomain"
        face = asyncio.run(resolve_niche(f"{subdomains[0]}.example.com"))
        assert face["niche_id"] == pack["id"], pack["id"]
        assert face["product_name"] == pack["product_name"], pack["id"]


def _face_icon_registry():
    text = FACE_ICON.read_text(encoding="utf-8")
    block = re.search(r"const ICONS = \{(.*?)\};", text, re.S)
    assert block, "FaceIcon.jsx: ICONS registry not found"
    names = set()
    for token in block.group(1).split(","):
        token = token.strip()
        if token:
            names.add(token.split(":")[0].strip())
    return names


def test_every_pack_icon_exists_in_the_faceicon_registry():
    registry = _face_icon_registry()
    # FaceIcon renders `ICONS[name] || Briefcase`: the hard-coded
    # fallback is a valid outcome too, everything else must be a key.
    assert "|| Briefcase" in FACE_ICON.read_text(encoding="utf-8")
    registry.add("Briefcase")
    for p in _spa_pack_list():
        assert p["icon"] in registry, (
            f"pack {p['id']} icon {p['icon']!r} is not in FaceIcon's registry; "
            "the landing page would silently render the fallback icon"
        )
