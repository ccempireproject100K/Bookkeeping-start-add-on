"""Audit-trail hash chain (security audit 2026-10-26, N-4 — ported
from Trade Circle's app/audit.py).

Run from the backend directory with:
    python -m pytest tests/test_audit_chain.py

Each new audit row is chained to the previous one (chain_seq +
prev_hash + hash over its canonical content), so editing or
deleting a chained row breaks verification at that point. Rows
written before the chain existed are pre-chain history: never
rewritten, counted separately by the verifier. The chain makes the
trail tamper-EVIDENT — tamper-resistance still needs the Atlas
restricted-user step (owner-side), exactly as in Trade Circle.

The fake below implements just the chain store surface (atomic $inc
head allocation, $exists queries); a fake WITHOUT the chain store
pins the graceful fallback — the row still lands, unchained, and
recording never raises.
"""
import asyncio
import os
from copy import deepcopy

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_audit_chain")

import pytest  # noqa: E402
from pymongo import ReturnDocument  # noqa: E402

from lib import audit as AU  # noqa: E402
from routers import portal as P  # noqa: E402


def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict) and "$exists" in v:
            if (k in doc) != bool(v["$exists"]):
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs = sorted(self._docs,
                            key=lambda d: d.get(key) or 0,
                            reverse=direction < 0)
        return self

    async def to_list(self, length=None):
        return deepcopy(self._docs[:length] if length else self._docs)


class _Result:
    def __init__(self, matched):
        self.matched_count = matched


class _ChainCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q or {}):
                return deepcopy(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([d for d in self.docs if _matches(d, q or {})])

    async def insert_one(self, doc):
        self.docs.append(deepcopy(doc))
        return _Result(1)

    async def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if _matches(d, q):
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                return _Result(1)
        return _Result(0)

    async def find_one_and_update(self, q, update, upsert=False,
                                  return_document=None):
        for d in self.docs:
            if _matches(d, q):
                for k, v in (update.get("$inc") or {}).items():
                    d[k] = d.get(k, 0) + v
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                return deepcopy(d)
        if upsert:
            doc = {k: v for k, v in (q or {}).items()
                   if not isinstance(v, dict)}
            for k, v in (update.get("$inc") or {}).items():
                doc[k] = doc.get(k, 0) + v
            self.docs.append(doc)
            return deepcopy(doc)
        return None

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q or {}))


class _ChainDB:
    def __init__(self):
        self.audit_log = _ChainCollection()
        self.audit_chain = _ChainCollection()


class _PlainDB:
    """No chain store at all — the minimal-double fallback case."""
    def __init__(self):
        self.audit_log = _ChainCollection()


def run(coro):
    return asyncio.run(coro)


def _doc(action="test.action", **kw):
    doc = {"id": f"row-{action}", "tenant_id": "t1",
           "actor_id": "u1", "actor_name": "Owner",
           "actor_role": "owner", "action": action,
           "entity": "invoice", "entity_id": "inv-1",
           "details": {"amount_cents": 100},
           "created_at": "2026-10-26T00:00:00+00:00"}
    doc.update(kw)
    return doc


def test_appends_form_a_verifiable_chain(monkeypatch):
    fdb = _ChainDB()
    monkeypatch.setattr(AU, "db", fdb)

    async def go():
        for action in ("a.one", "a.two", "a.three"):
            await AU.audit({"id": "u1", "tenant_id": "t1",
                            "name": "Owner", "email": "o@x"},
                           action, "invoice", "inv-1", {"n": 1})
        return await AU.verify_audit_chain(fdb)
    result = run(go())
    rows = fdb.audit_log.docs
    assert [r["chain_seq"] for r in rows] == [1, 2, 3]
    assert rows[0]["prev_hash"] == AU.GENESIS_PREV_HASH
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    assert rows[2]["prev_hash"] == rows[1]["hash"]
    assert result == {"ok": True, "checked": 3, "break": None,
                      "unchained": 0}


def test_pre_chain_rows_stay_readable_and_counted(monkeypatch):
    fdb = _ChainDB()
    monkeypatch.setattr(AU, "db", fdb)

    async def go():
        await fdb.audit_log.insert_one(_doc("old.one"))
        await fdb.audit_log.insert_one(_doc("old.two"))
        await AU.append_audit(fdb, _doc("new.one"))
        return await AU.verify_audit_chain(fdb)
    result = run(go())
    assert result["ok"] is True and result["checked"] == 1
    assert result["unchained"] == 2
    chained = [r for r in fdb.audit_log.docs if "chain_seq" in r]
    assert chained[0]["prev_hash"] == AU.GENESIS_PREV_HASH
    # The pre-chain rows were never rewritten to join the chain.
    assert all("chain_seq" not in r for r in fdb.audit_log.docs[:2])


def test_tampered_row_is_detected(monkeypatch):
    fdb = _ChainDB()
    monkeypatch.setattr(AU, "db", fdb)

    async def go():
        for i in range(3):
            await AU.append_audit(fdb, _doc(f"t.{i}"))
        # Someone edits history: the amount on the second row.
        fdb.audit_log.docs[1]["details"] = {"amount_cents": 999999}
        return await AU.verify_audit_chain(fdb)
    result = run(go())
    assert result["ok"] is False
    assert result["break"]["seq"] == 2
    assert "does not recompute" in result["break"]["reason"]


def test_deleted_row_is_detected(monkeypatch):
    fdb = _ChainDB()
    monkeypatch.setattr(AU, "db", fdb)

    async def go():
        for i in range(3):
            await AU.append_audit(fdb, _doc(f"d.{i}"))
        del fdb.audit_log.docs[1]
        return await AU.verify_audit_chain(fdb)
    result = run(go())
    assert result["ok"] is False
    assert result["break"] is not None


def test_ledger_shaped_rows_chain_and_verify(monkeypatch):
    """lib/ledger.py writes a different shape (detail/at, actor =
    email). The canonical projection must hash it consistently."""
    fdb = _ChainDB()
    monkeypatch.setattr(AU, "db", fdb)

    async def go():
        await AU.append_audit(fdb, {
            "id": "led-1", "tenant_id": "t1",
            "actor": "owner@example.com", "action": "journal.posted",
            "detail": {"number": "JE-1"}, "at": "2026-10-26T00:00:00"})
        await AU.append_audit(fdb, _doc("after.ledger"))
        return await AU.verify_audit_chain(fdb)
    result = run(go())
    assert result["ok"] is True and result["checked"] == 2


def test_writer_falls_back_when_chain_store_missing(monkeypatch):
    fdb = _PlainDB()  # no audit_chain attribute at all
    monkeypatch.setattr(AU, "db", fdb)

    async def go():
        out = await AU.audit({"id": "u1", "tenant_id": "t1",
                              "name": "Owner"}, "fallback.action")
        return out
    out = run(go())
    assert out  # never raises, returns the row
    assert len(fdb.audit_log.docs) == 1
    assert "chain_seq" not in fdb.audit_log.docs[0]


def test_converted_router_helper_chains(monkeypatch):
    """End-to-end through a converted writer: portal._audit must land
    a chained row when the chain store is available."""
    fdb = _ChainDB()
    fdb.portal_links = _ChainCollection()
    monkeypatch.setattr(P, "db", fdb)

    async def go():
        user = {"id": "u1", "tenant_id": "t1", "name": "Owner",
                "role": "owner"}
        await P._audit(user, "portal.link.create", "invoice", "inv-1",
                       {})
        return await AU.verify_audit_chain(fdb)
    result = run(go())
    assert result["ok"] is True and result["checked"] == 1
