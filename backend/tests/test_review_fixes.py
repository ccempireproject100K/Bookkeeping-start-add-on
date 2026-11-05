"""Pins for the 2026-11-04 review fixes: signing-secret strength and the CFDI
provider interface actually being defined.

Run from the backend directory with:
    python3 -m pytest tests/test_review_fixes.py -n 0 -q
"""
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_review_fixes")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from lib.auth import MIN_SECRET_LEN, check_jwt_secret


@pytest.mark.parametrize("secret", ["", "short", "x" * (MIN_SECRET_LEN - 1)])
def test_weak_or_missing_jwt_secret_refuses_to_start(secret):
    with pytest.raises(RuntimeError, match="JWT_SECRET must be set to at least"):
        check_jwt_secret({"JWT_SECRET": secret})


def test_missing_jwt_secret_refuses_to_start():
    with pytest.raises(RuntimeError):
        check_jwt_secret({})


def test_long_jwt_secret_is_accepted():
    check_jwt_secret({"JWT_SECRET": "x" * MIN_SECRET_LEN})


def test_server_startup_checks_the_secret_first():
    src = open(os.path.join(os.path.dirname(__file__), "..", "server.py")).read()
    body = src.split("async def startup():", 1)[1]
    assert body.index("check_jwt_secret()") < body.index("indexes = [")


def test_timbrado_provider_protocol_is_defined():
    # It used to raise NameError on forward references inside a swallowed try.
    import lib.timbrado as T
    assert hasattr(T, "TimbradoProvider")
    assert "stamp" in dir(T.TimbradoProvider)
