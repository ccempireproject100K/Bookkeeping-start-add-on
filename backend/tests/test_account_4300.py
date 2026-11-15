"""FIX-IT-FIRST regression suite: the account-4300 defect family on main.

On main (e8eebc8), code referenced revenue account "4300", which exists in
NO chart of accounts (not BASE_COA, not any niche pack), so the ledger's
static validation (lib/ledger.py `_validate_lines`: code must be in
ACCOUNT_TYPE_OF, the base+packs union) rejected every posting against it:

- POST /jobs/{id}/invoice could never post (routers/jobs.py hardcoded 4300)
- seed.py crashed with "Unknown account 4300" (demo services + job invoices)
- the Budget-vs-Actual frontend defaulted new budgets to 4300, and
  POST /reports/budgets validates against the same union -> 422
- the Estimates frontend defaulted new line items to 4300, which then
  failed the same way when an estimate converted to an invoice

Same family, found in the audit: GRNI "2020" (purchase_orders.py) exists in
no chart either, so PO receiving failed static validation for every tenant.

Fix (decided from the codebase's own conventions):
- 4300 references point at the established revenue accounts instead:
  jobs -> "4000" (the ONLY revenue code present in every niche pack's
  chart, each pack's primary service-revenue line; a hardcoded cross-niche
  code must be universal or I3 tenant-chart validation still fails --
  "4020", the model default for user-chosen lines, is missing from the
  appliance_repair and auto_repair packs);
  seed demo services -> "4020" (posting.py REVENUE_FOR["service"], the
  same code seed.py already used for its other service);
  seed/frontend budgets -> "4000".
- 2020 is a legitimate system account (a named accrual every tenant needs),
  so it is ADDED to BASE_COA as ("2020", "Goods Received Not Invoiced",
  liability, credit) -- the treatment the committed-costs lane used.

Direct-call tests: no mongod, no network. Router endpoints are awaited
directly with a fake user dict and an in-memory FakeDB monkeypatched into
every module under test (harness shared with the sibling lanes).
"""
import asyncio
import copy
import os
import re
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import HTTPException

from lib import audit as audit_mod
from lib.base import Scoped
from lib.coa import ACCOUNT_TYPE_OF, BASE_COA, NAME_OF, NORMAL_BALANCE_OF, full_coa, seed_coa
from routers import jobs as jobs_router
from routers import purchase_orders as po_router
from routers import reports as reports_router

TID = "tenant-test"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}


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


def _apply_update(doc, update):
    for op, fields in (update or {}).items():
        if op == "$set":
            for k, v in fields.items():
                doc[k] = copy.deepcopy(v)
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
    for mod in (jobs_router, po_router, reports_router, audit_mod):
        monkeypatch.setattr(mod, "db", fake)
    return fake


def seed_chart(env, niche="barbershop"):
    run(seed_coa(Scoped(env.accounts, TID), niche))


def mk_job(**kw):
    job = {"id": "job-1", "job_number": "JOB-0001", "customer_name": "Acme",
           "status": "active", "tenant_id": TID,
           "estimated_revenue_cents": 200_000,
           "estimated_costs_cents": {"material": 0, "labor": 0, "equipment": 0,
                                     "subcontractor": 0, "other": 0},
           "change_orders": []}
    job.update(kw)
    return job


def posted_lines(env, source_ref):
    entry = next(e for e in env.journal_entries.docs
                 if e.get("source_ref") == source_ref)
    lines = [l for l in env.journal_lines.docs if l["entry_id"] == entry["id"]]
    return entry, lines


# ==================== symptom 1: job invoicing ====================
def test_job_invoice_posts_balanced_entry(env):
    seed_chart(env)
    env.jobs.docs.append(mk_job())
    doc = run(jobs_router.invoice_job(
        "job-1", jobs_router.JobInvoiceBody(amount=1000.0, tax=0), USER))
    assert doc["status"] == "issued"
    assert doc["lines"][0]["revenue_code"] == "4000"
    entry, lines = posted_lines(env, doc["id"])
    assert entry["number"] and entry["number"].startswith("JE-")
    assert sum(l["debit"] for l in lines) == sum(l["credit"] for l in lines) == 100_000
    rev = [l for l in lines if l["credit"] == 100_000]
    assert rev and rev[0]["account_code"] == "4000"
    ar = [l for l in lines if l["debit"] == 100_000]
    assert ar and ar[0]["account_code"] == "1200"


