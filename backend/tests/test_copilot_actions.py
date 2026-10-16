"""Copilot action expansion: propose -> approve -> effect for the new kinds.

Run from the backend directory with: python -m pytest tests/test_copilot_actions.py

No live MongoDB or SMTP needed — the db is faked in-memory and send_email is
mocked. Covers:
- propose -> approve -> effect for categorize_transaction, post_draft_entry,
  reconcile_match, and send_reminder (sent + queued-when-unconfigured +
  no_recipient)
- approval is permission-gated per action kind: an unauthorized role's
  approval is rejected with 403 and nothing executes
- proposing stays open to any authenticated role
- already-decided actions cannot be approved again
- every proposal preview names exactly what will change
"""
import asyncio
import os
from unittest.mock import AsyncMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_copilot_actions")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from routers import copilot as COP  # noqa: E402
from routers import banking as BK  # noqa: E402
from routers import collections as COL  # noqa: E402


# ---------------- fake in-memory db ----------------
def _matches(doc, q):
    for k, v in q.items():
        if k == "tenant_id":
            continue
        if isinstance(v, dict):
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
            if "$gte" in v and not (doc.get(k) is not None and doc.get(k) >= v["$gte"]):
                return False
            if "$lte" in v and not (doc.get(k) is not None and doc.get(k) <= v["$lte"]):
                return False
            if "$ne" in v and doc.get(k) == v["$ne"]:
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

    async def insert_many(self, docs):
        for doc in docs:
            self.docs.append(dict(doc))

    def _apply(self, d, update):
        for op, changes in update.items():
            if op == "$set":
                d.update(changes)
            elif op == "$push":
                for f, v in changes.items():
                    d.setdefault(f, []).append(v)
            elif op == "$inc":
                for f, v in changes.items():
                    d[f] = d.get(f, 0) + v

    async def update_one(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                return

    async def update_many(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)

    async def find_one_and_update(self, q, update, upsert=False,
                                  return_document=False):
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                return dict(d)
        if upsert:
            d = dict(q)
            self._apply(d, update)
            self.docs.append(d)
            return dict(d)
        return None


class _FakeDB:
    def __init__(self):
        self.copilot_actions = _FakeCollection()
        self.audit_log = _FakeCollection()
        self.invoices = _FakeCollection()
        self.bills = _FakeCollection()
        self.products = _FakeCollection()
        self.bank_accounts = _FakeCollection()
        self.bank_transactions = _FakeCollection()
        self.bank_rules = _FakeCollection()
        self.journal_entries = _FakeCollection()
        self.journal_lines = _FakeCollection()
        self.fiscal_periods = _FakeCollection()
        self.accounts = _FakeCollection()
        self.counters = _FakeCollection()
        self.payment_reminders = _FakeCollection()
        self.reminders = _FakeCollection()
        self.draft_payments = _FakeCollection()
        self.customers = _FakeCollection()
        self.tenants = _FakeCollection()


OWNER = {"id": "u-owner", "tenant_id": "t1", "name": "Test Owner",
         "email": "owner@example.com", "role": "owner"}
ACCOUNTANT = {"id": "u-acct", "tenant_id": "t1", "name": "Test Accountant",
              "email": "acct@example.com", "role": "accountant"}
BOOKKEEPER = {"id": "u-bk", "tenant_id": "t1", "name": "Test Bookkeeper",
              "email": "bk@example.com", "role": "bookkeeper"}
BARBER = {"id": "u-barber", "tenant_id": "t1", "name": "Test Barber",
          "email": "barber@example.com", "role": "barber"}


def _fx(monkeypatch):
    """Swap the fake db into copilot, banking and collections; returns fdb."""
    fdb = _FakeDB()
    monkeypatch.setattr(COP, "db", fdb)
    monkeypatch.setattr(BK, "db", fdb)
    monkeypatch.setattr(COL, "db", fdb)
    # lib.audit writes to the real db module — silence it (it never raises anyway)
    monkeypatch.setattr(COL, "audit", AsyncMock())
    return fdb


def _seed_chart(fdb):
    fdb.accounts.docs.extend([
        {"id": "a1", "tenant_id": "t1", "code": "1020",
         "name": "Cash — Bank Checking", "type": "asset"},
        {"id": "a2", "tenant_id": "t1", "code": "5400",
         "name": "Utilities", "type": "expense"},
    ])


def _seed_categorize(fdb):
    _seed_chart(fdb)
    fdb.bank_accounts.docs.append(
        {"id": "ba1", "tenant_id": "t1", "name": "Checking", "gl_code": "1020"})
    fdb.bank_transactions.docs.append(
        {"id": "txn1", "tenant_id": "t1", "bank_account_id": "ba1",
         "txn_date": "2026-10-10", "description": "City Power & Light",
         "amount_cents": -4210, "status": "unmatched",
         "suggested_code": "5400", "category_code": "5400",
         "entry_id": None, "matched_entry_id": None})


def _seed_draft(fdb):
    _seed_chart(fdb)
    fdb.journal_entries.docs.append(
        {"id": "je-draft1", "tenant_id": "t1", "number": None,
         "entry_date": "2026-10-12", "status": "draft", "source": "manual",
         "memo": "Adjust utilities accrual",
         "total_cents": 4210, "created_at": "2026-10-12"})
    fdb.journal_lines.docs.extend([
        {"id": "jl1", "tenant_id": "t1", "entry_id": "je-draft1",
         "entry_number": None, "entry_date": "2026-10-12",
         "account_code": "5400", "debit": 4210, "credit": 0,
         "memo": "accrual", "status": "draft"},
        {"id": "jl2", "tenant_id": "t1", "entry_id": "je-draft1",
         "entry_number": None, "entry_date": "2026-10-12",
         "account_code": "1020", "debit": 0, "credit": 4210,
         "memo": "accrual", "status": "draft"},
    ])


def _seed_match(fdb):
    _seed_chart(fdb)
    fdb.bank_transactions.docs.append(
        {"id": "txn2", "tenant_id": "t1", "bank_account_id": "ba1",
         "txn_date": "2026-10-11", "description": "ACH Deposit Client",
         "amount_cents": 250000, "status": "unmatched",
         "suggested_code": None, "category_code": None,
         "entry_id": None, "matched_entry_id": None})
    fdb.journal_entries.docs.append(
        {"id": "je-posted1", "tenant_id": "t1", "number": "JE-2026-000001",
         "entry_date": "2026-10-09", "status": "posted", "source": "manual",
         "memo": "Client payment received", "total_cents": 250000,
         "created_at": "2026-10-09"})
    fdb.journal_lines.docs.extend([
        {"id": "jlm1", "tenant_id": "t1", "entry_id": "je-posted1",
         "entry_number": "JE-2026-000001", "entry_date": "2026-10-09",
         "account_code": "1020", "debit": 250000, "credit": 0,
         "memo": "deposit", "status": "posted"},
        {"id": "jlm2", "tenant_id": "t1", "entry_id": "je-posted1",
         "entry_number": "JE-2026-000001", "entry_date": "2026-10-09",
         "account_code": "1200", "debit": 0, "credit": 250000,
         "memo": "deposit", "status": "posted"},
    ])


def _seed_reminder(fdb, email="client@example.com"):
    fdb.tenants.docs.append({"id": "t1", "tenant_id": "t1",
                             "name": "Acme Plumbing Co."})
    fdb.customers.docs.append({"id": "c1", "tenant_id": "t1",
                               "name": "Jane Client", "email": email})
    fdb.invoices.docs.append(
        {"id": "inv-abcdef123456", "tenant_id": "t1", "customer_id": "c1",
         "client_name": "Jane Client", "total_cents": 50000, "paid_cents": 0,
         "due_date": "2026-09-01", "status": "issued", "autopilot_stages": []})


def _run(coro):
    return asyncio.run(coro)


def _propose_one(monkeypatch, user, kind, seed):
    fdb = _fx(monkeypatch)
    seed(fdb)
    proposed = _run(COP.propose_actions(user))
    docs = [d for d in proposed if d["kind"] == kind]
    assert len(docs) == 1, f"expected one {kind} proposal, got {len(docs)}"
    return fdb, docs[0]


# ---------------- new actions: propose -> approve -> effect ----------------
def test_categorize_transaction_propose_approve_effect(monkeypatch):
    fdb, doc = _propose_one(monkeypatch, OWNER, "categorize_transaction",
                            _seed_categorize)
    # preview names exactly what will change
    assert "5400" in doc["detail"] and "cleared" in doc["detail"]
    assert doc["payload"] == {"txn_id": "txn1", "category_code": "5400"}

    done = _run(COP.approve_action(doc["id"], OWNER))

    assert done["status"] == "executed"
    assert done["decided_by"] == "owner@example.com"
    # effect: one posted journal entry, txn marked cleared
    entries = [e for e in fdb.journal_entries.docs if e["status"] == "posted"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["total_cents"] == 4210
    lines = [l for l in fdb.journal_lines.docs if l["entry_id"] == entry["id"]]
    assert len(lines) == 2
    dr = [l for l in lines if l["debit"] > 0][0]
    assert dr["account_code"] == "5400" and dr["debit"] == 4210  # money out
    txn = fdb.bank_transactions.docs[0]
    assert txn["status"] == "cleared"
    assert txn["entry_id"] == entry["id"]


def test_post_draft_entry_propose_approve_effect(monkeypatch):
    fdb, doc = _propose_one(monkeypatch, OWNER, "post_draft_entry", _seed_draft)
    assert "posts it to the ledger" in doc["detail"]
    assert "final" in doc["detail"]  # posting is final
    assert doc["payload"] == {"entry_id": "je-draft1"}

    done = _run(COP.approve_action(doc["id"], OWNER))

    assert done["status"] == "executed"
    entry = [e for e in fdb.journal_entries.docs if e["id"] == "je-draft1"][0]
    assert entry["status"] == "posted"
    assert entry["number"]  # got a real JE number
    assert entry["posted_by"] == "owner@example.com"
    # still exactly one entry — approve posted the existing draft, never re-created it
    assert len(fdb.journal_entries.docs) == 1


def test_reconcile_match_propose_approve_effect(monkeypatch):
    fdb, doc = _propose_one(monkeypatch, OWNER, "reconcile_match", _seed_match)
    assert "matched" in doc["detail"]
    assert "JE-2026-000001" in doc["detail"]
    assert doc["payload"] == {"txn_id": "txn2", "entry_id": "je-posted1"}

    done = _run(COP.approve_action(doc["id"], OWNER))

    assert done["status"] == "executed"
    txn = fdb.bank_transactions.docs[0]
    assert txn["status"] == "matched"
    assert txn["matched_entry_id"] == "je-posted1"


def test_send_reminder_sends_when_configured(monkeypatch):
    fdb = _fx(monkeypatch)
    _seed_reminder(fdb)
    sender = AsyncMock(return_value=("sent", None))
    monkeypatch.setattr(COL, "send_email", sender)

    proposed = _run(COP.propose_actions(OWNER))
    docs = [d for d in proposed if d["kind"] == "send_reminder"]
    assert len(docs) == 1
    doc = docs[0]
    # preview: an actual email will be sent
    assert "SENDS" in doc["detail"]
    assert "client@example.com" in doc["detail"]

    done = _run(COP.approve_action(doc["id"], OWNER))

    assert done["status"] == "executed"
    sender.assert_awaited_once()
    to, _subject, _body = sender.await_args.args
    assert to == "client@example.com"
    rec = fdb.payment_reminders.docs[0]
    assert rec["email_status"] == "sent"
    assert "sent to client@example.com" in done["result"]


def test_send_reminder_queues_when_smtp_absent(monkeypatch):
    fdb = _fx(monkeypatch)
    _seed_reminder(fdb)
    sender = AsyncMock(return_value=("skipped", "SMTP not configured"))
    monkeypatch.setattr(COL, "send_email", sender)

    proposed = _run(COP.propose_actions(OWNER))
    doc = [d for d in proposed if d["kind"] == "send_reminder"][0]

    # must not raise — graceful queue, same as the collections autopilot
    done = _run(COP.approve_action(doc["id"], OWNER))

    assert done["status"] == "executed"
    sender.assert_awaited_once()
    rec = fdb.payment_reminders.docs[0]
    assert rec["email_status"] == "queued"
    assert rec["email_error"] == "SMTP not configured"
    assert "queued" in done["result"]


def test_send_reminder_no_recipient(monkeypatch):
    fdb = _fx(monkeypatch)
    _seed_reminder(fdb, email="")
    sender = AsyncMock(return_value=("sent", None))
    monkeypatch.setattr(COL, "send_email", sender)

    proposed = _run(COP.propose_actions(OWNER))
    doc = [d for d in proposed if d["kind"] == "send_reminder"][0]
    assert "no email on file" in doc["detail"]

    done = _run(COP.approve_action(doc["id"], OWNER))

    assert done["status"] == "executed"
    sender.assert_not_awaited()
    rec = fdb.payment_reminders.docs[0]
    assert rec["email_status"] == "no_recipient"
    assert "no customer email on file" in done["result"]


# ---------------- approval is permission-gated ----------------
def _pending_action(fdb, kind, payload):
    doc = {"id": f"act-{kind}", "kind": kind, "title": "t", "detail": "d",
           "brief": {}, "payload": payload, "status": "pending",
           "created_at": "2026-10-16", "decided_at": None,
           "decided_by": None, "result": None}
    fdb.copilot_actions.docs.append(doc)
    return doc["id"]


@pytest.mark.parametrize("kind,payload", [
    ("post_draft_entry", {"entry_id": "je-draft1"}),
    ("categorize_transaction", {"txn_id": "txn1", "category_code": "5400"}),
    ("reconcile_match", {"txn_id": "txn2", "entry_id": "je-posted1"}),
    ("send_reminder", {"invoice_id": "inv-abcdef123456", "stage": "nudge"}),
    ("chase_invoice", {"invoice_id": "inv-abcdef123456"}),
    ("pay_bill", {"bill_id": "b1", "amount": 10.0}),
])
@pytest.mark.parametrize("role_user", [ACCOUNTANT, BOOKKEEPER, BARBER])
def test_unauthorized_approval_rejected(monkeypatch, kind, payload, role_user):
    fdb = _fx(monkeypatch)
    aid = _pending_action(fdb, kind, payload)

    with pytest.raises(HTTPException) as exc:
        _run(COP.approve_action(aid, role_user))

    assert exc.value.status_code == 403
    # nothing executed: the action is still pending and untouched
    doc = fdb.copilot_actions.docs[0]
    assert doc["status"] == "pending"
    assert doc["decided_by"] is None


def test_owner_can_approve_ledger_kind_but_accountant_cannot(monkeypatch):
    # post_draft_entry needs ledger:approve — only the owner holds it
    fdb = _fx(monkeypatch)
    _seed_draft(fdb)
    aid = _pending_action(fdb, "post_draft_entry", {"entry_id": "je-draft1"})

    with pytest.raises(HTTPException) as exc:
        _run(COP.approve_action(aid, ACCOUNTANT))
    assert exc.value.status_code == 403

    done = _run(COP.approve_action(aid, OWNER))
    assert done["status"] == "executed"
    entry = [e for e in fdb.journal_entries.docs if e["id"] == "je-draft1"][0]
    assert entry["status"] == "posted"


def test_propose_open_to_any_role(monkeypatch):
    fdb = _fx(monkeypatch)
    _seed_reminder(fdb)
    # an accountant cannot approve, but can propose (same as chatting)
    proposed = _run(COP.propose_actions(ACCOUNTANT))
    assert any(d["kind"] == "send_reminder" for d in proposed)


def test_approve_twice_is_rejected(monkeypatch):
    fdb = _fx(monkeypatch)
    _seed_draft(fdb)
    aid = _pending_action(fdb, "post_draft_entry", {"entry_id": "je-draft1"})

    _run(COP.approve_action(aid, OWNER))
    with pytest.raises(HTTPException) as exc:
        _run(COP.approve_action(aid, OWNER))
    assert exc.value.status_code == 409


def test_approve_unknown_kind_rejected(monkeypatch):
    fdb = _fx(monkeypatch)
    aid = _pending_action(fdb, "teleport_money", {})
    with pytest.raises(HTTPException) as exc:
        _run(COP.approve_action(aid, OWNER))
    assert exc.value.status_code == 400


def test_proposals_do_not_duplicate(monkeypatch):
    fdb = _fx(monkeypatch)
    _seed_draft(fdb)
    first = _run(COP.propose_actions(OWNER))
    drafts = [d for d in first if d["kind"] == "post_draft_entry"]
    assert len(drafts) == 1
    second = _run(COP.propose_actions(OWNER))
    assert not [d for d in second if d["kind"] == "post_draft_entry"]


def test_system_prompt_describes_actions_and_approval():
    prompt = COP.CHAT_SYSTEM
    for kind in COP.ACTION_KINDS:
        assert kind in prompt, f"{kind} missing from system prompt"
    assert "ledger:approve" in prompt
    assert "ops:manage" in prompt
    assert "never invent" in prompt.lower()


def test_approval_permission_map_covers_all_kinds():
    assert set(COP.APPROVAL_PERMISSION) == set(COP.ACTION_KINDS)
    assert set(COP._EXECUTORS) == set(COP.ACTION_KINDS)
