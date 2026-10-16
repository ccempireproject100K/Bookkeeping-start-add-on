"""Security headers middleware: HSTS, clickjacking, MIME-sniffing, referrer controls,
and a Content-Security-Policy.

The API serves JSON to a first-party SPA; the headers below are the standard
hardening set. CSP allowlists exactly what the frontend demonstrably needs:
same-origin scripts/styles, Google Fonts (index.html links
fonts.googleapis.com / fonts.gstatic.com), the Plaid Link CDN
(https://cdn.plaid.com/link/v2/stable/link-initialize.js, injected at runtime
by frontend/src/pages/Banking.jsx — hence cdn.plaid.com in script-src and
frame-src, and *.plaid.com in connect-src for Link's API calls), and the music
player's radio streams (frontend/src/components/MusicPlayer.jsx: ice1.somafm.com,
stream.radioparadise.com, kexp-mp3-128.streamguys1.com in media-src).

'unsafe-inline' appears ONLY in style-src: React applies inline style
attributes, which CSP governs under style-src. Scripts need neither
unsafe-inline (CRA production builds load external bundles only) nor
unsafe-eval (no dynamic code generation). Everything else is 'self' or locked
down (object-src 'none', frame-ancestors 'none').
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://cdn.plaid.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "connect-src 'self' https://*.plaid.com",
    "frame-src 'self' https://cdn.plaid.com https://*.plaid.com",
    "media-src 'self' https://ice1.somafm.com https://stream.radioparadise.com "
    "https://kexp-mp3-128.streamguys1.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


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


class ContentSecurityPolicyMiddleware(BaseHTTPMiddleware):
    """Emits the Content-Security-Policy documented above on every response."""
    async def dispatch(self, request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response
