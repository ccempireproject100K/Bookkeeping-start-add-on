"""AIA-style progress billing (job-costing ask #8) — direct-call tests:
no mongod, no network. The pure G702/G703 math in lib/aia is exercised
against hand-computed fixtures; the async assembly (SOV save, periods,
freeze, carry-forward, invoice posting) runs against an in-memory
FakeDB monkeypatched into lib.aia (FakeDB pattern copied from the WIP
lane's suite). The journal engine itself is stubbed at the lib.aia
boundary — the lines AIA builds are asserted exactly; the engine's own
posting is covered by the ledger suites.
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

from lib import aia as A

TID = "tenant-aia"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}


def run(coro):
    return asyncio.run(coro)


# ============================ FakeDB ============================
_MISSING = object()


def _matches(doc, query):
    for key, cond in (query or {}).items():
        val = doc.get(key, _MISSING)
        if isinstance(cond, dict) and cond and all(k.startswith("$")
                                                   for k in cond):
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
            self._docs.sort(key=lambda d: (d.get(field) is None,
                                           d.get(field)),
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

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _matches(d, query))


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


class JournalCapture:
    """Stands in for lib.ledger.post_journal_entry at the lib.aia
    boundary: records the draft, returns a numbered entry."""

    def __init__(self):
        self.drafts = []

    async def __call__(self, db_, tenant_id, user, draft, status="posted",
                       idempotency_key=None):
        self.drafts.append(copy.deepcopy(draft))
        assert sum(l["debit"] for l in draft["lines"]) == \
            sum(l["credit"] for l in draft["lines"]), "unbalanced draft"
        return {"number": f"JE-2026-{len(self.drafts):06d}",
                "draft": draft}


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(A, "db", fake)
    cap = JournalCapture()
    monkeypatch.setattr(A.L, "post_journal_entry", cap)

    async def _audit(*a, **k):
        return None
    monkeypatch.setattr(A.L, "audit", _audit)
    return fake, cap


# ============================ fixtures ============================
def mk_job(**kw):
    job = {
        "id": "job-1", "job_number": "JOB-0007",
        "customer_name": "Acme Development", "status": "active",
        "estimated_revenue_cents": 10_000_000,       # $100,000 original
        "estimated_costs_cents": {"material": 3_000_000, "labor": 3_000_000,
                                  "equipment": 500_000,
                                  "subcontractor": 1_000_000,
                                  "other": 500_000},
        "retainage_pct": 5,
        "change_orders": [
            {"id": "co-1", "description": "Added lobby",
             "revenue_delta_cents": 1_000_000, "cost_delta_cents": 700_000,
             "state": "approved"},
            {"id": "co-2", "description": "Pending extra",
             "revenue_delta_cents": 500_000, "cost_delta_cents": 300_000,
             "state": "pending"},
            {"id": "co-3", "description": "Legacy approved",
             "revenue_delta_cents": 200_000, "cost_delta_cents": 100_000,
             "approved": True},
            {"id": "co-4", "description": "Rejected extra",
             "revenue_delta_cents": 900_000, "cost_delta_cents": 100_000,
             "state": "rejected"},
        ],
        "created_at": "2026-10-01T00:00:00Z",
    }
    job.update(kw)
    return job


# Contract sum to date: 10,000,000 + 1,000,000 + 200,000 = 11,200,000
SOV_LINES = [
    {"item_no": "1.00", "description": "Site work",
     "scheduled_value_cents": 2_000_000, "retainage_pct": 10},
    {"item_no": "2.00", "description": "Concrete",
     "scheduled_value_cents": 5_000_000},
    {"item_no": "3.00", "description": "Steel",
     "scheduled_value_cents": 4_200_000},
]


def seed_job(env, job=None, tenant=TID):
    fake, _ = env
    job = job or mk_job()
    doc = dict(job)
    doc["tenant_id"] = tenant
    fake.jobs.docs.append(doc)
    return job


def line_ids(sov):
    return {l["item_no"]: l["id"] for l in sov["lines"]}


# ============================ pure math ============================
def test_contract_figures_approved_only():
    f = A.contract_figures(mk_job())
    assert f["original_contract_cents"] == 10_000_000
    assert f["change_orders_cents"] == 1_200_000   # pending+rejected excluded
    assert f["contract_sum_to_date_cents"] == 11_200_000
    assert [c["id"] for c in f["change_orders"]] == ["co-1", "co-3"]


def test_sov_sum_check_match_and_warning():
    ok = A.sov_sum_check(SOV_LINES, 11_200_000)
    assert ok["matches"] is True and ok["warning"] is None
    bad = A.sov_sum_check(SOV_LINES, 11_400_000)
    assert bad["matches"] is False
    assert "under by $2,000.00" in bad["warning"]


def test_application_math_period1():
    job = mk_job()
    lines = [dict(l, id=f"line-{i}") for i, l in enumerate(SOV_LINES, 1)]
    entries = {
        "line-1": {"this_period_cents": 1_000_000, "stored_cents": 0},
        "line-2": {"this_period_cents": 1_250_000, "stored_cents": 250_000},
    }
    app = A.compute_application(job, lines, None, entries, 1)
    assert app["format"] == "AIA-style (G702/G703 format)"
    assert app["total_completed_stored_cents"] == 2_500_000
    assert app["retainage_total_cents"] == 175_000     # 100k line + 75k job
    assert app["total_earned_less_retainage_cents"] == 2_325_000
    assert app["less_previous_certificates_cents"] == 0
    assert app["current_payment_due_cents"] == 2_325_000
    assert app["balance_to_finish_cents"] == 8_700_000
    rows = {r["item_no"]: r for r in app["lines"]}
    assert rows["1.00"]["pct_complete"] == 50.0
    assert rows["1.00"]["retainage_pct"] == 10
    assert rows["1.00"]["retainage_pct_source"] == "line"
    assert rows["2.00"]["total_completed_stored_cents"] == 1_500_000
    assert rows["2.00"]["pct_complete"] == 30.0
    assert rows["2.00"]["retainage_pct_source"] == "job"
    assert rows["3.00"]["balance_to_finish_cents"] == 4_200_000
    # per-line retainage sums to the application total
    assert sum(r["retainage_held_cents"] for r in app["lines"]) == \
        app["retainage_total_cents"]


def test_application_math_period2_carry_forward():
    job = mk_job()
    lines = [dict(l, id=f"line-{i}") for i, l in enumerate(SOV_LINES, 1)]
    e1 = {"line-1": {"this_period_cents": 1_000_000, "stored_cents": 0},
          "line-2": {"this_period_cents": 1_250_000, "stored_cents": 250_000}}
    app1 = A.compute_application(job, lines, None, e1, 1)
    e2 = {"line-1": {"this_period_cents": 1_000_000, "stored_cents": 0},
          "line-2": {"this_period_cents": 500_000, "stored_cents": 0},
          "line-3": {"this_period_cents": 840_000, "stored_cents": 420_000}}
    app2 = A.compute_application(job, lines, app1, e2, 2)
    rows = {r["item_no"]: r for r in app2["lines"]}
    assert rows["1.00"]["previous_completed_cents"] == 1_000_000
    assert rows["1.00"]["total_completed_stored_cents"] == 2_000_000
    assert rows["1.00"]["pct_complete"] == 100.0
    assert rows["3.00"]["total_completed_stored_cents"] == 1_260_000
    assert app2["total_completed_stored_cents"] == 5_260_000
    assert app2["retainage_total_cents"] == 363_000
    assert app2["less_previous_certificates_cents"] == 2_325_000
    assert app2["current_payment_due_cents"] == 2_572_000
    assert app2["balance_to_finish_cents"] == 5_940_000


def test_retainage_phase_override_and_line_priority():
    job = mk_job(phases=[{"id": "ph1", "name": "Structure"}],
                 retainage_phase_pcts={"ph1": {"receivable": 7.5}})
    line = {"id": "x", "item_no": "9.00", "description": "Framing",
            "scheduled_value_cents": 1_000_000, "phase_id": "ph1"}
    rows = A.compute_lines(job, [line], {},
                           {"x": {"this_period_cents": 400_000,
                                  "stored_cents": 0}})
    assert rows[0]["retainage_pct"] == 7.5
    assert rows[0]["retainage_pct_source"] == "phase"
    assert rows[0]["retainage_held_cents"] == 30_000
    assert rows[0]["phase_name"] == "Structure"
    line2 = dict(line, retainage_pct=2)
    rows = A.compute_lines(job, [line2], {},
                           {"x": {"this_period_cents": 400_000,
                                  "stored_cents": 0}})
    assert rows[0]["retainage_pct"] == 2   # line % beats phase


# ============================ assembly ============================
def test_sov_save_keeps_line_ids_stable(env):
    job = seed_job(env)
    sov = run(A.save_sov(TID, USER, job, SOV_LINES))
    ids1 = line_ids(sov)
    assert sov["sum_check"]["matches"] is True
    sov2 = run(A.save_sov(TID, USER, job,
                          [dict(l, description="Site work v2")
                           for l in SOV_LINES]))
    assert line_ids(sov2) == ids1


def test_full_two_period_flow_with_invoices(env):
    fake, cap = env
    job = seed_job(env)
    sov = run(A.save_sov(TID, USER, job, SOV_LINES))
    ids = line_ids(sov)

    p1 = run(A.open_period(TID, job, label="October"))
    with pytest.raises(ValueError):
        run(A.open_period(TID, job))          # one open at a time
    run(A.set_entries(TID, p1, {
        ids["1.00"]: {"this_period_cents": 1_000_000, "stored_cents": 0},
        ids["2.00"]: {"this_period_cents": 1_250_000,
                      "stored_cents": 250_000}}, sov["lines"]))
    closed1 = run(A.close_period(TID, USER, job, p1))
    app1 = closed1["application"]
    assert app1["current_payment_due_cents"] == 2_325_000
    assert closed1["invoice_id"]
    inv1 = fake.invoices.docs[0]
    assert inv1["subtotal_cents"] == 2_500_000
    assert inv1["retainage_held_cents"] == 175_000
    assert inv1["net_due_cents"] == 2_325_000
    assert inv1["aia_application_no"] == 1
    # journal: Dr AR net, Dr retainage held, Cr revenue gross
    jl = {l["account_code"]: (l["debit"], l["credit"])
          for l in cap.drafts[0]["lines"]}
    assert jl["1200"] == (2_325_000, 0)
    assert jl["1210"] == (175_000, 0)
    assert jl["4000"] == (0, 2_500_000)
    # the 1210 account was ensured for the tenant
    assert any(a["code"] == "1210" for a in fake.accounts.docs)

    p2 = run(A.open_period(TID, job, label="November"))
    run(A.set_entries(TID, p2, {
        ids["1.00"]: {"this_period_cents": 1_000_000, "stored_cents": 0},
        ids["2.00"]: {"this_period_cents": 500_000, "stored_cents": 0},
        ids["3.00"]: {"this_period_cents": 840_000,
                      "stored_cents": 420_000}}, sov["lines"]))
    closed2 = run(A.close_period(TID, USER, job, p2))
    app2 = closed2["application"]
    assert app2["current_payment_due_cents"] == 2_572_000
    inv2 = fake.invoices.docs[1]
    assert inv2["subtotal_cents"] == 2_760_000       # this period's growth
    assert inv2["retainage_held_cents"] == 188_000
    assert inv2["net_due_cents"] == 2_572_000


def test_closed_period_is_frozen(env):
    job = seed_job(env)
    sov = run(A.save_sov(TID, USER, job, SOV_LINES))
    ids = line_ids(sov)
    p1 = run(A.open_period(TID, job))
    run(A.set_entries(TID, p1, {
        ids["1.00"]: {"this_period_cents": 1_000_000, "stored_cents": 0}},
        sov["lines"]))
    closed = run(A.close_period(TID, USER, job, p1))
    frozen_total = closed["application"]["total_completed_stored_cents"]

    with pytest.raises(ValueError):
        run(A.set_entries(TID, closed, {
            ids["1.00"]: {"this_period_cents": 9, "stored_cents": 0}},
            sov["lines"]))
    with pytest.raises(ValueError):
        run(A.close_period(TID, USER, job, closed))

    # editing the SOV afterwards cannot move the frozen application
    run(A.save_sov(TID, USER, job,
                   [dict(l, scheduled_value_cents=9_999_999)
                    if l["item_no"] == "1.00" else l for l in SOV_LINES]))
    stored = run(A.get_period(TID, p1["id"]))
    assert stored["application"]["total_completed_stored_cents"] == \
        frozen_total == 1_000_000
    assert stored["application"]["lines"][0]["scheduled_value_cents"] == \
        2_000_000


def test_zero_period_close_posts_no_invoice(env):
    fake, cap = env
    job = seed_job(env)
    run(A.save_sov(TID, USER, job, SOV_LINES))
    p1 = run(A.open_period(TID, job))
    closed = run(A.close_period(TID, USER, job, p1))
    assert closed["billing"]["posted"] is False
    assert closed["invoice_id"] is None
    assert fake.invoices.docs == [] and cap.drafts == []
    assert closed["application"]["current_payment_due_cents"] == 0


def test_unknown_line_entry_rejected(env):
    job = seed_job(env)
    sov = run(A.save_sov(TID, USER, job, SOV_LINES))
    p1 = run(A.open_period(TID, job))
    with pytest.raises(ValueError):
        run(A.set_entries(TID, p1,
                          {"nope": {"this_period_cents": 1,
                                    "stored_cents": 0}}, sov["lines"]))


def test_tenant_isolation(env):
    job = seed_job(env)
    run(A.save_sov(TID, USER, job, SOV_LINES))
    assert run(A.get_sov(TID2, job["id"])) is None
    assert run(A.list_periods(TID2, job["id"])) == []
    live = run(A.live_application(TID2, job))
    assert live["has_sov"] is False


def test_non_sov_job_untouched(env):
    fake, _ = env
    job = seed_job(env)
    live = run(A.live_application(TID, job))
    assert live == {"has_sov": False, "period": None, "application": None}
    assert fake.aia_sovs.docs == [] and fake.aia_periods.docs == []


def test_document_lines_are_honest_and_traceable(env):
    job = mk_job()
    lines = [dict(l, id=f"line-{i}") for i, l in enumerate(SOV_LINES, 1)]
    app = A.compute_application(
        job, lines, None,
        {"line-1": {"this_period_cents": 1_000_000, "stored_cents": 0}}, 1)
    text = "\n".join(A.document_lines(job, app, {"label": "October",
                                                 "period_no": 1}))
    assert "AIA-style (G702/G703 format)" in text
    assert "not an official AIA form" in text
    assert "$9,000.00" in text   # $10k completed at 10% line retainage
    pdf = A.pdf_bytes("t", A.document_lines(job, app, None))
    assert pdf.startswith(b"%PDF-1.4")


# ============================ owner additions ============================
def test_seed_from_estimate_categories():
    lines = A.seed_lines_from_estimate(mk_job())
    by_desc = {l["description"]: l["scheduled_value_cents"] for l in lines}
    assert by_desc == {"Material": 3_000_000, "Labor": 3_000_000,
                       "Equipment": 500_000, "Subcontractor": 1_000_000,
                       "Other": 500_000}
    assert [l["item_no"] for l in lines] == ["1.00", "2.00", "3.00",
                                             "4.00", "5.00"]


def test_seed_from_phases_when_phased():
    job = mk_job(phases=[
        {"id": "ph1", "name": "Rough-in",
         "budget_cents": {"material": 100_000, "labor": 200_000,
                          "equipment": 0, "subcontractor": 0,
                          "other": 0}},
        {"id": "ph2", "name": "Trim",
         "budget_cents": {"material": 50_000, "labor": 50_000,
                          "equipment": 0, "subcontractor": 0,
                          "other": 0}},
        {"id": "ph3", "name": "Unbudgeted", "budget_cents": {}},
    ])
    lines = A.seed_lines_from_estimate(job)
    assert [(l["description"], l["scheduled_value_cents"], l["phase_id"])
            for l in lines] == [("Rough-in", 300_000, "ph1"),
                                ("Trim", 100_000, "ph2")]


def test_mode_enable_seeds_and_disable_keeps(env):
    fake, _ = env
    job = seed_job(env)
    res = run(A.set_mode(TID, USER, job, True))
    assert res["aia_enabled"] is True and res["seeded"] is True
    assert res["sov"]["lines"]            # pre-filled from the estimate
    stored = fake.jobs.docs[0]
    assert stored["aia_enabled"] is True
    # turning it off hides the mode but keeps the data
    res2 = run(A.set_mode(TID, USER, job, False))
    assert res2["aia_enabled"] is False and res2["seeded"] is False
    assert run(A.get_sov(TID, job["id"])) is not None


def test_history_deltas_and_copy_forward(env):
    job = seed_job(env)
    sov = run(A.save_sov(TID, USER, job, SOV_LINES))
    ids = line_ids(sov)
    p1 = run(A.open_period(TID, job))
    assert p1["copied_from_application_no"] is None
    run(A.set_entries(TID, p1, {
        ids["1.00"]: {"this_period_cents": 1_000_000, "stored_cents": 0},
        ids["2.00"]: {"this_period_cents": 1_250_000,
                      "stored_cents": 250_000}}, sov["lines"]))
    run(A.close_period(TID, USER, job, p1))
    p2 = run(A.open_period(TID, job))
    assert p2["copied_from_application_no"] == 1   # copied forward
    run(A.set_entries(TID, p2, {
        ids["1.00"]: {"this_period_cents": 1_000_000, "stored_cents": 0}},
        sov["lines"]))
    run(A.close_period(TID, USER, job, p2))

    hist = A.period_history(run(A.list_periods(TID, job["id"])))
    assert [h["period_no"] for h in hist] == [1, 2]
    h1 = {l["item_no"]: l for l in hist[0]["lines"]}
    assert h1["1.00"]["previous_total_cents"] == 0
    assert h1["1.00"]["delta_total_cents"] == 1_000_000
    assert h1["2.00"]["delta_total_cents"] == 1_500_000
    h2 = {l["item_no"]: l for l in hist[1]["lines"]}
    assert h2["1.00"]["previous_total_cents"] == 1_000_000
    assert h2["1.00"]["delta_total_cents"] == 1_000_000
    assert h2["2.00"]["delta_total_cents"] == 0
    assert hist[1]["current_payment_due_cents"] is not None
    assert hist[0]["invoice_id"] and hist[1]["invoice_id"]


def test_retainage_account_in_static_chart():
    """Regression (found in the live drive): the ledger validates
    account codes against lib.coa's static ACCOUNT_TYPE_OF map, not
    the tenant's accounts collection — 1210 must be IN the chart or
    every retainage-bearing close fails with 'Unknown account 1210'."""
    from lib.coa import ACCOUNT_TYPE_OF, NORMAL_BALANCE_OF
    assert ACCOUNT_TYPE_OF["1210"] == "asset"
    assert NORMAL_BALANCE_OF["1210"] == "debit"
    assert ACCOUNT_TYPE_OF["2020"] == "liability"
    # 4000 (job revenue — the credit leg of every AIA close,
    # and of the jobs module's own invoice path) was likewise missing
    # from main's chart; the retainage lane added the same line.
    assert ACCOUNT_TYPE_OF["4000"] == "revenue"
    assert NORMAL_BALANCE_OF["4000"] == "credit"
