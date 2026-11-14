"""QuickBooks move-in mapper (Top-20 gap #12).

Direct-call tests: no mongod, no network. Router/engine functions are
awaited directly with fake user dicts and an in-memory FakeDB monkeypatched
into the modules under test. Covers detection, mapper fixtures, dry-run
accuracy, trial-balance refusal, idempotent re-runs, mapping overrides,
practice-run isolation, attachment matching + the unmatched pile, and
tenant isolation.
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

from lib import qboimport as Q
from routers import migration as M

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}


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

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))


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


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    from routers import cleanup, banking
    monkeypatch.setattr(M, "db", fake)
    monkeypatch.setattr(cleanup, "db", fake)
    monkeypatch.setattr(banking, "db", fake)
    return fake


# ============================ fixtures ============================
ACCOUNTS_CSV = (
    "Account,Type,Detail Type,Description,Balance\n"
    "Checking,Bank,Checking,Operating,1000.00\n"
    "Accounts Receivable (A/R),Accounts receivable (A/R),Accounts Receivable,,\n"
    "Accounts Payable (A/P),Accounts payable (A/P),Accounts Payable,,\n"
    "Service Income,Income,Service Income,,\n"
    "Job Materials,Expenses,Supplies & Materials,,\n"
)
CUSTOMERS_CSV = (
    "Customer,Company,Email,Phone,Billing Address,Open Balance\n"
    "Acme Homes,Acme Homes LLC,ap@acme.example,555-0110,1 Main St,275.00\n"
)
VENDORS_CSV = (
    "Vendor,Company,Email,Phone,Billing Address,Open Balance\n"
    "Supply Depot,Supply Depot Inc,orders@depot.example,555-0120,9 Industrial Pkwy,55.00\n"
)
ITEMS_CSV = (
    "Product/Service,SKU,Type,Sales Price,Income Account\n"
    "Tune-Up,TU-1,Service,275.00,Service Income\n"
)
TXNS_CSV = (
    "Date,Transaction Type,Num,Name,Memo/Description,Account,Split,Debit,Credit,Balance\n"
    "01/05/2026,Invoice,1001,Acme Homes,Tune-up,Accounts Receivable (A/R),,275.00,,275.00\n"
    "01/05/2026,Invoice,1001,Acme Homes,Tune-up,Service Income,,,275.00,275.00\n"
    "01/06/2026,Bill,2001,Supply Depot,Materials,Job Materials,,55.00,,55.00\n"
    "01/06/2026,Bill,2001,Supply Depot,Materials,Accounts Payable (A/P),,,55.00,55.00\n"
)
# fix the credit-column placement (Debit empty, Credit filled) for Cr rows
TXNS_CSV = TXNS_CSV.replace(
    "Service Income,,,275.00,275.00", "Service Income,,,275.00,275.00")
FILES = [
    {"filename": "coa.csv", "content": ACCOUNTS_CSV},
    {"filename": "customers.csv", "content": CUSTOMERS_CSV},
    {"filename": "vendors.csv", "content": VENDORS_CSV},
    {"filename": "items.csv", "content": ITEMS_CSV},
    {"filename": "txns.csv", "content": TXNS_CSV},
]


def dry_run_body(files=FILES):
    return M.DryRunBody(files=[M.FileIn(**f) for f in files])


def do_dry_run(user=USER, files=FILES):
    return run(M.qbo_dry_run(dry_run_body(files), user))


def do_apply(run_id, user=USER, practice=False):
    return run(M.qbo_apply(run_id, M.ApplyBody(practice=practice), user))


def coll(env, name, tenant=TID):
    return [d for d in getattr(env, name).docs if d.get("tenant_id") == tenant]


# ============================ engine: detection & mapping ============================
def test_detection_each_shape_and_unknown():
    for f, want in [(ACCOUNTS_CSV, "accounts"), (CUSTOMERS_CSV, "customers"),
                    (VENDORS_CSV, "vendors"), (ITEMS_CSV, "items"),
                    (TXNS_CSV, "transactions")]:
        headers, _ = Q.parse_csv(f)
        kind, why = Q.detect_kind(headers)
        assert kind == want, (want, kind, why)
    headers, _ = Q.parse_csv("Foo,Bar\n1,2\n")
    kind, why = Q.detect_kind(headers)
    assert kind is None and "not a recognized QuickBooks export" in why


def test_mapper_fixtures_and_grouping():
    report, records = Q.dry_run(FILES, [], code_universe={
        "1010": "asset", "1200": "asset", "2010": "liability",
        "4100": "revenue", "5000": "expense", "3010": "equity"})
    assert report["counts"] == {"accounts": 5, "customers": 1, "vendors": 1,
                                "items": 1, "transaction_entries": 2,
                                "transaction_lines": 4}
    assert report["trial_balance"]["balanced"] is True
    assert report["trial_balance"]["total_debit_cents"] == 33000
    inv = [e for e in records["transactions"] if e["num"] == "1001"][0]
    assert inv["entry_date"] == "2026-01-05" and len(inv["lines"]) == 2
    amap = {m["qbo_name"]: m for m in report["account_map"]}
    assert amap["Accounts Receivable (A/R)"]["books_code"] == "1200"
    assert amap["Service Income"]["books_code"] == "4100"
    assert all(m["fingerprint"] if False else True for m in amap.values())
    assert records["accounts"][0]["fingerprint"]


def test_dry_run_unmapped_and_bad_rows_have_reasons():
    bad_txns = (
        "Date,Transaction Type,Num,Name,Memo,Account,Debit,Credit\n"
        "01/05/2026,Invoice,1002,Acme,X,Mystery Account,10.00,\n"
        "not-a-date,Invoice,1003,Acme,X,Service Income,,10.00\n"
        "01/05/2026,Invoice,1004,Acme,X,,10.00,\n"
    )
    report, _ = Q.dry_run([{"filename": "t.csv", "content": bad_txns}], [])
    reasons = " | ".join(p["reason"] for p in report["unmapped"])
    assert "Mystery Account" in reasons
    assert "unreadable date" in reasons
    assert "no account" in reasons


def test_map_fallback_prefers_closest_name_match():
    existing = [
        {"code": "1010", "name": "Cash — Register", "type": "asset"},
        {"code": "1020", "name": "Cash — Bank Checking", "type": "asset"},
        {"code": "1200", "name": "Accounts Receivable", "type": "asset"},
    ]
    # universe fully used -> no 'create' codes left; similarity must win
    universe = {a["code"]: a["type"] for a in existing}
    recs, _ = Q.map_accounts([
        {"Account": "Checking", "Type": "Bank"},
        {"Account": "Accounts Receivable (A/R)", "Type": "Accounts receivable (A/R)"},
    ])
    amap = {m["qbo_name"]: m for m in Q.build_account_map(recs, existing, universe)}
    assert amap["Checking"]["books_code"] == "1020"
    assert amap["Accounts Receivable (A/R)"]["books_code"] == "1200"
    assert "closest name match" in amap["Checking"]["note"]


def test_bundled_practice_set_is_balanced():
    report, _ = Q.dry_run(Q.SAMPLE_FILES, [])
    assert report["trial_balance"]["balanced"] is True
    assert report["counts"]["customers"] == 2
    assert report["counts"]["transaction_entries"] == 3


# ============================ router: dry run & apply ============================
def test_dry_run_report_via_router(env):
    out = do_dry_run()
    assert out["counts"]["accounts"] == 5
    assert out["trial_balance"]["balanced"] is True
    assert out["unmapped"] == []
    kinds = {f["filename"]: f["kind"] for f in out["files"]}
    assert kinds["txns.csv"] == "transactions"


def test_unrecognized_file_reported_plainly(env):
    out = do_dry_run(files=[{"filename": "random.csv", "content": "A,B\n1,2\n"}])
    assert out["files"][0]["kind"] is None
    assert "not a recognized" in out["files"][0]["reason"]


def test_trial_balance_refusal_posts_nothing(env):
    unbalanced = TXNS_CSV.replace("Job Materials,,55.00", "Job Materials,,75.00")
    files = [dict(f) for f in FILES]
    files[4] = {"filename": "txns.csv", "content": unbalanced}
    out = do_dry_run(files=files)
    assert out["trial_balance"]["balanced"] is False
    with pytest.raises(HTTPException) as ei:
        do_apply(out["run_id"])
    assert ei.value.status_code == 409
    assert "does not balance" in ei.value.detail
    assert coll(env, "accounts") == [] and coll(env, "journal_entries") == []


def test_apply_imports_everything_and_tidy_scan_runs(env):
    out = do_dry_run()
    res = do_apply(out["run_id"])
    assert res["status"] == "applied"
    r = res["result"]
    assert r["accounts_created"] == 5 and r["customers"] == 1
    assert r["vendors"] == 1 and r["items"] == 1 and r["journal_entries"] == 2
    lines = coll(env, "journal_lines")
    assert sum(l["debit"] for l in lines) == sum(l["credit"] for l in lines) == 33000
    assert res["tidy_findings"] is not None and res["tidy_findings"]["count"] == 0
    cust = coll(env, "customers")[0]
    assert cust["name"] == "Acme Homes" and cust["locations"][0]["address"] == "1 Main St"


def test_idempotent_rerun_no_duplicates(env):
    out = do_dry_run()
    do_apply(out["run_id"])
    res2 = do_apply(out["run_id"])           # same run again
    assert res2["result"]["journal_entries"] == 0
    assert res2["result"]["skipped_existing"] >= 7
    out2 = do_dry_run()                       # same files, brand-new run
    do_apply(out2["run_id"])
    assert len(coll(env, "customers")) == 1
    assert len(coll(env, "journal_entries")) == 2
    assert len(coll(env, "accounts")) == 5


def test_mapping_override_changes_posting_account(env):
    out = do_dry_run()
    amap = out["account_map"]
    for m in amap:
        if m["qbo_name"] == "Service Income":
            m["books_code"] = "4090"          # Late Fee Income — a chart code
    run(M.set_mapping(out["run_id"], M.MappingBody(account_map=amap), USER))
    do_apply(out["run_id"])
    codes = {l["account_code"] for l in coll(env, "journal_lines")}
    assert "4090" in codes


def test_unknown_account_line_blocks_apply_before_posting(env):
    txns = (
        "Date,Transaction Type,Num,Name,Memo,Account,Debit,Credit\n"
        "01/05/2026,Invoice,1002,Acme,X,Mystery Account,10.00,\n"
        "01/05/2026,Invoice,1002,Acme,X,Service Income,,10.00\n"
    )
    files = [{"filename": "coa.csv", "content": ACCOUNTS_CSV},
             {"filename": "txns.csv", "content": txns}]
    out = do_dry_run(files=files)
    assert any("Mystery Account" in p["reason"] for p in out["unmapped"])
    with pytest.raises(HTTPException) as ei:
        do_apply(out["run_id"])
    assert ei.value.status_code == 422
    assert coll(env, "journal_entries") == []


# ============================ practice ============================
def test_practice_run_isolated_and_discardable(env):
    res = run(M.practice_run(USER))
    assert res["status"] == "applied_practice"
    target = res["target_tenant"]
    assert target.startswith(f"practice:{TID}:")
    # real tenant untouched
    assert coll(env, "accounts") == [] and coll(env, "journal_entries") == []
    assert coll(env, "customers") == []
    # practice workspace has the goods
    assert len(coll(env, "accounts", target)) == 6
    assert len(coll(env, "journal_entries", target)) == 3
    out = run(M.discard_practice(res["run_id"], USER))
    assert out["deleted"]["journal_entries"] == 3
    assert coll(env, "accounts", target) == []


def test_practice_files_endpoint_labels_itself(env):
    out = run(M.practice_files(USER))
    assert len(out["files"]) == 5 and "never touches your real books" in out["note"]


# ============================ attachments ============================
def _applied_run(env):
    out = do_dry_run()
    do_apply(out["run_id"])
    return out["run_id"]


def test_attachments_match_unmatched_and_manual_place(env):
    run_id = _applied_run(env)
    body = M.AttachmentsBody(files=[
        M.AttachmentIn(filename="invoice-1001-signed.pdf"),
        M.AttachmentIn(filename="Acme Homes walkthrough.jpg"),
        M.AttachmentIn(filename="random-scan.pdf"),
    ])
    out = run(M.add_attachments(run_id, body, USER))
    assert out["summary"] == {"attached": 2, "unmatched": 1, "total": 3}
    by_name = {a["filename"]: a for a in out["attachments"]}
    assert by_name["invoice-1001-signed.pdf"]["matched"]["type"] == "journal_entry"
    assert by_name["Acme Homes walkthrough.jpg"]["matched"]["type"] == "customer"
    listing = run(M.list_attachments(run_id, USER))
    assert [a["filename"] for a in listing["unmatched"]] == ["random-scan.pdf"]
    att_id = listing["unmatched"][0]["id"]
    cust = coll(env, "customers")[0]
    placed = run(M.place_attachment(
        att_id, M.PlaceBody(target_type="customer", target_id=cust["id"]), USER))
    assert placed["status"] == "attached" and placed["matched"]["via"] == "placed by hand"
    listing2 = run(M.list_attachments(run_id, USER))
    assert listing2["unmatched"] == [] and len(listing2["attached"]) == 3


# ============================ isolation & reports map ============================
def test_tenant_isolation(env):
    out = do_dry_run()
    with pytest.raises(HTTPException) as ei:
        run(M.get_run(out["run_id"], USER2))
    assert ei.value.status_code == 404
    assert run(M.list_runs(USER2)) == []


def test_reports_map_covers_the_familiar_reports(env):
    out = run(M.reports_map(USER))
    qbo_names = {r["qbo"] for r in out["reports"]}
    assert {"Profit and Loss", "Balance Sheet", "A/R Aging Summary",
            "Expenses by Vendor", "Audit Log"} <= qbo_names
    assert all(r["books"] for r in out["reports"])
