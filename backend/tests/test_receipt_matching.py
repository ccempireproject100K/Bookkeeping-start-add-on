"""Receipt <-> bank-feed matching lane (owner-approved 2026-11-14, gap #8).

Direct-call tests: no mongod, no network. Endpoint functions are awaited
directly with a fake user dict and an in-memory FakeDB monkeypatched into
the `db` attribute of the modules under test. The ledger seam
(receipts.L.post_journal_entry) is faked so no journal is needed;
everything else — composite scoring, flagging, auto-linking, the
one-claim uniqueness guard, resolves — runs for real.
"""
import asyncio
import copy
import os
import sys
import uuid
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib import receiptmatch as RM
from routers import receipts, receipt_match
import lib.audit as audit_mod

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}

TODAY = date(2026, 11, 14)


def run(coro):
    return asyncio.run(coro)


# ============================ FakeDB ============================
_MISSING = object()


def _matches(doc, query):
    for key, cond in (query or {}).items():
        val = doc.get(key, _MISSING)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if op == "$in":
                    if val is _MISSING or val not in operand:
                        return False
                elif op == "$ne":
                    if val is not _MISSING and val == operand:
                        return False
                elif op == "$gte":
                    if val is _MISSING or not val >= operand:
                        return False
                elif op == "$lte":
                    if val is _MISSING or not val <= operand:
                        return False
                elif op == "$exists":
                    if (val is not _MISSING) != bool(operand):
                        return False
                else:
                    raise NotImplementedError(f"FakeDB: operator {op}")
        else:
            if val is _MISSING or val != cond:
                return False
    return True


def _apply_update(doc, update):
    for op, fields in (update or {}).items():
        if op == "$set":
            for k, v in fields.items():
                doc[k] = copy.deepcopy(v)
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = (doc.get(k) or 0) + v
        elif op == "$unset":
            for k in fields:
                doc.pop(k, None)
        elif op == "$push":
            for k, v in fields.items():
                doc.setdefault(k, []).append(copy.deepcopy(v))
        else:
            raise NotImplementedError(f"FakeDB: update operator {op}")


def _project(doc, projection):
    if not projection:
        return copy.deepcopy(doc)
    exc = [k for k, v in projection.items() if not v]
    out = copy.deepcopy(doc)
    for k in exc:
        out.pop(k, None)
    return out


