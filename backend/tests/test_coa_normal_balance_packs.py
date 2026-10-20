"""Regression: pack revenue/expense codes must resolve a normal balance.

lib/coa.py originally built NORMAL_BALANCE_OF from BASE_COA only, while
ACCOUNT_TYPE_OF covers every niche pack. Pack revenue codes (4000-series)
were therefore absent from the sign map, lib/ledger.py's _net_balance
defaulted them to "debit", and every pack revenue balance came back
NEGATED — negative revenue on the P&L and a balance sheet that would not
balance for any tenant with pack-revenue postings (all 33 packs, since
the pack system landed). Postings were always correct; only the report
sign was wrong. These tests pin the map and the end-to-end report math.

Fully mocked (no live MongoDB), in the style of test_currency_phase4.py.

Run from the backend directory with: python -m pytest tests/test_coa_normal_balance_packs.py
"""
import asyncio
from types import SimpleNamespace

import lib.ledger as L
from lib import niches
from lib.base import Scoped
from lib.coa import ACCOUNT_TYPE_OF, BASE_COA, NORMAL_BALANCE_OF, seed_coa

TID = "tenant-hvac"


def run(coro):
    # Dedicated loop, NOT asyncio.get_event_loop() — same fix as
    # test_currency_phase4.py (suite order must not matter).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------- fakes

def _matches(doc, filt):
    for k, v in (filt or {}).items():
        if isinstance(v, dict):
            for op, val in v.items():
                if op == "$lte" and not (doc.get(k) is not None and doc.get(k) <= val):
                    return False
                if op == "$gte" and not (doc.get(k) is not None and doc.get(k) >= val):
                    return False
                if op == "$ne" and doc.get(k) == val:
                    return False
        elif doc.get(k) != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec):
        items = spec.items() if isinstance(spec, dict) else spec
        for key, direction in reversed(list(items)):
            self.docs.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=0):
        return self.docs[:length] if length else self.docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    def find(self, filt=None, proj=None):
        return FakeCursor([d for d in self.docs if _matches(d, filt)])

    async def find_one(self, filt=None, proj=None):
        rows = [d for d in self.docs if _matches(d, filt)]
        return rows[0] if rows else None

    async def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", f"fake-{len(self.docs) + 1}")
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    async def update_one(self, filt, update):
        for d in self.docs:
            if _matches(d, filt):
                d.update(update.get("$set", {}))
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def find_one_and_update(self, filt, update, upsert=False,
                                  return_document=False):
        for d in self.docs:
            if _matches(d, filt):
                for k, inc in update.get("$inc", {}).items():
                    d[k] = d.get(k, 0) + inc
                return d
        if upsert:
            doc = dict(filt)
            for k, inc in update.get("$inc", {}).items():
                doc[k] = inc
            self.docs.append(doc)
            return doc
        return None

    def aggregate(self, pipeline):
        docs = list(self.docs)
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                key_field = spec["_id"].lstrip("$")
                groups = {}
                for d in docs:
                    g = groups.setdefault(d.get(key_field), {"_id": d.get(key_field)})
                    for out, agg in spec.items():
                        if out == "_id":
                            continue
                        src = agg["$sum"].lstrip("$")
                        g[out] = g.get(out, 0) + (d.get(src) or 0)
                docs = list(groups.values())
        return FakeCursor(docs)


def make_db():
    return SimpleNamespace(
        accounts=FakeCollection(),
        journal_entries=FakeCollection(),
        journal_lines=FakeCollection(),
        fiscal_periods=FakeCollection(),
        audit_log=FakeCollection(),
        counters=FakeCollection(),
    )


# ------------------------------------------- the sign map itself

def test_pack_revenue_and_expense_codes_resolve_normal_balance():
    """HVAC + plumbing + electrical: every pack revenue code is
    credit-normal and every trade-expense code debit-normal, through
    the same NORMAL_BALANCE_OF map _net_balance reads."""
    for pack_id in ("hvac", "plumbing", "electrical"):
        pack = niches.get_pack(pack_id)
        assert pack["id"] == pack_id
        assert pack["revenue_accounts"], f"{pack_id} has no revenue accounts"
        for a in pack["revenue_accounts"]:
            assert ACCOUNT_TYPE_OF[a["code"]] == "revenue"
            assert NORMAL_BALANCE_OF[a["code"]] == "credit", (pack_id, a["code"])
        for a in pack["trade_expense_accounts"]:
            assert ACCOUNT_TYPE_OF[a["code"]] == "expense"
            assert NORMAL_BALANCE_OF[a["code"]] == "debit", (pack_id, a["code"])


def test_every_pack_code_resolves_a_normal_balance():
    """Full sweep: no code from any of the 33 packs may fall through
    to _net_balance's debit default."""
    packs = niches.all_packs()
    assert len(packs) == 33
    for p in packs:
        for a in p.get("revenue_accounts", []):
            assert NORMAL_BALANCE_OF.get(a["code"]) == "credit", (p["id"], a["code"])
        for a in p.get("trade_expense_accounts", []):
            assert NORMAL_BALANCE_OF.get(a["code"]) == "debit", (p["id"], a["code"])


def test_base_chart_stays_authoritative_on_collision():
    """Base entries are inserted first and win: every BASE_COA code
    keeps its declared normal balance, including the contra accounts
    whose flipped side a pack sweep must never overwrite."""
    for code, _name, _type, normal in BASE_COA:
        assert NORMAL_BALANCE_OF[code] == normal
    assert NORMAL_BALANCE_OF["4900"] == "debit"   # contra-revenue
    assert NORMAL_BALANCE_OF["1850"] == "credit"  # contra-asset
    assert NORMAL_BALANCE_OF["3020"] == "debit"   # contra-equity


# ------------------------------------------- end-to-end report math

def test_hvac_pack_revenue_posts_positive_on_pnl_and_balance_sheet():
    """Post a real balanced journal through lib.ledger (Dr cash /
    Cr HVAC 4020 Maintenance Agreements), then read it back through
    the P&L and balance sheet: revenue must be POSITIVE and the
    balance sheet must balance. On the buggy map both failed."""
    db = make_db()
    run(seed_coa(Scoped(db.accounts, TID), "hvac"))
    entry = run(L.post_journal_entry(db, TID, {"email": "owner@shop.test"}, {
        "entry_date": "2026-10-15",
        "memo": "Maintenance agreement — October",
        "lines": [
            {"account_code": "1020", "debit": 150000, "credit": 0},
            {"account_code": "4020", "debit": 0, "credit": 150000},
        ],
    }, status="posted"))
    assert entry["status"] == "posted"

    # The exact helper the report layer signs balances with.
    assert L._net_balance("4020", 0, 150000) == 150000

    report = run(L.pnl(db, TID, "2026-10-01", "2026-10-31"))
    rows = {r["code"]: r["amount"] for r in report["revenue"]}
    assert rows["4020"] == 150000
    assert report["revenue_total"] == 150000
    assert report["net_income"] == 150000

    bs = run(L.balance_sheet(db, TID, "2026-10-31"))
    assert bs["assets_total"] == 150000
    ni = [r for r in bs["equity"] if r["code"] == "NI"]
    assert ni and ni[0]["amount"] == 150000
    assert bs["balanced"] is True
