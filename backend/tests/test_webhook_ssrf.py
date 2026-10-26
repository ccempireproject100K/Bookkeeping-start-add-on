"""Marketplace webhook SSRF lock (security audit 2026-10-26, N-2;
owner ruling: the straight lock, no self-hosting carve-out).

Run from the backend directory with:
    python -m pytest tests/test_webhook_ssrf.py

Destinations are https-only and must resolve ONLY to public
addresses — at save time, and again inside delivery on every hop
(redirects are followed manually and each target is re-validated
and re-resolved before it is requested). DNS is pinned through
lib.developer._resolve_ips and HTTP through a requests stub: no real
network anywhere in this file.
"""
import asyncio
import ipaddress
import os
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_webhook_ssrf")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from lib import audit as audit_mod  # noqa: E402
from lib import developer as dev  # noqa: E402
from routers import developers as developers_r  # noqa: E402
from test_marketplace_developers import FakeDB, create_webhook, make_user  # noqa: E402

PUBLIC = [ipaddress.ip_address("93.184.215.14")]
RESOLVER = {
    "hooks.example.com": PUBLIC,
    "a.example.com": PUBLIC,
    "b.example.com": PUBLIC,
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def fdb(monkeypatch):
    fake = FakeDB()
    for mod in (developers_r, dev, audit_mod):
        monkeypatch.setattr(mod, "db", fake)
    monkeypatch.setattr(dev, "_resolve_ips",
                        lambda host: list(RESOLVER.get(host, [])))
    return fake


# ---------------- save time ----------------
def test_save_rejects_ip_literal_targets(fdb):
    for url in ("https://169.254.169.254/hook",   # link-local / metadata
                "https://127.0.0.1/hook",          # loopback
                "https://10.1.2.3/hook",           # RFC1918
                "https://192.168.0.9/hook",
                "https://[::1]/hook"):
        with pytest.raises(HTTPException) as e:
            create_webhook(fdb, url=url)
        assert e.value.status_code == 422, url


def test_save_rejects_http_even_for_public_host(fdb):
    with pytest.raises(HTTPException) as e:
        create_webhook(fdb, url="http://hooks.example.com/mae")
    assert e.value.status_code == 422


def test_save_rejects_hostname_resolving_private(fdb, monkeypatch):
    monkeypatch.setattr(dev, "_resolve_ips",
                        lambda host: [ipaddress.ip_address("10.0.0.9")])
    with pytest.raises(HTTPException) as e:
        create_webhook(fdb, url="https://hooks.example.com/mae")
    assert e.value.status_code == 422


def test_save_accepts_public_https(fdb):
    out = create_webhook(fdb, url="https://hooks.example.com/mae")
    assert out["url"] == "https://hooks.example.com/mae"
    assert out["signing_secret"]


# ---------------- delivery time ----------------
class _Resp:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}


class _Http:
    """requests stub: records every URL actually requested and
    answers from a script of {url: _Resp}."""
    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None,
             allow_redirects=True):
        self.calls.append(url)
        resp = self.script.get(url)
        if resp is None:
            return _Resp(200)
        return resp


def _subscription(fdb, url):
    doc, _secret = dev.new_subscription_doc(
        tenant_id="t1", created_by="owner@t1.example", url=url,
        events=["invoice.created"])
    fdb.webhook_subscriptions.docs.append(doc)
    return doc


def test_post_refuses_private_literal_without_sending(fdb, monkeypatch):
    http = _Http()
    monkeypatch.setattr(dev, "requests", SimpleNamespace(post=http.post))
    with pytest.raises(dev.WebhookUrlError):
        dev._post("https://169.254.169.254/hook", b"{}", {})
    assert http.calls == []  # _post raises before requests is touched


def test_delivery_rechecks_resolution_at_send_time(fdb, monkeypatch):
    """Saved while the name resolved publicly; by delivery time DNS
    answers privately (rebinding). The delivery must refuse and the
    HTTP layer must never be invoked."""
    sub = _subscription(fdb, "https://hooks.example.com/mae")
    monkeypatch.setattr(dev, "_resolve_ips",
                        lambda host: [ipaddress.ip_address("127.0.0.1")])
    http = _Http()
    monkeypatch.setattr(dev, "requests", SimpleNamespace(post=http.post))

    async def go():
        return await dev.deliver_to_subscription(
            sub, "t1", "webhook.test", {})
    delivery = run(go())
    assert delivery["status"] != "delivered"
    assert "non-public" in (delivery["last_error"] or "")
    assert http.calls == []


def test_redirect_to_private_is_refused(fdb, monkeypatch):
    http = _Http({
        "https://a.example.com/start": _Resp(
            302, "https://169.254.169.254/latest/meta-data"),
    })
    monkeypatch.setattr(dev, "requests", SimpleNamespace(post=http.post))
    with pytest.raises(dev.WebhookUrlError):
        dev._post("https://a.example.com/start", b"{}", {})
    # Only the first, public hop was ever requested.
    assert http.calls == ["https://a.example.com/start"]


def test_redirect_to_public_https_is_followed(fdb, monkeypatch):
    http = _Http({
        "https://a.example.com/start": _Resp(
            302, "https://b.example.com/final"),
        "https://b.example.com/final": _Resp(200),
    })
    monkeypatch.setattr(dev, "requests", SimpleNamespace(post=http.post))
    assert dev._post("https://a.example.com/start", b"{}", {}) == 200
    assert http.calls == ["https://a.example.com/start",
                          "https://b.example.com/final"]


def test_redirect_to_http_is_refused(fdb, monkeypatch):
    http = _Http({
        "https://a.example.com/start": _Resp(
            302, "http://b.example.com/final"),
    })
    monkeypatch.setattr(dev, "requests", SimpleNamespace(post=http.post))
    with pytest.raises(dev.WebhookUrlError):
        dev._post("https://a.example.com/start", b"{}", {})
    assert http.calls == ["https://a.example.com/start"]


def test_plain_public_delivery_still_works(fdb, monkeypatch):
    http = _Http()
    monkeypatch.setattr(dev, "requests", SimpleNamespace(post=http.post))
    sub = _subscription(fdb, "https://hooks.example.com/mae")

    async def go():
        return await dev.deliver_to_subscription(
            sub, "t1", "webhook.test", {})
    delivery = run(go())
    assert delivery["status"] == "delivered"
    assert delivery["last_status"] == 200
    assert http.calls == ["https://hooks.example.com/mae"]
