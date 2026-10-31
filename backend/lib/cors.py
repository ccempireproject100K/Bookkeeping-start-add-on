"""CORS origin configuration for the API.

Historically the API allowed exactly one origin: the FRONTEND_URL env
var. The niche subdomain structure (<niche>.bookkeeping.<domain>) puts
the same SPA on many origins, so the allowlist needs two more shapes:

  FRONTEND_URL          comma-separated list of exact origins
                        (back-compatible: a single URL works unchanged)
  FRONTEND_ORIGIN_REGEX optional regex; any origin it fully matches is
                        allowed (e.g. every niche subdomain of the
                        product domain). Compiled once at import.

Pure helpers, unit-tested without the app: server.py feeds the results
to CORSMiddleware as allow_origins / allow_origin_regex.
"""
import os
import re

DEFAULT_ORIGIN = "http://localhost:3000"


def cors_origins(env: dict | None = None) -> list:
    """Exact-match origins from FRONTEND_URL (comma-separated)."""
    env = os.environ if env is None else env
    raw = env.get("FRONTEND_URL") or DEFAULT_ORIGIN
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins or [DEFAULT_ORIGIN]


def cors_origin_regex(env: dict | None = None) -> str | None:
    """Optional FRONTEND_ORIGIN_REGEX, validated. A broken regex must
    never take the API down at boot: it degrades to no regex (exact
    origins still apply) rather than raising."""
    env = os.environ if env is None else env
    raw = (env.get("FRONTEND_ORIGIN_REGEX") or "").strip()
    if not raw:
        return None
    try:
        re.compile(raw)
    except re.error:
        return None
    return raw
