"""Field-level encryption + PII helpers.

Symmetric encryption via Fernet, keyed by TOKEN_ENCRYPTION_KEY (same key the
Plaid bank-feed integration uses — one key to manage, documented in
PLAID_SETUP.md). Used for: TOTP secrets, contractor tax IDs, team payout
accounts, encrypted backups.

Nothing here logs or returns plaintext. Decryption happens server-side only,
behind explicit permission checks in the routers.
"""

import os
import re

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException


def _fernet() -> Fernet:
    key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise HTTPException(503, "TOKEN_ENCRYPTION_KEY is not set. Generate one with "
                                 "python -c \"from cryptography.fernet import Fernet; "
                                 "print(Fernet.generate_key().decode())\" and set it in the server env.")
    try:
        return Fernet(key.encode())
    except Exception:
        raise HTTPException(500, "TOKEN_ENCRYPTION_KEY is not a valid Fernet key.")


def pii_encrypt(plaintext: str) -> str:
    """Encrypt a short string field. Returns the Fernet token as text."""
    return _fernet().encrypt(plaintext.encode()).decode()


def pii_decrypt(token: str) -> str:
    """Decrypt a field encrypted with pii_encrypt."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise HTTPException(500, "Could not decrypt a stored value. Check TOKEN_ENCRYPTION_KEY.")


def pii_encrypt_bytes(raw: bytes) -> bytes:
    return _fernet().encrypt(raw)


def pii_decrypt_bytes(raw: bytes) -> bytes:
    try:
        return _fernet().decrypt(raw)
    except InvalidToken:
        raise HTTPException(500, "Could not decrypt a stored value. Check TOKEN_ENCRYPTION_KEY.")


def last4(value: str | None) -> str:
    v = (value or "").strip()
    return v[-4:] if v else ""


def masked(value: str | None) -> str:
    """Display form: •••1234. Never the full value."""
    l4 = last4(value)
    return f"•••{l4}" if l4 else "—"


# ---------------- masking for logs / error bodies ----------------
# These helpers keep PII and secrets out of logs and client-visible error
# bodies. Rule of thumb: logs get masked values, never plaintext.

def mask_email(email: str | None) -> str:
    """j***@example.com — enough to identify the record, not the person."""
    e = (email or "").strip()
    if "@" not in e:
        return "—" if not e else "***"
    local, domain = e.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


# Patterns that look like secrets/keys when they show up in free text
# (provider error bodies, exception messages). Deliberately broad.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,})"),                       # OpenAI-style
    re.compile(r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/=]{16,})"),           # bearer tokens
    re.compile(r"(?i)(api[_-]?key|client[_-]?secret|secret)\s*[:=]\s*['\"]?([^\s'\"]{8,})['\"]?"),
    re.compile(r"\b(gAAAAA[A-Za-z0-9_\-]{20,})\b"),                       # Fernet tokens
    re.compile(r"\b(eyJ[A-Za-z0-9_\-]{16,})\b"),                          # JWTs
]


def scrub_secrets(text: str | None) -> str:
    """Redact anything secret-looking from free text before logging it or
    returning it in an error body. Best-effort: masks known shapes, does not
    guarantee a determined reader learns nothing."""
    t = text or ""

    def _redact(m: "re.Match") -> str:
        # Keep the label/prefix (e.g. "Bearer ", "api_key="), redact the value.
        last = m.lastindex or 0
        prefix_end = (m.start(last) - m.start(0)) if last else 0
        return m.group(0)[:prefix_end] + "***REDACTED***"

    for pat in _SECRET_PATTERNS:
        t = pat.sub(_redact, t)
    return t


def mask_pii(value, kind: str = "generic"):
    """Dispatch masking by kind: 'email', 'account'/'tin' (last-4), 'generic'."""
    if kind == "email":
        return mask_email(value if isinstance(value, str) else None)
    if kind in ("account", "tin", "ssn", "routing"):
        return masked(value if isinstance(value, str) else None)
    if value is None:
        return "—"
    s = str(value)
    return "***" if s else "—"


def redact_mapping(mapping: dict, keys: tuple[str, ...]) -> dict:
    """Copy of a mapping with the named keys masked for safe logging."""
    redacted = dict(mapping or {})
    for k in keys:
        if k in redacted:
            redacted[k] = mask_pii(redacted[k], "email" if "email" in k else "generic")
    return redacted