def test_job_invoice_posts_for_appliance_repair_tenant(env):
    """4000 is the only revenue code in EVERY pack. appliance_repair has
    no 4020, so a hardcoded 4020 would still fail tenant-chart validation
    (I3) for those tenants; 4000 must post cleanly."""
    seed_chart(env, "appliance_repair")
    codes = {a["code"] for a in env.accounts.docs}
    assert "4000" in codes and "4020" not in codes
    env.jobs.docs.append(mk_job())
    doc = run(jobs_router.invoice_job(
        "job-1", jobs_router.JobInvoiceBody(amount=250.0, tax=0), USER))
    assert doc["status"] == "issued"


# ==================== symptom 2 (family): PO receive / GRNI 2020 ====================
def test_grni_account_in_base_chart():
    assert ACCOUNT_TYPE_OF.get("2020") == "liability"
    assert NORMAL_BALANCE_OF.get("2020") == "credit"
    assert NAME_OF.get("2020") == "Goods Received Not Invoiced"
    assert any(c[0] == "2020" for c in BASE_COA)


def test_po_receive_posts_grni_entry(env):
    seed_chart(env)
    env.products.docs.append({"id": "prod-1", "tenant_id": TID, "name": "Filter",
                              "quantity_on_hand": 0, "unit_cost_cents": 1000})
    env.purchase_orders.docs.append({
        "id": "po-1", "tenant_id": TID, "number": "PO-0001", "vendor": "SupplyCo",
        "status": "approved",
        "lines": [{"product_id": "prod-1", "product_name": "Filter",
                   "quantity": 2, "received_qty": 0, "unit_cost_cents": 1000}],
    })
    out = run(po_router.receive_po(
        "po-1", po_router.ReceiveBody(
            lines=[po_router.ReceiveLine(product_id="prod-1", quantity=2)]), USER))
    assert out["status"] == "received"
    entry, lines = posted_lines(env, "po-1")
    assert sum(l["debit"] for l in lines) == sum(l["credit"] for l in lines) == 2000
    by_code = {l["account_code"]: l for l in lines}
    assert by_code["1300"]["debit"] == 2000
    assert by_code["2020"]["credit"] == 2000


# ==================== symptom 3: budgets validate the same way ====================
def test_budget_endpoint_rejects_4300_accepts_4000(env):
    seed_chart(env)
    with pytest.raises(HTTPException) as ei:
        run(reports_router.set_budget(
            reports_router.BudgetBody(year="2026", account_code="4300",
                                      amount=100.0), USER))
    assert ei.value.status_code == 422
    ok = run(reports_router.set_budget(
        reports_router.BudgetBody(year="2026", account_code="4000",
                                  amount=100.0), USER))
    assert ok["account_code"] == "4000"


# ==================== source guards: no 4300 anywhere ====================
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_SRC = os.path.join(BACKEND, "..", "frontend", "src")


def _scan(root, exts):
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        if "node_modules" in dirpath or "__pycache__" in dirpath:
            continue
        for f in files:
            if f.endswith(exts) and f != os.path.basename(__file__):
                p = os.path.join(dirpath, f)
                with open(p, errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if re.search(r"""["']4300["']""", line):
                            hits.append(f"{p}:{i}: {line.strip()}")
    return hits


def test_no_4300_literals_remain():
    hits = _scan(BACKEND, (".py",)) + _scan(FRONTEND_SRC, (".js", ".jsx"))
    assert hits == [], "4300 references remain:\n" + "\n".join(hits)


def test_seed_demo_codes_exist_in_seeded_chart():
    """seed.py seeds its demo tenant with seed_coa's default (barbershop)
    chart; every revenue/budget code it uses must exist in that chart."""
    import seed as seed_mod
    chart = {c[0] for c in full_coa("barbershop")}
    for _kind, _name, _price, code in seed_mod.SERVICES:
        assert code in ACCOUNT_TYPE_OF and code in chart
    src = open(os.path.join(BACKEND, "seed.py")).read()
    budget_block = re.search(r"for code, amt in \[(.*?)\]:", src, re.S).group(1)
    for code in re.findall(r'"(\d{4})"', budget_block):
        assert code in ACCOUNT_TYPE_OF and code in chart
