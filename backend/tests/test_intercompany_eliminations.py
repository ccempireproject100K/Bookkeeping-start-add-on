"""Top-20 #18 depth: automatic intercompany eliminations in the
consolidated multi-entity view (owner-approved 2026-11-14), with the
owner's condition pinned: eliminations are a TENANT-LEVEL SETTING,
DEFAULT OFF — off means today's flagged view, unchanged.

Direct-call tests: no mongod, no network. Router endpoints are awaited
directly with a fake user dict and an in-memory FakeDB monkeypatched
into the modules under test (harness shared with the sibling lanes,
extended with the aggregate pipeline lib.ledger._balances uses and
dotted-key $set for tenant settings).
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

from lib import ledger as L
from lib.base import Scoped
from lib.coa import seed_coa
from routers import entities as ent

A, B, C = "tenant-a", "tenant-b", "tenant-c"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": A,
        "memberships": [{"tenant_id": A, "role": "owner"},
                        {"tenant_id": B, "role": "owner"}]}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": C,
         "memberships": [{"tenant_id": C, "role": "owner"}]}
FROM, TO = "2026-01-01", "2026-12-31"


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
                else:
                    raise NotImplementedError(f"FakeDB: operator {op}")
        else:
            if val is _MISSING or val != cond:
                return False
    return True


def _set_dotted(doc, key, value):
    parts = key.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = copy.deepcopy(value)


def _apply_update(doc, update):
    for op, fields in (update or {}).items():
        if op == "$set":
            for k, v in fields.items():
                _set_dotted(doc, k, v)
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = (doc.get(k) or 0) + v
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


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        items = [spec] if isinstance(spec, tuple) else list(spec)
        for field, direction in reversed(items):
            self._docs.sort(key=lambda d: (d.get(field) is None, d.get(field)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    # Merge-train (2026-11-14): main's ledger balance scans iterate find
    # cursors with async-for; the fake cursor supports it like motor's.
    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()

    async def to_list(self, n=None):
        return list(self._docs)

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)


class FakeAggCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return copy.deepcopy(self._docs)


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
        ids = []
        for d in docs:
            res = await self.insert_one(d)
            ids.append(res.inserted_id)
        return SimpleNamespace(inserted_ids=ids)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
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

    async def find_one_and_update(self, query, update, upsert=False,
                                  return_document=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return copy.deepcopy(d)
        if upsert:
            new = {k: copy.deepcopy(v) for k, v in (query or {}).items()
                   if not isinstance(v, dict)}
            _apply_update(new, update)
            new.setdefault("_id", uuid.uuid4().hex)
            self.docs.append(new)
            return copy.deepcopy(new)
        return None

    def aggregate(self, pipeline):
        docs = list(self.docs)
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                field = spec["_id"].lstrip("$")
                groups = {}
                for d in docs:
                    g = groups.setdefault(d.get(field), {"_id": d.get(field)})
                    for out_key, agg in spec.items():
                        if out_key == "_id":
                            continue
                        src = agg["$sum"].lstrip("$")
                        g[out_key] = g.get(out_key, 0) + (d.get(src) or 0)
                docs = list(groups.values())
            else:
                raise NotImplementedError(f"FakeDB aggregate stage {stage}")
        return FakeAggCursor(docs)


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
    monkeypatch.setattr(ent, "db", fake)
    fake.tenants.docs.append({"id": A, "name": "Alpha HVAC", "niche_id": "hvac"})
    fake.tenants.docs.append({"id": B, "name": "Beta Plumbing", "niche_id": "plumbing"})
    fake.tenants.docs.append({"id": C, "name": "Gamma Stranger", "niche_id": "hvac"})
    return fake


_seq = [0]


def post(env, tid, date, lines, intercompany=False, counterparty=None,
         entry_flags=True):
    """Seed a posted entry + lines straight into the fake collections,
    in exactly the shape lib.ledger._write_lines writes."""
    _seq[0] += 1
    eid = f"entry-{_seq[0]}"
    number = f"JE-2026-{_seq[0]:06d}"
    env.journal_entries.docs.append({
        "id": eid, "tenant_id": tid, "number": number, "entry_date": date,
        "status": "posted", "source": "manual", "memo": None,
        "intercompany": intercompany if entry_flags else False,
        "counterparty_tenant_id": counterparty if entry_flags else None,
    })
    for code, debit, credit in lines:
        env.journal_lines.docs.append({
            "id": uuid.uuid4().hex, "tenant_id": tid, "entry_id": eid,
            "entry_number": number, "entry_date": date, "period_key": date[:7],
            "account_code": code, "debit": debit, "credit": credit,
            "memo": None, "status": "posted",
            "intercompany": intercompany, "counterparty_tenant_id": counterparty,
        })
    return eid


def matched_sale(env, a_amount=100_000, b_amount=100_000):
    """Alpha sells to Beta: A Dr AR / Cr Revenue; B Dr Expense / Cr AP."""
    post(env, A, "2026-03-01", [("1200", a_amount, 0), ("4090", 0, a_amount)],
         intercompany=True, counterparty=B)
    post(env, B, "2026-03-02", [("5700", b_amount, 0), ("2010", 0, b_amount)],
         intercompany=True, counterparty=A)


def external_activity(env):
    post(env, A, "2026-02-01", [("1020", 50_000, 0), ("4090", 0, 50_000)])
    post(env, B, "2026-02-03", [("5300", 20_000, 0), ("1020", 0, 20_000)])


def set_enabled(env, *tids):
    for t in env.tenants.docs:
        if t["id"] in tids:
            t.setdefault("settings", {})["eliminations_enabled"] = True


def consolidated(env, user=USER, view=None):
    return run(ent.consolidated(date_from=FROM, date_to=TO, view=view, user=user))


# ============ owner condition: default OFF = today's view ============
def test_default_off_is_todays_view(env):
    matched_sale(env)
    external_activity(env)
    out = consolidated(env)
    assert out["eliminations_enabled"] is False
    assert out["view"] == "gross"
    # Every legacy key, with exactly the legacy values.
    assert out["entity_count"] == 2
    assert out["date_from"] == FROM and out["date_to"] == TO
    assert out["total_revenue_cents"] == 150_000
    assert out["total_expense_cents"] == 120_000
    assert out["net_cents"] == 30_000
    assert out["combined_cash_cents"] == 30_000
    assert out["intercompany_review"] == []
    assert out["note"] == ("Intercompany balances are flagged, not "
                           "auto-eliminated — review before reporting.")
    legacy_lines = [("4090", 150_000), ("5300", 20_000), ("5700", 100_000)]
    assert [(l["code"], l["amount_cents"]) for l in out["lines"]] == legacy_lines
    # ...and yet the detail that WOULD eliminate is already visible.
    pairs = [d for d in out["eliminations_detail"] if d["eliminated_cents"]]
    assert sorted(d["amount_cents"] for d in pairs) == [100_000, 100_000]
    assert out["unmatched_intercompany"] == []


def test_no_setting_anywhere_means_off(env):
    out = run(ent.get_eliminations_setting(user=USER))
    assert out["enabled"] is False
    assert out["by_entity"] == {A: False, B: False}


# ============ matched pair eliminates to zero group effect ============
def test_matched_pair_eliminates_when_on(env):
    matched_sale(env)
    external_activity(env)
    set_enabled(env, A, B)
    out = consolidated(env)
    assert out["view"] == "net"
    assert out["gross"]["total_revenue_cents"] == 150_000
    assert out["gross"]["total_expense_cents"] == 120_000
    assert out["total_revenue_cents"] == 50_000     # only outside revenue
    assert out["total_expense_cents"] == 20_000     # only outside expense
    assert out["net_cents"] == 30_000
    assert out["eliminations_totals"]["revenue_cents"] == 100_000
    assert out["eliminations_totals"]["expense_cents"] == 100_000
    assert out["eliminations_totals"]["balance_cents"] == 100_000
    pairs = {(d["account_a_code"], d["account_b_code"]): d["amount_cents"]
             for d in out["eliminations_detail"] if d["eliminated_cents"]}
    assert pairs == {("1200", "2010"): 100_000, ("4090", "5700"): 100_000}
    bals = {r["code"]: r for r in out["balances"]}
    assert bals["1200"]["gross_cents"] == 100_000
    assert bals["1200"]["consolidated_cents"] == 0
    assert bals["2010"]["consolidated_cents"] == 0
    assert out["unmatched_intercompany"] == []


def test_individual_entity_books_unchanged_with_eliminations_on(env):
    matched_sale(env)
    external_activity(env)
    set_enabled(env, A, B)
    consolidated(env)
    pnl_a = run(L.pnl(env, A, FROM, TO))
    assert pnl_a["revenue_total"] == 150_000 and pnl_a["expense_total"] == 0
    pnl_b = run(L.pnl(env, B, FROM, TO))
    assert pnl_b["expense_total"] == 120_000
    stats = run(ent.list_entities(user=USER))
    by_id = {s["tenant_id"]: s for s in stats}
    assert by_id[A]["revenue_cents"] >= 150_000  # YTD-to-today includes it all
    assert by_id[B]["expense_cents"] >= 120_000


# ============ unmatched: listed, never force-matched, never dropped ====
def test_unmatched_when_other_side_missing(env):
    post(env, A, "2026-03-01", [("1200", 40_000, 0), ("4090", 0, 40_000)],
         intercompany=True, counterparty=B)
    set_enabled(env, A, B)
    out = consolidated(env)
    assert out["total_revenue_cents"] == 40_000  # nothing eliminated
    assert out["eliminations_totals"]["revenue_cents"] == 0
    unmatched = {(u["account_code"], u["amount_cents"]) for u in out["unmatched_intercompany"]}
    assert unmatched == {("1200", 40_000), ("4090", 40_000)}
    for u in out["unmatched_intercompany"]:
        assert "No matching intercompany entry was found in Beta Plumbing" in u["reason"]


def test_amount_mismatch_eliminates_min_remainder_unmatched(env):
    matched_sale(env, a_amount=100_000, b_amount=60_000)
    set_enabled(env, A, B)
    out = consolidated(env)
    assert out["eliminations_totals"]["revenue_cents"] == 60_000
    assert out["eliminations_totals"]["expense_cents"] == 60_000
    rem = [u for u in out["unmatched_intercompany"] if u["amount_cents"] == 40_000]
    assert {u["account_code"] for u in rem} == {"1200", "4090"}
    for u in rem:
        assert "recorded less on the other side" in u["reason"]


def test_legacy_account_flag_unmatched_with_reason(env):
    # A line on a 14xx account with no flag at all: the pre-existing
    # heuristic spots it, but with no sister named it cannot pair.
    post(env, A, "2026-03-01", [("1450", 25_000, 0), ("4090", 0, 25_000)])
    set_enabled(env, A, B)
    out = consolidated(env)
    legacy = [u for u in out["unmatched_intercompany"] if u["account_code"] == "1450"]
    assert len(legacy) == 1 and legacy[0]["source"] == "account_flag"
    assert "names no sister business" in legacy[0]["reason"]
    assert out["total_revenue_cents"] == 25_000


def test_entry_attribution_separates_matched_from_unmatched(env):
    """Two Alpha entries share one account: the $1,000 one pairs, the
    $400 one doesn't. The detail must name only the matched entry; the
    unmatched list only the other — never the whole group for both."""
    matched_sale(env)
    sale_number = env.journal_entries.docs[0]["number"]
    post(env, A, "2026-03-10", [("1200", 40_000, 0), ("4090", 0, 40_000)],
         intercompany=True, counterparty=B)
    orphan_number = env.journal_entries.docs[-1]["number"]
    set_enabled(env, A, B)
    out = consolidated(env)
    pnl_row = next(d for d in out["eliminations_detail"] if d["kind"] == "pnl")
    assert pnl_row["entries_a"] == [sale_number]
    assert orphan_number not in pnl_row["entries_a"]
    unmatched = [u for u in out["unmatched_intercompany"]
                 if u["account_code"] == "4090"]
    assert len(unmatched) == 1 and unmatched[0]["entries"] == [orphan_number]
    # Balance rows are balance-sheet accounts only (no P&L codes leak in).
    assert {r["code"] for r in out["balances"]} <= {
        "1020", "1200", "2010", "3010", "1850", "1300", "1310", "1500",
        "1800", "1010", "1021", "1022", "1023", "1050", "1060"}


# ============ gross vs net toggle math ================================
def test_gross_toggle_still_available_when_on(env):
    matched_sale(env)
    external_activity(env)
    set_enabled(env, A, B)
    gross = consolidated(env, view="gross")
    net = consolidated(env, view="net")
    assert gross["view"] == "gross"
    assert gross["total_revenue_cents"] == 150_000
    assert gross["total_expense_cents"] == 120_000
    assert gross["gross"] == net["gross"]
    line = next(l for l in gross["lines"] if l["code"] == "4090")
    assert line["amount_cents"] == 150_000  # legacy field untouched in gross
    nline = next(l for l in net["lines"] if l["code"] == "4090")
    assert (nline["gross_cents"], nline["eliminations_cents"],
            nline["consolidated_cents"]) == (150_000, 100_000, 50_000)


def test_net_view_refused_while_setting_off(env):
    matched_sale(env)
    out = consolidated(env, view="net")
    assert out["view"] == "gross"  # setting wins: no clean total until ON
    assert out["total_revenue_cents"] == 100_000


# ============ cash: same three-column treatment, honestly $0 ==========
def test_cash_transfer_never_inflates_combined_cash(env):
    post(env, A, "2026-01-05", [("1020", 100_000, 0), ("3010", 0, 100_000)])
    post(env, B, "2026-01-05", [("1020", 50_000, 0), ("3010", 0, 50_000)])
    # Alpha lends Beta $300: Dr Due-from(AR) / Cr Cash; Beta Dr Cash / Cr AP
    post(env, A, "2026-03-01", [("1200", 30_000, 0), ("1020", 0, 30_000)],
         intercompany=True, counterparty=B)
    post(env, B, "2026-03-01", [("1020", 30_000, 0), ("2010", 0, 30_000)],
         intercompany=True, counterparty=A)
    set_enabled(env, A, B)
    out = consolidated(env)
    assert out["combined_cash_cents"] == 150_000
    assert out["cash"]["gross_cents"] == 150_000
    assert out["cash"]["eliminations_cents"] == 0
    assert out["cash"]["consolidated_cents"] == 150_000
    assert out["eliminations_totals"]["cash_movement_cents"] == 60_000
    cash_rows = [d for d in out["eliminations_detail"] if d["kind"] == "cash_transfer"]
    assert len(cash_rows) == 2 and all(d["eliminated_cents"] == 0 for d in cash_rows)
    bals = {r["code"]: r for r in out["balances"]}
    assert bals["1200"]["consolidated_cents"] == 0
    assert bals["2010"]["consolidated_cents"] == 0


# ============ tenant isolation ========================================
def test_tenant_isolation_no_cross_owner_grouping(env):
    matched_sale(env)
    # Gamma (another owner's business) flags a deal with Alpha.
    post(env, C, "2026-03-01", [("1200", 77_000, 0), ("4090", 0, 77_000)],
         intercompany=True, counterparty=A)
    # Alpha flags a deal naming Gamma — outside USER's group.
    post(env, A, "2026-03-05", [("1200", 9_000, 0), ("4090", 0, 9_000)],
         intercompany=True, counterparty=C)
    set_enabled(env, A, B)
    out = consolidated(env)
    assert out["entity_count"] == 2
    assert out["gross"]["total_revenue_cents"] == 109_000  # Gamma's 77k absent
    outside = [u for u in out["unmatched_intercompany"]
               if u["counterparty_tenant_id"] == C]
    assert len(outside) == 2
    assert all("outside this owner's group" in u["reason"] for u in outside)
    out2 = consolidated(env, user=USER2)
    assert out2["entity_count"] == 1
    assert out2["total_revenue_cents"] == 77_000


def test_single_entity_user_view_unchanged(env):
    post(env, C, "2026-03-01", [("1020", 10_000, 0), ("4090", 0, 10_000)])
    out = consolidated(env, user=USER2)
    assert out["entity_count"] == 1
    assert out["total_revenue_cents"] == 10_000
    assert out["eliminations_detail"] == []
    assert out["unmatched_intercompany"] == []


# ============ setting endpoints =======================================
def test_setting_toggle_roundtrip_and_owner_gate(env):
    got = run(ent.get_eliminations_setting(user=USER))
    assert got["enabled"] is False
    put = run(ent.put_eliminations_setting(
        ent.EliminationsSettingBody(enabled=True), USER))
    assert put["enabled"] is True
    assert put["by_entity"] == {A: True, B: True}
    got = run(ent.get_eliminations_setting(user=USER))
    assert got["enabled"] is True
    tenants = {t["id"]: t for t in env.tenants.docs}
    assert tenants[A]["settings"]["eliminations_enabled"] is True
    assert tenants[B]["settings"]["eliminations_enabled"] is True
    assert "settings" not in tenants[C] or not tenants[C].get("settings")
    bookkeeper = {**USER, "role": "bookkeeper"}
    with pytest.raises(HTTPException) as ei:
        run(ent.put_eliminations_setting(
            ent.EliminationsSettingBody(enabled=False), bookkeeper))
    assert ei.value.status_code == 403


# ============ flag wiring through the real ledger =====================
def test_flags_wired_through_post_journal_entry(env):
    run(seed_coa(Scoped(env.accounts, A), "barbershop"))
    run(seed_coa(Scoped(env.accounts, B), "barbershop"))
    entry = run(L.post_journal_entry(env, A, USER, {
        "entry_date": "2026-03-01", "source": "manual",
        "intercompany": True, "counterparty_tenant_id": B,
        "lines": [{"account_code": "1200", "debit": 5000, "credit": 0},
                  {"account_code": "4000", "debit": 0, "credit": 5000}],
    }, status="posted"))
    assert entry["intercompany"] is True
    assert entry["counterparty_tenant_id"] == B
    lines = [l for l in env.journal_lines.docs if l["entry_id"] == entry["id"]]
    assert all(l["intercompany"] and l["counterparty_tenant_id"] == B for l in lines)
    # A line-level override beats the entry flag.
    entry2 = run(L.post_journal_entry(env, A, USER, {
        "entry_date": "2026-03-02", "source": "manual",
        "intercompany": True, "counterparty_tenant_id": B,
        "lines": [{"account_code": "1200", "debit": 700, "credit": 0,
                   "intercompany": False, "counterparty_tenant_id": None},
                  {"account_code": "4000", "debit": 0, "credit": 700}],
    }, status="posted"))
    lines2 = {l["account_code"]: l for l in env.journal_lines.docs
              if l["entry_id"] == entry2["id"]}
    assert lines2["1200"]["intercompany"] is False
    assert lines2["4000"]["intercompany"] is True
