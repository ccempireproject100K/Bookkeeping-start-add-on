"""Categorization confidence lane (owner-approved 2026-11-14, gap #7).

Direct-call tests: no mongod, no network. Endpoint/engine functions are
awaited directly with a fake user dict and an in-memory FakeDB monkeypatched
into the `db` attribute of the modules under test. The one posting seam
(cat_review._post_txn) is faked so no ledger is needed; everything else —
scoring, queueing, remembering, the learning OFF switch — runs for real.
"""
import asyncio
import copy
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib import catconfidence as CC
from routers import banking, cat_review

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}

ACCT = "acct-1"


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
    fake.bank_accounts.docs.append(
        {"id": ACCT, "tenant_id": TID, "name": "Checking", "kind": "bank",
         "gl_code": "1020"})
    monkeypatch.setattr(banking, "db", fake)
    monkeypatch.setattr(cat_review, "db", fake)

    async def fake_post(user, txn_id, category_code):
        t = await banking.txns(user).find_one({"id": txn_id})
        assert t, "posting a missing txn"
        await banking.txns(user).update_one({"id": txn_id}, {"$set": {
            "status": "cleared", "category_code": category_code,
            "entry_id": "entry-x", "entry_number": 101}})
        return {"status": "cleared", "entry_number": 101}

    monkeypatch.setattr(cat_review, "_post_txn", fake_post)
    return fake


def add_rule(env, contains, code, user=USER):
    return run(banking.create_rule(
        banking.RuleBody(contains=contains, category_code=code), user))


def insert(env, desc, amount=-50.0, user=USER, date="2026-11-10"):
    return run(banking._insert_txn(user, ACCT, date, desc, amount, "test"))


def pending(env):
    return run(cat_review.list_queue(USER))


# ============================ pure scoring ============================
def rule(contains, code):
    return {"contains": contains, "category_code": code}


def test_pattern_key_ignores_store_numbers():
    assert CC.pattern_key("SHELL OIL 57442 DALLAS TX") == \
        CC.pattern_key("shell oil 99813")
    assert CC.pattern_key("HOME DEPOT #1234") == "home depot"


def test_clean_single_rule_is_confident():
    a = CC.evaluate("HOME DEPOT 1234", [rule("home depot", "5000")], {})
    assert a["confident"] and a["category_code"] == "5000"
    assert a["source"] == "rule" and a["reason"] is None


def test_no_rule_is_unsure_with_plain_reason():
    a = CC.evaluate("MYSTERY VENDOR LLC", [rule("home depot", "5000")], {})
    assert not a["confident"] and a["guess_code"] is None
    assert "never seen" in a["reason"].lower()


def test_conflicting_rules_are_unsure_guess_is_first():
    rules = [rule("shell", "5700"), rule("shell oil", "5400")]
    a = CC.evaluate("SHELL OIL 57442", rules, {})
    assert not a["confident"] and a["guess_code"] == "5700"
    assert "different categories" in a["reason"]


def test_two_rules_same_category_stay_confident():
    rules = [rule("shell", "5700"), rule("shell oil", "5700")]
    a = CC.evaluate("SHELL OIL 57442", rules, {})
    assert a["confident"] and a["category_code"] == "5700"


def test_fuzzy_midword_match_is_unsure():
    a = CC.evaluate("CATERING BY DESIGN", [rule("cat", "5700")], {})
    assert not a["confident"] and a["guess_code"] == "5700"
    assert "loose" in a["reason"]


def test_fuzzy_short_needle_is_unsure():
    a = CC.evaluate("TAX PREP PROS", [rule("ax", "5700")], {})
    assert not a["confident"]


def test_learned_pattern_wins_over_no_rules():
    learned = {CC.pattern_key("ACME WIDGETS 88"): {"category_code": "5000"}}
    a = CC.evaluate("ACME WIDGETS 91", [], learned)
    assert a["confident"] and a["source"] == "learned"
    assert a["category_code"] == "5000"


def test_learned_ignored_when_learning_disabled():
    learned = {CC.pattern_key("ACME WIDGETS 88"): {"category_code": "5000"}}
    a = CC.evaluate("ACME WIDGETS 91", [], learned, learning_enabled=False)
    assert not a["confident"]


# ============================ queueing at insert ============================
def test_confident_txn_is_not_queued(env):
    add_rule(env, "home depot", "5000")
    doc = insert(env, "HOME DEPOT 1234")
    assert doc["cat_confidence"] == "confident"
    assert doc["needs_review"] is False
    assert doc["category_code"] == "5000"
    assert pending(env) == []


def test_unknown_vendor_is_queued_not_silently_suggested(env):
    doc = insert(env, "MYSTERY VENDOR LLC")
    assert doc["needs_review"] is True
    assert doc["category_code"] is None
    items = pending(env)
    assert len(items) == 1
    assert items[0]["txn_id"] == doc["id"]
    assert items[0]["guess_code"] is None
    assert "never seen" in items[0]["reason"].lower()


def test_conflict_is_queued_with_guess(env):
    add_rule(env, "shell", "5700")
    add_rule(env, "shell oil", "5400")
    doc = insert(env, "SHELL OIL 57442")
    items = pending(env)
    assert len(items) == 1 and items[0]["guess_code"] == "5700"
    assert doc["suggested_code"] == "5700" and doc["category_code"] is None