def _sort_key(v):
    return (v is None, v if isinstance(v, (str, int, float)) else str(v))


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        items = [spec] if isinstance(spec, tuple) else list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: _sort_key(d.get(field)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return _project(d, projection)
        return None

    def find(self, query=None, projection=None):
        return FakeCursor([_project(d, projection) for d in self.docs
                           if _matches(d, query)])

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", uuid.uuid4().hex)
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)
        return SimpleNamespace(inserted_ids=[])

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return SimpleNamespace(matched_count=0, modified_count=0)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, query, update):
        n = 0
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                n += 1
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class FakeDB:
    def __init__(self):
        object.__setattr__(self, "_colls", {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        colls = object.__getattribute__(self, "_colls")
        if name not in colls:
            colls[name] = FakeCollection(name)
        return colls[name]


# ============================ fixtures ============================
@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    fake.tenants.docs.append({"id": TID, "settings": {}})
    fake.tenants.docs.append({"id": TID2, "settings": {}})
    monkeypatch.setattr(receipts, "db", fake)
    monkeypatch.setattr(receipt_match, "db", fake)
    monkeypatch.setattr(audit_mod, "db", fake)

    async def fake_post_entry(db_, tenant_id, user, payload, status="posted"):
        return {"number": "JE-2026-000001", "id": "entry-fake"}

    monkeypatch.setattr(receipts.L, "post_journal_entry", fake_post_entry)
    return fake


def add_receipt(env, vendor="Home Depot", rdate="2026-11-10", amount=52.31,
                user=USER, rid=None, status="scanned"):
    rid = rid or uuid.uuid4().hex[:12]
    env.receipts.docs.append({
        "id": rid, "tenant_id": user["tenant_id"], "status": status,
        "filename": "r.jpg", "stored": f"{rid}.jpg",
        "content_type": "image/jpeg",
        "extracted": {"vendor": vendor, "date": rdate, "amount": amount,
                      "tax": 0, "category_code": "5700",
                      "category_name": "", "description": ""},
        "expense_id": None, "created_at": "2026-11-10T10:00:00+00:00"})
    return rid


def add_txn(env, desc="HOME DEPOT 1234", cents=-5231, tdate="2026-11-10",
            user=USER, tid=None):
    tid = tid or uuid.uuid4().hex[:12]
    env.bank_transactions.docs.append({
        "id": tid, "tenant_id": user["tenant_id"],
        "bank_account_id": "acct-1", "txn_date": tdate,
        "description": desc, "amount_cents": cents, "status": "unmatched"})
    return tid


def receipt_doc(env, rid, user=USER):
    return run(receipts.receipts(user).find_one({"id": rid}))


def pending(env, user=USER):
    return run(receipt_match.list_queue(user))


def scan(env, user=USER):
    return run(receipt_match.scan(user))


# ============================ pure scoring ============================
def test_amount_tiers():
    assert RM.amount_tier(5231, -5231) == "exact"
    assert RM.amount_tier(5231, -5234) == "rounding"      # 3 cents
    assert RM.amount_tier(10000, -9950) == "near"         # 0.5%
    assert RM.amount_tier(10000, -8000) == "off"          # 20%


def test_vendor_similarity_uses_pattern_key():
    assert RM.vendor_similarity("Home Depot", "HOME DEPOT 1234 PURCHASE") == 1.0
    assert RM.vendor_similarity("Shell Oil", "SHELL OIL 57442 DALLAS") == 1.0
    assert RM.vendor_similarity("Home Depot", "BURGER PALACE 88") < 0.4
    assert RM.vendor_similarity("", "HOME DEPOT 1234") == 0.0


def test_vendor_similarity_fuzzy_typo():
    # a truncated/misspelled vendor on the bank line still scores similar
    assert RM.vendor_similarity("Home Depot", "HOME DEPOT #4521") >= 0.9
    assert RM.vendor_similarity("Grainger", "GRAINGR INDUSTRIAL 991") >= 0.6


def test_date_score_decays_with_distance():
    assert RM.date_score("2026-11-10", "2026-11-10") == 1.0
    assert RM.date_score("2026-11-10", "2026-11-14") == 0.72
    assert RM.date_score("2026-11-10", "2026-11-24") == 0.15
    assert RM.date_score("2026-11-10", "2026-11-25") == 0.0


def test_score_candidate_strong_shape():
    ex = {"vendor": "Home Depot", "date": "2026-11-10", "amount": 52.31}
    txn = {"id": "t1", "txn_date": "2026-11-10",
           "description": "HOME DEPOT 1234", "amount_cents": -5231}
    s = RM.score_candidate(ex, txn)
    assert s["score"] >= 0.95 and s["amount_tier"] == "exact"
    assert "same amount" in s["reason"] and "same day" in s["reason"]


# ============================ classify ============================
def _scored(ex, txns):
    return [RM.score_candidate(ex, t) for t in txns]


def test_classify_strong_unique():
    ex = {"vendor": "Home Depot", "date": "2026-11-10", "amount": 52.31}
    v = RM.classify(ex, _scored(ex, [
        {"id": "t1", "txn_date": "2026-11-10", "description": "HOME DEPOT 1",
         "amount_cents": -5231}]), today=TODAY)
    assert v["verdict"] == "strong" and v["best"]["txn_id"] == "t1"
    assert v["flags"] == []


def test_classify_ambiguous_two_equal_candidates():
    ex = {"vendor": "Home Depot", "date": "2026-11-10", "amount": 52.31}
    v = RM.classify(ex, _scored(ex, [
        {"id": "t1", "txn_date": "2026-11-10", "description": "HOME DEPOT 1",
         "amount_cents": -5231},
        {"id": "t2", "txn_date": "2026-11-10", "description": "HOME DEPOT 2",
         "amount_cents": -5231}]), today=TODAY)
    assert v["verdict"] == "weak"
    assert [f["type"] for f in v["flags"]] == ["ambiguous"]


def test_classify_amount_mismatch_flag():
    ex = {"vendor": "Home Depot", "date": "2026-11-10", "amount": 100.00}
    v = RM.classify(ex, _scored(ex, [
        {"id": "t1", "txn_date": "2026-11-10", "description": "HOME DEPOT 1",
         "amount_cents": -8000}]), today=TODAY)
    types = [f["type"] for f in v["flags"]]
    assert "amount_mismatch" in types
    assert "different amount" in v["flags"][0]["reason"]


def test_classify_date_outside_window_flag():
    ex = {"vendor": "Home Depot", "date": "2026-11-01", "amount": 50.00}
    v = RM.classify(ex, _scored(ex, [
        {"id": "t1", "txn_date": "2026-11-13", "description": "HOME DEPOT 1",
         "amount_cents": -5000}]), today=TODAY)
    types = [f["type"] for f in v["flags"]]
    assert "date_outside_window" in types


def test_classify_no_twin_only_when_receipt_is_old():
    ex = {"vendor": "Home Depot", "date": "2026-10-01", "amount": 50.00}
    v = RM.classify(ex, [], today=TODAY)
    assert v["verdict"] == "none"
    assert [f["type"] for f in v["flags"]] == ["no_twin"]
    young = {"vendor": "Home Depot", "date": "2026-11-12", "amount": 50.00}
    v2 = RM.classify(young, [], today=TODAY)
    assert v2["verdict"] == "none" and v2["flags"] == []


def test_classify_near_amount_is_weak_not_strong():
    # within 1% but not exact: plausible, never auto-linked
    ex = {"vendor": "Home Depot", "date": "2026-11-10", "amount": 100.00}
    v = RM.classify(ex, _scored(ex, [
        {"id": "t1", "txn_date": "2026-11-10", "description": "HOME DEPOT 1",
         "amount_cents": -9950}]), today=TODAY)
    assert v["verdict"] == "weak"
    assert [f["type"] for f in v["flags"]] == ["weak_match"]


# ============================ scan: link + flag ============================
def test_scan_auto_links_strong_match(env):
    rid = add_receipt(env)
    tid = add_txn(env)
    r = scan(env)
    assert r["auto_linked"] == 1 and r["flags_open"] == 0
    doc = receipt_doc(env, rid)
    assert doc["match_link"]["txn_id"] == tid
    assert doc["match_link"]["source"] == "auto"
    assert doc["match_link"]["score"] >= RM.STRONG_SCORE
    s = run(receipt_match.stats(USER))
    assert s["auto_linked"] == 1 and s["pending"] == 0


def test_scan_flags_weak_match(env):
    rid = add_receipt(env, amount=100.00)
    add_txn(env, cents=-9950)
    r = scan(env)
    assert r["auto_linked"] == 0 and r["flags_open"] == 1
    items = pending(env)
    assert len(items) == 1 and items[0]["receipt_id"] == rid
    assert items[0]["flag_type"] == "weak_match"
    assert items[0]["best_txn_id"]
    assert items[0]["candidates"][0]["score"] >= RM.WEAK_SCORE
    assert receipt_doc(env, rid).get("match_link") is None


def test_scan_flags_no_twin_for_old_receipt(env):
    rid = add_receipt(env, rdate="2026-10-01")
    r = scan(env)
    assert r["flags_open"] == 1
    items = pending(env)
    assert items[0]["flag_type"] == "no_twin"
    assert "should have shown it" in items[0]["reason"]


def test_scan_rerun_does_not_stack_duplicate_flags(env):
    add_receipt(env, amount=100.00)
    add_txn(env, cents=-9950)
    scan(env)
    scan(env)
    assert len(pending(env)) == 1


def test_scan_skips_receipt_without_extraction(env):
    env.receipts.docs.append({
        "id": "raw-1", "tenant_id": TID, "status": "scanned",
        "extracted": None, "created_at": "2026-11-10T10:00:00+00:00"})
    r = scan(env)
    assert r["receipts_checked"] == 0


# ============================ resolve paths ============================
def _flagged_weak(env):
    rid = add_receipt(env, amount=100.00)
    tid = add_txn(env, cents=-9950)
    scan(env)
    return rid, tid, pending(env)[0]


def test_resolve_confirm_link(env):
    rid, tid, item = _flagged_weak(env)
    r = run(receipt_match.resolve(
        item["id"], receipt_match.ResolveBody(action="confirm_link"), USER))
    assert r["status"] == "resolved" and r["txn_id"] == tid
    doc = receipt_doc(env, rid)
    assert doc["match_link"]["txn_id"] == tid
    assert doc["match_link"]["source"] == "review"
    assert pending(env) == []
    saved = [d for d in env.receipt_match_queue.docs if d["id"] == item["id"]][0]
    assert saved["status"] == "resolved"
    assert saved["resolution_action"] == "confirm_link"
    assert saved["resolution_txn_id"] == tid


def test_resolve_pick_txn_links_the_picked_one(env):
    rid = add_receipt(env, rdate="2026-10-01")   # no twin -> no_twin flag
    scan(env)
    item = pending(env)[0]
    assert item["flag_type"] == "no_twin"
    tid = add_txn(env, cents=-5000, tdate="2026-10-02",
                  desc="HOME DEPOT 77")
    # receipt amount is 52.31; adjust the txn to the receipt's real twin
    env.bank_transactions.docs[-1]["amount_cents"] = -5231
    r = run(receipt_match.resolve(
        item["id"], receipt_match.ResolveBody(action="pick_txn",
                                              txn_id=tid), USER))
    assert r["txn_id"] == tid
    assert receipt_doc(env, rid)["match_link"]["txn_id"] == tid


def test_resolve_no_match_stands_alone_and_stays_quiet(env):
    rid, tid, item = _flagged_weak(env)
    r = run(receipt_match.resolve(
        item["id"], receipt_match.ResolveBody(action="no_match"), USER))
    assert r["status"] == "resolved" and r["txn_id"] is None
    doc = receipt_doc(env, rid)
    assert doc["match_reviewed"]["action"] == "no_match"
    assert doc.get("match_link") is None
    scan(env)  # re-scan must not re-flag a decided receipt
    assert pending(env) == []


def test_resolve_unknown_action_and_double_resolve(env):
    rid, tid, item = _flagged_weak(env)
    with pytest.raises(HTTPException) as ei:
        run(receipt_match.resolve(
            item["id"], receipt_match.ResolveBody(action="explode"), USER))
    assert ei.value.status_code == 422
    run(receipt_match.resolve(
        item["id"], receipt_match.ResolveBody(action="no_match"), USER))
    with pytest.raises(HTTPException) as ei:
        run(receipt_match.resolve(
            item["id"], receipt_match.ResolveBody(action="no_match"), USER))
    assert ei.value.status_code == 409


def test_resolve_missing_item_404(env):
    with pytest.raises(HTTPException) as ei:
        run(receipt_match.resolve(
            "nope", receipt_match.ResolveBody(action="no_match"), USER))
    assert ei.value.status_code == 404


# ============================ uniqueness guard (FIX-IT-FIRST) ============================
def _post(env, rid, txn_id=None, amount=52.31):
    return run(receipts.post_receipt(rid, receipts.PostBody(
        vendor="Home Depot", category_code="5700", amount=amount,
        expense_date="2026-11-10", bank_transaction_id=txn_id), USER))


def test_post_twice_against_same_bank_txn_is_refused(env):
    tid = add_txn(env)
    rid_a = add_receipt(env, rid="rcpt-a")
    rid_b = add_receipt(env, rid="rcpt-b")
    out = _post(env, rid_a, txn_id=tid)
    assert out["bank_transaction_id"] == tid
    with pytest.raises(HTTPException) as ei:
        _post(env, rid_b, txn_id=tid)
    assert ei.value.status_code == 409
    assert "already linked" in ei.value.detail
    # receipt B is untouched — still scanned, postable another way
    assert receipt_doc(env, rid_b)["status"] == "scanned"
    out_b = _post(env, rid_b, txn_id=None)
    assert out_b["bank_transaction_id"] is None


def test_post_honors_the_matchers_link(env):
    rid = add_receipt(env)
    tid = add_txn(env)
    scan(env)  # auto-links
    out = _post(env, rid, txn_id=None)
    assert out["bank_transaction_id"] == tid
    assert receipt_doc(env, rid)["matched_bank_txn"] == tid


def test_resolve_cannot_steal_a_claimed_txn(env):
    tid = add_txn(env)
    rid_a = add_receipt(env, rid="rcpt-a")
    _post(env, rid_a, txn_id=tid)
    rid_b, _, item = _flagged_weak(env)
    with pytest.raises(HTTPException) as ei:
        run(receipt_match.resolve(
            item["id"], receipt_match.ResolveBody(
                action="pick_txn", txn_id=tid), USER))
    assert ei.value.status_code == 409
    assert receipt_doc(env, rid_b).get("match_link") is None
    assert len(pending(env)) == 1  # flag stays open for a real decision


def test_claimed_txn_not_offered_to_second_receipt(env):
    tid = add_txn(env)
    rid_a = add_receipt(env, rid="rcpt-a")
    _post(env, rid_a, txn_id=tid)
    rid_b = add_receipt(env, rid="rcpt-b")
    matches = run(receipts.receipt_matches(rid_b, USER))
    assert all(m["id"] != tid for m in matches)


# ============================ matches endpoint ============================
def test_matches_endpoint_returns_scored_candidates(env):
    rid = add_receipt(env)
    add_txn(env)
    add_txn(env, desc="COFFEE SHOP 9", cents=-600, tdate="2026-11-11")
    matches = run(receipts.receipt_matches(rid, USER))
    assert matches and matches[0]["description"] == "HOME DEPOT 1234"
    assert matches[0]["score"] >= 0.95
    assert "same amount" in matches[0]["reason"]
    assert matches[0]["amount_tier"] == "exact"


# ============================ tenant isolation ============================
def test_other_tenants_bank_lines_are_never_candidates(env):
    rid = add_receipt(env, rdate="2026-10-01")
    add_txn(env, user=USER2)  # identical twin, wrong tenant
    r = scan(env)
    assert r["auto_linked"] == 0
    items = pending(env)
    assert len(items) == 1 and items[0]["flag_type"] == "no_twin"
    assert pending(env, user=USER2) == []
    doc = receipt_doc(env, rid)
    assert doc.get("match_link") is None


def test_other_tenant_cannot_resolve_or_see_flags(env):
    _flagged_weak(env)
    assert pending(env, user=USER2) == []
    item = pending(env)[0]
    with pytest.raises(HTTPException) as ei:
        run(receipt_match.resolve(
            item["id"], receipt_match.ResolveBody(action="no_match"),
            USER2))
    assert ei.value.status_code == 404
