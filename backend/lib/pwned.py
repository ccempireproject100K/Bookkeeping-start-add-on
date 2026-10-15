"""Breached-password screening via the Have I Been Pwned k-anonymity API.

Only the first 5 hex chars of the SHA-1 go over the wire, so the password
(and even its full hash) never leaves the server. Fail-open: if the HIBP
API is unreachable, the password is accepted — availability beats a
best-effort reputation check, and the event is logged.
"""
import hashlib
import logging

import requests

log = logging.getLogger(__name__)
_HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{}"
_TIMEOUT = 5


def is_pwned(password: str) -> bool:
    """True if the password appears in a known breach corpus."""
    try:
        digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
        prefix, suffix = digest[:5], digest[5:]
        resp = requests.get(_HIBP_RANGE_URL.format(prefix), timeout=_TIMEOUT,
                            headers={"User-Agent": "bookkeeping-security-check"})
        resp.raise_for_status()
        for line in resp.text.splitlines():
            h, _, _count = line.partition(":")
            if h.strip().upper() == suffix:
                return True
        return False
    except Exception as e:  # network or API issue -> fail open
        log.warning("HIBP check failed, allowing password: %s", e)
        return False
