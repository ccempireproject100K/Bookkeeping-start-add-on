"""Global API rate limiting: sliding-window per client IP.

A second layer under the login-specific brute-force protection in
routers/auth.py. Limits apply to every /api route so no endpoint can be
hammered for enumeration or scraping.

In-memory store: correct for a single worker. If the app ever runs multiple
uvicorn workers, move the counters to Redis/Mongo (the interface below is
small enough to swap).
"""
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# (requests, window_seconds) — tuned for an interactive SPA, not a scraper.
LIMITS = [
    (300, 60),      # 300 req/min per IP
    (2000, 3600),   # 2000 req/hour per IP
]
LONGEST = max(w for _, w in LIMITS)

_buckets: dict[str, deque] = defaultdict(deque)
_last_sweep = 0.0


def _client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/api"):
            now = time.monotonic()
            ip = _client_ip(request)
            hits = _buckets[ip]
            while hits and hits[0] <= now - LONGEST:
                hits.popleft()
            for limit, window in LIMITS:
                recent = sum(1 for t in hits if t > now - window)
                if recent >= limit:
                    return JSONResponse(
                        {"detail": "Rate limit exceeded. Slow down and try again."},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
            hits.append(now)
            # Opportunistic cleanup so the table can't grow without bound.
            global _last_sweep
            if now - _last_sweep > 300:
                _last_sweep = now
                for k in [k for k, v in _buckets.items()
                          if not v or v[-1] <= now - LONGEST]:
                    del _buckets[k]
        return await call_next(request)
