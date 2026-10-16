"""Collections reminder email delivery: the autopilot actually sends mail now.

Run from the backend directory with: python -m pytest tests/test_collections_delivery.py

No live MongoDB or SMTP needed — the db is faked in-memory and send_email is
mocked. Covers: delivery when configured, graceful queueing when SMTP is
absent, no-recipient handling, and no duplicate sends on retry/flush.
"""
import asyncio
import os
from unittest.mock import AsyncMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_delivery")

from routers import collections as C  # noqa: E402


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


class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q or {}):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([dict(d) for d in self.docs if _matches(d, q or {})])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                for op, changes in update.items():
                    if op == "$set":
                        d.update(changes)
                    elif op == "$push":
                        for f, v in changes.items():
                            d.setdefault(f, []).append(v)
                    elif op == "$inc":
                        for f, v in changes.items():
                            d[f] = d.get(f, 0) + v
                return


class _FakeDB:
    def __init__(self):
        self.payment_reminders = _FakeCollection()
        self.customers = _FakeCollection()
        self.invoices = _FakeCollection()
        self.tenants = _FakeCollection()


def _fx(monkeypatch):
    """Swap in the fake db and a silent audit; returns (fdb, user)."""
    db = _FakeDB()
    monkeypatch.setattr(C, "db", db)
    monkeypatch.setattr(C, "audit", AsyncMock())
    user = {"id": "u1", "tenant_id": "t1", "name": "Test Owner",
            "email": "owner@example.com"}
    return db, user


def _seed(fdb, email="client@example.com"):
    fdb.tenants.docs.append({"id": "t1", "tenant_id": "t1",
                             "name": "Acme Plumbing Co."})
    fdb.customers.docs.append({"id": "c1", "tenant_id": "t1",
                               "name": "Jane Client", "email": email})
    inv = {"id": "inv-abcdef123456", "tenant_id": "t1", "customer_id": "c1",
           "client_name": "Jane Client", "total_cents": 50000,
           "paid_cents": 0, "due_date": "2026-09-01", "status": "issued",
           "autopilot_stages": []}
    fdb.invoices.docs.append(inv)
    return inv


# ---------------- tests ----------------
def test_build_reminder_email_branding():
    subject, body = C._build_reminder_email(
        "Hi Jane,\n\nInvoice #inv-abcd for $500.00 was due.",
        "inv-abcdef123456", 12, "firm", "Acme Plumbing Co.")
    assert "inv-abcd" in subject
    assert "Acme Plumbing Co." in subject or "Acme Plumbing Co." in body
    assert "12 days overdue" in subject
    assert "Mind at Ease Solutions" in body
    low = (subject + body).lower()
    assert "barber" not in low
    assert "emergent" not in low


def test_record_reminder_sends_when_configured(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        inv = _seed(fdb)
        sender = AsyncMock(return_value=("sent", None))
        monkeypatch.setattr(C, "send_email", sender)

        rid = await C._record_reminder(user, inv, "Pay up please.", "nudge", 5)

        sender.assert_awaited_once()
        to, subject, body = sender.await_args.args
        assert to == "client@example.com"
        assert "Pay up please." in body
        rec = fdb.payment_reminders.docs[0]
        assert rec["id"] == rid
        assert rec["email_status"] == "sent"
        assert rec["email_sent_at"]
        assert "nudge" in fdb.invoices.docs[0]["autopilot_stages"]
    asyncio.run(_run())


def test_record_reminder_queues_when_smtp_absent(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        inv = _seed(fdb)
        sender = AsyncMock(return_value=("skipped", "SMTP not configured"))
        monkeypatch.setattr(C, "send_email", sender)

        # must not raise — graceful skip, reminder stays queued
        rid = await C._record_reminder(user, inv, "Pay up please.", "firm", 10)

        sender.assert_awaited_once()
        rec = fdb.payment_reminders.docs[0]
        assert rec["id"] == rid
        assert rec["email_status"] == "queued"
        assert rec["email_error"] == "SMTP not configured"
        # in-app record still exists and the stage is marked done
        assert "firm" in fdb.invoices.docs[0]["autopilot_stages"]
    asyncio.run(_run())


def test_record_reminder_no_recipient(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        inv = _seed(fdb, email="")
        sender = AsyncMock(return_value=("sent", None))
        monkeypatch.setattr(C, "send_email", sender)

        await C._record_reminder(user, inv, "Pay up please.", "nudge", 5)

        sender.assert_not_awaited()
        rec = fdb.payment_reminders.docs[0]
        assert rec["email_status"] == "no_recipient"
    asyncio.run(_run())


def test_no_duplicate_sends_on_retry(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        inv = _seed(fdb)
        sender = AsyncMock(return_value=("sent", None))
        monkeypatch.setattr(C, "send_email", sender)

        await C._record_reminder(user, inv, "First nudge.", "nudge", 5)
        assert sender.await_count == 1

        # flush must not re-send reminders already marked sent
        flushed = await C._flush_queued_reminders(user)
        assert flushed == {"sent": 0, "still_pending": 0}
        assert sender.await_count == 1

        # a queued reminder (SMTP was absent) gets delivered exactly once
        fdb.payment_reminders.docs.append({
            "id": "r-queued", "invoice_id": inv["id"], "tenant_id": "t1",
            "client_name": "Jane Client", "message": "Second nudge.",
            "email_subject": "Friendly reminder", "stage": "firm",
            "via": "autopilot", "days_overdue": 10,
            "created_at": "2026-10-16",
            "email_to": "client@example.com", "email_status": "queued",
            "email_error": "SMTP not configured"})
        flushed = await C._flush_queued_reminders(user)
        assert flushed == {"sent": 1, "still_pending": 0}
        assert sender.await_count == 2

        # and never again
        flushed = await C._flush_queued_reminders(user)
        assert flushed == {"sent": 0, "still_pending": 0}
        assert sender.await_count == 2
        rec = [d for d in fdb.payment_reminders.docs
               if d["id"] == "r-queued"][0]
        assert rec["email_status"] == "sent"
    asyncio.run(_run())


def test_flush_skips_gracefully_when_still_unconfigured(monkeypatch):
    async def _run():
        fdb, user = _fx(monkeypatch)
        inv = _seed(fdb)
        sender = AsyncMock(return_value=("skipped", "SMTP not configured"))
        monkeypatch.setattr(C, "send_email", sender)
        fdb.payment_reminders.docs.append({
            "id": "r-q2", "invoice_id": inv["id"], "tenant_id": "t1",
            "client_name": "Jane Client", "message": "Nudge.",
            "email_subject": "Friendly reminder", "stage": "nudge",
            "via": "autopilot", "days_overdue": 5,
            "created_at": "2026-10-16",
            "email_to": "client@example.com", "email_status": "queued",
            "email_error": "SMTP not configured"})

        flushed = await C._flush_queued_reminders(user)

        assert flushed == {"sent": 0, "still_pending": 1}
        rec = fdb.payment_reminders.docs[0]
        assert rec["email_status"] == "queued"  # stays queued, no crash
    asyncio.run(_run())
