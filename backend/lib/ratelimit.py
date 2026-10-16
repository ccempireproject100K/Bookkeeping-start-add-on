"""Global API rate limiting: sliding-window per client IP.

A second layer under the login-specific brute-force protection in
routers/auth.py. Limits apply to every /api route so no endpoint can be
hammered for enumeration or scraping.

Store: Redis when REDIS_URL is set (works across multiple uvicorn workers);
in-memory fallback otherwise. Behavior and limits are identical either way.
If Redis is unreachable, the middleware fails OPEN (requests allowed) and logs
a warning — availability over strictness; Redis going down must not take the
API down. Set REDIS_URL for multi-worker deployments.
"""
import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# (requests, window_seconds) — tuned for an interactive SPA, not a scraper.
LIMITS = [
    (300, 60),      # 300 req/min per IP
    (2000, 3600),   # 2000 req/hour per IP
]
LONGEST = max(w for _, w in LIMITS)
_STORE_KEY_PREFIX = "bk:ratelimit:"


def _client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class _MemoryStore:
    """Single-process sliding window. Correct for one worker only."""

    def __init__(self):
        self._buckets: dict[str, deque] = defaultdict(deque)
        self._last_sweep = 0.0

    def check_and_hit(self, key: str, now: float) -> bool:
        hits = self._buckets[key]
        while hits and hits[0] <= now - LONGEST:
            hits.popleft()
        for limit, window in LIMITS:
            recent = sum(1 for t in hits if t > now - window)
            if recent >= limit:
                return False
        hits.append(now)
        # Opportunistic cleanup so the table can't grow without bound.
        if now - self._last_sweep > 300:
            self._last_sweep = now
            for k in [k for k, v in self._buckets.items()
                      if not v or v[-1] <= now - LONGEST]:
                del self._buckets[k]
        return True


class _RedisStore:
    """Multi-worker sliding window over a Redis sorted set per key.

    Each request is a member scored by its monotonic timestamp; old members
    are trimmed, and per-window counts gate admission. Atomicity per request
    is approximate (trim + counts + add in one pipeline); exact distributed
    fairness isn't required for a throttling layer.
    """

    def __init__(self, client):
        self._client = client

    def _key(self, ip: str) -> str:
        return _STORE_KEY_PREFIX + ip

    def check_and_hit(self, key: str, now: float) -> bool:
        rkey = self._key(key)
        cutoff_longest = now - LONGEST
        member = f"{now:.6f}:{uuid.uuid4().hex}"
        pipe = self._client.pipeline()
        pipe.zremrangebyscore(rkey, 0, cutoff_longest)
        for limit, window in LIMITS:
            pipe.zcount(rkey, now - window, "+inf")
        results = pipe.execute()
        counts = results[1:]
        for (limit, _window), count in zip(LIMITS, counts):
            if count >= limit:
                return False
        self._client.zadd(rkey, {member: now})
        self._client.expire(rkey, LONGEST * 2)
        return True


def build_store():
    """Primary store for the rate limiter.

    Redis when REDIS_URL is set and reachable; in-memory otherwise.
    Never raises — an unusable Redis degrades to the memory store with a
    warning, because the limiter must not prevent the API from booting.
    """
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning(
            "REDIS_URL is not set: rate limiting uses the in-memory store, "
            "which does not share state across workers. Set REDIS_URL for "
            "multi-worker deployments."
        )
        return _MemoryStore()
    try:
        import redis  # optional dependency; only needed when REDIS_URL is set
        client = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
    except Exception as e:
        logger.warning(
            "Redis rate-limit store unreachable (%s); falling back to the "
            "in-memory store.", e
        )
        return _MemoryStore()
    logger.info("Rate limiting: Redis-backed store active.")
    return _RedisStore(client)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, store=None):
        super().__init__(app)
        self._store = store if store is not None else build_store()
        self._redis_fail_logged_at = 0.0

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/api"):
            ip = _client_ip(request)
            allowed = await self._check(ip)
            if not allowed:
                return JSONResponse(
                    {"detail": "Rate limit exceeded. Slow down and try again."},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
        return await call_next(request)

    async def _check(self, ip: str) -> bool:
        """Fail-open: any store error allows the request rather than 500ing."""
        now = time.monotonic()
        try:
            if isinstance(self._store, _RedisStore):
                return await asyncio.to_thread(self._store.check_and_hit, ip, now)
            return self._store.check_and_hit(ip, now)
        except Exception as e:
            if now - self._redis_fail_logged_at > 60:
                self._redis_fail_logged_at = now
                logger.warning("Rate-limit store error (fail-open): %s", e)
            return True
