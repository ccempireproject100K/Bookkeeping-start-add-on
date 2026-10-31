"""Niche packs: one codebase, many trade faces.

Each YAML file in backend/niches/ describes a vertical (HVAC, plumbing,
barbershop, ...): brand face, terminology dictionary, trade chart-of-accounts
lines, starter services, setup checklist, and copilot vocabulary.

Resolution order for "which niche am I looking at":
  1. the workspace's stored niche_id (logged-in app), else
  2. the subdomain of the request Host (niche.<domain>), else
  3. the generic (trade-neutral) default — the product's own identity,
     never one trade's face.

Adding niche #47 = adding one YAML file. No code changes, no redeploy
beyond the file itself, no per-subdomain Cloudflare config (one wildcard
DNS record covers every niche subdomain).
"""
import os
import re

import yaml

PACKS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "niches")

# The unresolved/default face: Mind at Ease Solutions itself, trade-neutral.
# Deliberately NOT a YAML pack, so it never appears in the trade picker or
# the trade x market matrix; it only answers "no niche resolved".
GENERIC_PACK = {
    "id": "generic",
    "trade": "All trades",
    "product_name": "Mind at Ease Solutions",
    "tagline": "Bookkeeping, job costing, payroll and tax tools for service businesses.",
    "icon": "Calculator",
    "terms": {
        "worker": "Staff",
        "worker_plural": "Staff",
        "shop": "business",
        "job": "job",
        "job_plural": "jobs",
        "customer": "customer",
        "customer_plural": "customers",
    },
    "colors": {"accent": "#f59e0b"},
    "subdomains": [],
    "revenue_accounts": [
        {"code": "4000", "name": "Service Revenue"},
        {"code": "4020", "name": "Service Revenue — Other"},
        {"code": "4100", "name": "Product Revenue — Retail"},
        {"code": "4200", "name": "Other Income"},
    ],
    "trade_expense_accounts": [
        {"code": "5100", "name": "Commissions"},
        {"code": "5200", "name": "Contractor Payouts"},
    ],
    "services": [
        {"name": "Service call", "kind": "service"},
        {"name": "Project work", "kind": "service"},
    ],
    "checklist": [
        {"id": "team", "label": "Add your team", "hint": "Staff show up once they have a workspace login", "link": "/team", "check": "team"},
        {"id": "services", "label": "Add services & products", "hint": "Your services and your product shelf", "link": "/inventory", "check": "services"},
        {"id": "bank", "label": "Add a bank account", "hint": "Reconcile payouts against deposits in Banking", "link": "/banking", "check": "bank"},
        {"id": "tax", "label": "Set up contractor tracking", "hint": "W-9s and 1099-NECs for your contractors", "link": "/tax", "check": "tax"},
        {"id": "sale", "label": "Run your first sale", "hint": "Quick Sell at the counter", "link": "/quick-sell", "check": "sale"},
    ],
    "copilot_notes": "",
}

DEFAULT_NICHE = "generic"

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
    if DEFAULT_NICHE != GENERIC_PACK["id"] and DEFAULT_NICHE not in _cache:
        raise RuntimeError(f"default niche '{DEFAULT_NICHE}' pack not found")
    return sorted(_cache.values(), key=lambda p: p["trade"])


def get_pack(niche_id: str | None) -> dict:
    """Pack for an id. A real trade id returns its pack; None or an
    unknown id returns the generic trade-neutral pack, so an unresolved
    face is the product itself — never one trade standing in for all.
    Never raises."""
    try:
        packs = all_packs()
    except Exception:
        return dict(GENERIC_PACK)
    for p in packs:
        if p["id"] == niche_id:
            return p
    return dict(GENERIC_PACK)


def _slug_forms(pack: dict) -> set:
    """The canonical slug forms of a pack's id, for hostname labels: the
    id itself, hyphenated (the only form legal in a DNS label), and
    squashed. Declared marketing subdomains are matched separately,
    in the first pass of niche_from_host."""
    pid = pack["id"].lower()
    return {pid, pid.replace("_", "-"), pid.replace("_", "")}


def niche_from_host(host: str | None) -> str | None:
    """Map a request Host to a niche id via pack subdomains.

    The niche is the first DNS label:
      hvac.example.com                              -> "hvac"
      hvac.bookkeeping.mindateasesolutions.com      -> "hvac"
      pest-control.bookkeeping.mindateasesolutions.com -> "pest_control"
    Apex / www / the bare "bookkeeping" host -> None (the generic face).
    Returns None when no pack claims the label (caller falls back to the
    workspace niche or default) — an unknown niche label is never an
    error, it just gets the product's own trade-neutral face.
    """
    if not host:
        return None
    host = host.split(":")[0].lower()
    sub = host.split(".")[0] if host.count(".") >= 2 else ""
    if not sub or sub == "www":
        return None
    # Declared subdomains win over derived slug forms across all packs,
    # so a marketing alias can never be shadowed by another pack's id.
    for p in all_packs():
        if sub in [s.lower() for s in p.get("subdomains", [])]:
            return p["id"]
    for p in all_packs():
        if sub in _slug_forms(p):
            return p["id"]
    return None


def public_pack(pack: dict) -> dict:
    """Safe subset for public endpoints (signup picker, resolvers,
    trade landing pages)."""
    return {
        "id": pack["id"], "trade": pack["trade"],
        "product_name": pack["product_name"], "tagline": pack["tagline"],
        "icon": pack.get("icon", GENERIC_PACK["icon"]),
        "terms": pack["terms"],
        "colors": pack.get("colors", {}),
        "subdomains": pack.get("subdomains", []),
        "services": [s.get("name") for s in pack.get("services", [])],
        "checklist": [{"id": c["id"], "label": c["label"],
                       "hint": c.get("hint", "")}
                      for c in pack.get("checklist", [])],
    }


def reload():
    """For tests."""
    global _cache
    _cache = {}
