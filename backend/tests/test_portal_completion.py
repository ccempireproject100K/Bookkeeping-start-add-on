"""Portal completion: link-management UI endpoints + email delivery.

Run from the backend directory with: python -m pytest tests/test_portal_completion.py

No live MongoDB or SMTP needed — the db is faked in-memory and send_email is
mocked. Covers: email branding, delivery when configured, graceful queueing
when SMTP is absent, no-recipient handling, token rotation on resend,
revoke, and the mailer multipart extension.
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_portal")

from routers import portal as P  # noqa: E402


# ---------------- fake in-memory db ----------------
def _matches(doc, q):
    for k, v in q.items():
        if k == "tenant_id":
            continue
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _UpdateResult:
    def __init__(self, matched):
        self.matched_count = matched


class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q or {}):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        docs = [dict(d) for d in self.docs if _matches(d, q or {})]
        if projection:
            excluded = {k for k, v in projection.items() if v == 0}
            if excluded:
                docs = [{k: v for k, v in d.items() if k not in excluded}
                        for d in docs]
        return _Cursor(docs)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update):
        matched = 0
        for d in self.docs:
            if _matches(d, q):
                matched += 1
                for op, changes in update.items():
                    if op == "$set":
                        d.update(changes)
                    elif op == "$push":
                        for f, v in changes.items():
                            d.setdefault(f, []).append(v)
        return _UpdateResult(matched)


class _FakeDB:
    def __init__(self):
        self.portal_links = _FakeCollection()
        self.customers = _FakeCollection()
        self.invoices = _FakeCollection()
        self.tenants = _FakeCollection()
        self.audit_log = _FakeCollection()


def _fx(monkeypatch):
    """Swap in the fake db; returns (fdb, user)."""
    fdb = _FakeDB()
    monkeypatch.setattr(P, "db", fdb)
    user = {"id": "u1", "tenant_id": "t1", "name": "Test Owner",
            "email": "owner@example.com"}
    return fdb, user


def _seed(fdb, email="client@example.com"):
    fdb.tenants.docs.append({"id": "t1", "tenant_id": "t1",
                             "name": "Acme Plumbing Co."})
    fdb.customers.docs.append({"id": "c1", "tenant_id": "t1",
                               "name": "Jane Client", "email": email})
    inv = {"id": "inv-abcdef123456", "tenant_id": "t1", "customer_id": "c1",
           "client_name": "Jane Client", "total_cents": 50000,
           "paid_cents": 0, "status": "issued", "issue_entry": "INV-0007",
           "created_at": "2026-10-01T00:00:00"}
    fdb.invoices.docs.append(inv)
    return inv


def _link_body(**kw):
    b = {"invoice_id": "inv-abcdef123456", "expires_in_hours": 168,
         "send_email": False, "origin_url": "https://books.example.com"}
    b.update(kw)
    return P.LinkIn(**b)


# ---------------- tests ----------------
def test_build_portal_email_branding():
    subject, text, html = P._build_portal_email(
        "Acme Plumbing Co.", "https://books.example.com/p/abc123",
        "INV-0007", "Jane Client", 50000, "2026-10-23T00:00:00")
    assert "Acme Plumbing Co." in subject
    assert "INV-0007" in subject
    assert "https://books.example.com/p/abc123" in text
    assert "https://books.example.com/p/abc123" in html
    assert "$500.00" in text
    assert "Mind at Ease Solutions" in text
    assert "Mind at Ease Solutions" in html
    low = (subject + text + html).lower()
    assert "barber" not in low
    assert "emergent" not in low


def test_create_link_sends_when_configured(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        _seed(fdb)
        sender = AsyncMock(return_value=("sent", None))
        monkeypatch.setattr(P, "send_email", sender)

        res = await P.create_link(_link_body(send_email=True), user, None)

        sender.assert_awaited_once()
        to, subject, text, html = sender.await_args.args
        assert to == "client@example.com"
        assert "/p/" in text and "/p/" in html
        assert res["email_status"] == "sent"
        assert res["portal_url"].startswith("https://books.example.com/p/")
        doc = fdb.portal_links.docs[0]
        assert doc["email_status"] == "sent"
        assert doc["email_to"] == "client@example.com"
        assert doc["email_sent_at"]
    asyncio.run(_run())


def test_create_link_queues_when_smtp_absent(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        _seed(fdb)
        sender = AsyncMock(return_value=("skipped", "SMTP not configured"))
        monkeypatch.setattr(P, "send_email", sender)

        res = await P.create_link(_link_body(send_email=True), user, None)

        assert res["email_status"] == "queued"
        doc = fdb.portal_links.docs[0]
        assert doc["email_status"] == "queued"
        assert doc["email_sent_at"] is None
    asyncio.run(_run())


def test_create_link_no_recipient(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        _seed(fdb, email="")
        sender = AsyncMock(return_value=("sent", None))
        monkeypatch.setattr(P, "send_email", sender)

        res = await P.create_link(_link_body(send_email=True), user, None)

        sender.assert_not_awaited()
        assert res["email_status"] == "no_recipient"
    asyncio.run(_run())


def test_create_link_without_send_email_skips_mailer(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        _seed(fdb)
        sender = AsyncMock(return_value=("sent", None))
        monkeypatch.setattr(P, "send_email", sender)

        res = await P.create_link(_link_body(send_email=False), user, None)

        sender.assert_not_awaited()
        assert res["email_status"] == "not_sent"
    asyncio.run(_run())


def test_send_rotates_token_and_delivers(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        _seed(fdb)
        sender = AsyncMock(return_value=("sent", None))
        monkeypatch.setattr(P, "send_email", sender)

        created = await P.create_link(_link_body(send_email=False), user, None)
        old_hash = fdb.portal_links.docs[0]["token_hash"]

        res = await P.send_link(created["link_id"], P.LinkSendIn(
            origin_url="https://books.example.com"), user, None)

        assert res["email_status"] == "sent"
        assert res["token"] != created["token"]  # fresh token
        doc = fdb.portal_links.docs[0]
        assert doc["token_hash"] != old_hash  # old URL stops working
        assert doc["email_sent_count"] == 1
        sender.assert_awaited_once()
    asyncio.run(_run())


def test_send_on_revoked_link_conflicts(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        _seed(fdb)
        created = await P.create_link(_link_body(), user, None)
        await P.revoke_link(created["link_id"], user)
        try:
            await P.send_link(created["link_id"], P.LinkSendIn(), user, None)
            raise AssertionError("expected HTTPException")
        except Exception as e:
            assert getattr(e, "status_code", None) == 409
    asyncio.run(_run())


def test_list_links_reports_email_status(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        _seed(fdb)
        sender = AsyncMock(return_value=("skipped", "SMTP not configured"))
        monkeypatch.setattr(P, "send_email", sender)
        await P.create_link(_link_body(send_email=True), user, None)

        rows = await P.list_links(user)

        assert len(rows) == 1
        assert rows[0]["email_status"] == "queued"
        assert rows[0]["status"] == "active"
        assert "token_hash" not in rows[0]
    asyncio.run(_run())


def test_mailer_multipart_extension(monkeypatch):
    """send_email accepts optional html_body and sends multipart/alternative."""
    import lib.mailer as M
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    captured = {}

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def send_message(self, msg):
            captured["msg"] = msg

    monkeypatch.setattr(M.smtplib, "SMTP", _FakeSMTP)

    async def _run():
        status, err = await M.send_email("a@b.c", "Subj", "plain text",
                                         "<b>html</b>")
        assert status == "sent"
        msg = captured["msg"]
        assert msg.is_multipart()
        parts = list(msg.iter_parts())
        assert any(p.get_content_type() == "text/plain" for p in parts)
        assert any(p.get_content_type() == "text/html" for p in parts)

        # backward compatible: no html -> single part
        status, err = await M.send_email("a@b.c", "Subj", "plain text")
        assert status == "sent"
        assert not captured["msg"].is_multipart()

    asyncio.run(_run())


def test_mailer_still_key_gated():
    """Without SMTP_HOST the mailer stays a graceful no-op (unchanged behavior)."""
    import lib.mailer as M
    # ensure the env var is absent for this test
    old = os.environ.pop("SMTP_HOST", None)
    try:
        async def _run():
            status, err = await M.send_email("a@b.c", "Subj", "body", "<b>h</b>")
            assert status == "skipped"
            assert err == "SMTP not configured"
        asyncio.run(_run())
    finally:
        if old is not None:
            os.environ["SMTP_HOST"] = old
