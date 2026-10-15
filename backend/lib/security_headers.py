"""Security headers middleware: HSTS, clickjacking, MIME-sniffing and referrer controls.

The API serves JSON to a first-party SPA; the headers below are the standard
hardening set. CSP is intentionally not set to block-all here because the
frontend legitimately loads external media streams (music player radio);
tighten it if the frontend ever stops doing that.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response: Response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(self), geolocation=()")
        # HSTS only makes sense over HTTPS; harmless on plain HTTP (ignored).
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
