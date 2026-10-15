"""Field-level encryption + PII helpers.

Symmetric encryption via Fernet, keyed by TOKEN_ENCRYPTION_KEY (same key the
Plaid bank-feed integration uses — one key to manage, documented in
PLAID_SETUP.md). Used for: TOTP secrets, contractor tax IDs, team payout
accounts, encrypted backups.

Nothing here logs or returns plaintext. Decryption happens server-side only,
behind explicit permission checks in the routers.
"""

import os

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
