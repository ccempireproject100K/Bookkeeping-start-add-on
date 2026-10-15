"""Niche packs: one codebase, many trade faces.

Each YAML file in backend/niches/ describes a vertical (HVAC, plumbing,
barbershop, ...): brand face, terminology dictionary, trade chart-of-accounts
lines, starter services, setup checklist, and copilot vocabulary.

Resolution order for "which niche am I looking at":
  1. the workspace's stored niche_id (logged-in app), else
  2. the subdomain of the request Host (niche.<domain>), else
  3. the "barbershop" default.

Adding niche #47 = adding one YAML file. No code changes, no redeploy
beyond the file itself, no per-subdomain Cloudflare config (one wildcard
DNS record covers every niche subdomain).
"""
import os
import re

import yaml

PACKS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "niches")

DEFAULT_NICHE = "barbershop"

_REQUIRED = ["id", "trade", "product_name", "tagline", "terms",
             "revenue_accounts", "services", "checklist"]
_REQUIRED_TERMS = ["worker", "worker_plural", "shop", "job", "customer"]

_cache: dict = {}


def _validate(pack: dict, path: str) -> dict:
    for key in _REQUIRED:
        if not pack.get(key):
            raise ValueError(f"niche pack {path}: missing required key '{key}'")
    for t in _REQUIRED_TERMS:
        if not pack["terms"].get(t):
            raise ValueError(f"niche pack {path}: missing terms.{t}")
    for acct in pack["revenue_accounts"]:
        if not isinstance(acct.get("code"), str) or not acct.get("name"):
            raise ValueError(f"niche pack {path}: revenue_accounts need code+name")
    for item in pack["checklist"]:
        if not item.get("id") or not item.get("label"):
            raise ValueError(f"niche pack {path}: checklist items need id+label")
    pack.setdefault("subdomains", [pack["id"]])
    pack.setdefault("colors", {}).setdefault("accent", "#f59e0b")
    pack.setdefault("trade_expense_accounts", [])
    pack.setdefault("copilot_notes", "")
    return pack


def all_packs() -> list:
    """All packs, validated, sorted by trade name. Cached after first load."""
    global _cache
    if _cache:
        return sorted(_cache.values(), key=lambda p: p["trade"])
    if not os.path.isdir(PACKS_DIR):
        raise RuntimeError(f"niches directory missing: {PACKS_DIR}")
    for fname in sorted(os.listdir(PACKS_DIR)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(PACKS_DIR, fname)
        with open(path, "r", encoding="utf-8") as fh:
            pack = yaml.safe_load(fh)
        pack = _validate(pack, fname)
        if pack["id"] in _cache:
            raise ValueError(f"duplicate niche id '{pack['id']}' in {fname}")
        _cache[pack["id"]] = pack
    if DEFAULT_NICHE not in _cache:
        raise RuntimeError(f"default niche '{DEFAULT_NICHE}' pack not found")
    return sorted(_cache.values(), key=lambda p: p["trade"])


def get_pack(niche_id: str | None) -> dict:
    """Pack for an id, falling back to the default niche. Never raises."""
    try:
        packs = all_packs()
    except Exception:
        return {}
    for p in packs:
        if p["id"] == (niche_id or DEFAULT_NICHE):
            return p
    return next(p for p in packs if p["id"] == DEFAULT_NICHE)


def niche_from_host(host: str | None) -> str | None:
    """Map a request Host to a niche id via pack subdomains.

    hvac.example.com -> "hvac". Returns None when no pack claims the
    subdomain (caller falls back to the workspace niche or default).
    """
    if not host:
        return None
    host = host.split(":")[0].lower()
    sub = host.split(".")[0] if host.count(".") >= 2 else ""
    if not sub or sub == "www":
        return None
    for p in all_packs():
        if sub in [s.lower() for s in p.get("subdomains", [])]:
            return p["id"]
    # also accept "<niche-id>" directly, e.g. pestcontrol.example.com
    for p in all_packs():
        if sub == p["id"].replace("_", ""):
            return p["id"]
    return None


def public_pack(pack: dict) -> dict:
    """Safe subset for public endpoints (signup picker, resolvers)."""
    return {
        "id": pack["id"], "trade": pack["trade"],
        "product_name": pack["product_name"], "tagline": pack["tagline"],
        "terms": pack["terms"],
        "colors": pack.get("colors", {}),
        "subdomains": pack.get("subdomains", []),
    }


def reload():
    """For tests."""
    global _cache
    _cache = {}
