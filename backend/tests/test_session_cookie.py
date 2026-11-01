"""Session cookie attributes: the cookie must be SameSite=None + Secure.

Why: the web app and the API live on different *sites* in production
(bookkeeping-web.onrender.com vs bookkeeping-api-ymh6.onrender.com —
onrender.com is on the Public Suffix List, so each subdomain is its own
site). A SameSite=Lax cookie is withheld by browsers on the app's
cross-site API calls, so /api/auth/me 401s right after a successful
login and the app bounces the user back to /login. SameSite=None +
Secure is the pairing that lets the cookie ride cross-site (the same
pattern the AI Office backend runs live). The clear path must mirror
the attributes or logout cannot delete the cross-site cookie.

No live MongoDB needed — the helpers are exercised against a bare
Starlette Response.

Run from the backend directory with:
    python3 -m pytest tests/test_session_cookie.py -n 0 -q
"""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "testdb")
os.environ.setdefault("JWT_SECRET", "test-secret")

from starlette.responses import Response

from lib.auth import set_session_cookie, clear_session_cookie


def _set_cookie_header(response) -> str:
    return response.headers["set-cookie"].lower()


def test_set_session_cookie_is_cross_site_capable():
    resp = Response()
    set_session_cookie(resp, "tok-123")
    header = _set_cookie_header(resp)
    assert header.startswith("access_token=tok-123")
    assert "samesite=none" in header
    assert "samesite=lax" not in header
    assert "secure" in header
    assert "httponly" in header
    assert "path=/" in header
    assert "max-age=604800" in header


def test_clear_session_cookie_mirrors_attributes_and_expires():
    resp = Response()
    clear_session_cookie(resp)
    header = _set_cookie_header(resp)
    # Empty value + zero max-age is what actually deletes the cookie.
    assert header.startswith("access_token=")
    assert 'access_token=""' in header or "access_token=;" in header
    assert "max-age=0" in header
    # Mirrored attributes, so the deletion targets the same cross-site
    # cookie that set_session_cookie issued.
    assert "samesite=none" in header
    assert "samesite=lax" not in header
    assert "secure" in header
    assert "path=/" in header