# ============================ resolve + remember ============================
def test_resolve_remember_then_same_vendor_not_flagged_again(env):
    doc = insert(env, "ACME WIDGETS 88")
    item = pending(env)[0]
    r = run(cat_review.resolve(
        item["id"], cat_review.ResolveBody(category_code="5000",
                                           remember=True), USER))
    assert r["status"] == "resolved" and r["learned"] is True
    assert pending(env) == []
    doc2 = insert(env, "ACME WIDGETS 91", date="2026-11-12")
    assert doc2["cat_confidence"] == "confident"
    assert doc2["cat_source"] == "learned"
    assert doc2["category_code"] == "5000"
    assert pending(env) == []  # flags trend down: nothing new queued


def test_resolve_without_remember_stores_nothing(env):
    insert(env, "ACME WIDGETS 88")
    item = pending(env)[0]
    r = run(cat_review.resolve(
        item["id"], cat_review.ResolveBody(category_code="5000",
                                           remember=False), USER))
    assert r["learned"] is False
    assert run(cat_review.list_patterns(USER)) == []
    insert(env, "ACME WIDGETS 92", date="2026-11-12")
    assert len(pending(env)) == 1  # flagged again — nothing was learned


def test_learning_off_stores_nothing_even_when_remember_asked(env):
    run(cat_review.patch_settings(
        cat_review.SettingsBody(learning_enabled=False), USER))
    insert(env, "ACME WIDGETS 88")
    item = pending(env)[0]
    r = run(cat_review.resolve(
        item["id"], cat_review.ResolveBody(category_code="5000",
                                           remember=True), USER))
    assert r["learned"] is False
    assert env.cat_learned_patterns.docs == []  # the OFF proof: zero stored
    s = run(cat_review.stats(USER))
    assert s["learned_count"] == 0
    # and with learning off, the same vendor is flag-and-fix again
    insert(env, "ACME WIDGETS 93", date="2026-11-12")
    assert len(pending(env)) == 1


def test_learning_off_ignores_previously_stored_patterns(env):
    # learn one while ON
    insert(env, "ACME WIDGETS 88")
    item = pending(env)[0]
    run(cat_review.resolve(
        item["id"], cat_review.ResolveBody(category_code="5000",
                                           remember=True), USER))
    assert len(env.cat_learned_patterns.docs) == 1
    # turn OFF: the stored pattern must no longer auto-apply
    run(cat_review.patch_settings(
        cat_review.SettingsBody(learning_enabled=False), USER))
    doc = insert(env, "ACME WIDGETS 94", date="2026-11-12")
    assert doc["needs_review"] is True


def test_patterns_are_tenant_scoped(env):
    insert(env, "ACME WIDGETS 88")
    item = pending(env)[0]
    run(cat_review.resolve(
        item["id"], cat_review.ResolveBody(category_code="5000",
                                           remember=True), USER))
    doc = insert(env, "ACME WIDGETS 95", user=USER2, date="2026-11-12")
    assert doc["needs_review"] is True  # other tenant learned nothing


def test_resolve_unknown_category_rejected(env):
    insert(env, "ACME WIDGETS 88")
    item = pending(env)[0]
    with pytest.raises(HTTPException) as ei:
        run(cat_review.resolve(
            item["id"], cat_review.ResolveBody(category_code="9999"), USER))
    assert ei.value.status_code == 422
    assert len(pending(env)) == 1  # still waiting


def test_resolve_already_posted_txn_is_graceful(env):
    doc = insert(env, "ACME WIDGETS 88")
    # user posted it through the old Categorize dialog first
    run(banking.txns(USER).update_one({"id": doc["id"]}, {"$set": {
        "status": "cleared", "category_code": "5700", "entry_number": 77}}))
    item = pending(env)[0]
    r = run(cat_review.resolve(
        item["id"], cat_review.ResolveBody(category_code="5700"), USER))
    assert r["status"] == "resolved" and r["entry_number"] == 77
    assert pending(env) == []


def test_stats_counters(env):
    add_rule(env, "home depot", "5000")
    insert(env, "HOME DEPOT 1")            # confident — never counted
    insert(env, "ACME WIDGETS 88")         # flagged
    insert(env, "MYSTERY VENDOR LLC")      # flagged
    s = run(cat_review.stats(USER))
    assert s["pending"] == 2 and s["flagged_this_week"] == 2
    assert s["learned_count"] == 0 and s["resolved_total"] == 0
    item = pending(env)[0]
    run(cat_review.resolve(
        item["id"], cat_review.ResolveBody(category_code="5000",
                                           remember=True), USER))
    s = run(cat_review.stats(USER))
    assert s["pending"] == 1 and s["resolved_total"] == 1
    assert s["learned_count"] == 1


def test_delete_pattern_forgets(env):
    insert(env, "ACME WIDGETS 88")
    item = pending(env)[0]
    run(cat_review.resolve(
        item["id"], cat_review.ResolveBody(category_code="5000",
                                           remember=True), USER))
    pats = run(cat_review.list_patterns(USER))
    assert len(pats) == 1
    run(cat_review.delete_pattern(pats[0]["id"], USER))
    doc = insert(env, "ACME WIDGETS 96", date="2026-11-12")
    assert doc["needs_review"] is True
