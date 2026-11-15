"""Job costing additions — committed costs + burdened labor
(owner-approved 2026-11-14, job-costing requests report #1/#2).

Direct-call tests: no mongod, no network. Endpoints and lib functions
are awaited directly with a fake user dict and an in-memory FakeDB
monkeypatched into the `db` attribute of every module under test
(same pattern as test_job_overrun_alerts.py). The one seam stubbed
is the ledger posting call inside jobs.add_cost — the double-entry
engine has its own coverage; the real posting path is exercised by
the live drive in ~/workspace/buildout/job-costing-committed-burden/proof/.
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

from lib import jobcosting as JC
from lib import jobalerts as ja_mod
from lib import audit as audit_mod
from routers import jobs as jobs_router
from routers import job_costing as jc_router
from routers import purchase_orders as po_router
from routers import bills as bills_router
from routers import time as time_router

TID = "tenant-test"
TID2 = "tenant-other"
USER = {"id": "user-1", "email": "owner@test.com", "name": "Owner",
        "role": "owner", "tenant_id": TID}
USER2 = {"id": "user-2", "email": "other@test.com", "name": "Other",
         "role": "owner", "tenant_id": TID2}
TECH = "tech-1"
TECH2 = "tech-2"


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

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                _apply_update(d, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

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
    # ja_mod included: in the merged tree jobs.add_cost also evaluates
    # budget alerts (job-overrun lane), which reads jobalerts' own db.
    for mod in (JC, jobs_router, jc_router, po_router, time_router,
                audit_mod, ja_mod):
        monkeypatch.setattr(mod, "db", fake)
    return fake


@pytest.fixture()
def stub_ledger(monkeypatch):
    """Stub the double-entry posting seam used by jobs.add_cost."""
    async def fake_post(db_, tenant_id, user, draft, status="posted"):
        return {"number": "JE-TEST-1"}
    monkeypatch.setattr(jobs_router.L, "post_journal_entry", fake_post)
    return fake_post


def seed_job(env, tenant=TID, est_cost=100_000, status="active", **kw):
    doc = {"id": kw.pop("id", "job-1"), "tenant_id": tenant,
           "job_number": "JOB-0001", "customer_name": "Acme",
           "status": status, "estimated_revenue_cents": 200_000,
           "estimated_costs_cents": {"labor": est_cost},
           "change_orders": [], "created_at": "2026-11-14T00:00:00"}
    doc.update(kw)
    env.jobs.docs.append(doc)
    return doc


def seed_cost(env, job_id="job-1", category="labor", amount=0, burden=0,
              tenant=TID, **kw):
    doc = {"id": uuid.uuid4().hex, "tenant_id": tenant, "job_id": job_id,
           "category": category, "amount_cents": amount,
           "burden_cents": burden, "created_at": "2026-11-14T00:00:00"}
    doc.update(kw)
    env.job_costs.docs.append(doc)
    return doc


def seed_po(env, job_id="job-1", status="approved", qty=2, unit=25_000,
            received=0, tenant=TID, **kw):
    doc = {"id": kw.pop("id", uuid.uuid4().hex), "tenant_id": tenant,
           "number": "PO-0001", "vendor": "SupplyCo", "status": status,
           "job_id": job_id, "bill_id": None, "billed_cents": None,
           "lines": [{"product_id": "prod-1", "product_name": "Filter",
                      "quantity": qty, "received_qty": received,
                      "unit_cost_cents": unit}],
           "created_at": "2026-11-14T00:00:00"}
    doc.update(kw)
    env.purchase_orders.docs.append(doc)
    return doc


def profit_of(env, job, user=USER):
    return run(jobs_router._profit(user, job))


def enable_committed(env, user=USER):
    return run(jc_router.patch_config_ep(
        jc_router.ConfigIn(committed_costs_enabled=True), user))


# ================== config defaults / invariance ==================

def test_config_defaults_are_todays_behaviour(env):
    cfg = run(JC.get_config(TID))
    assert cfg == {"committed_costs_enabled": False, "labor_view": "base",
                   "default_burden_pct": 0.0}


def test_budget_view_disabled_by_default(env):
    job = seed_job(env)
    out = run(JC.budget_view(TID, job, profit_of(env, job)))
    assert out == {"enabled": False}


def test_get_job_budget_has_no_jobcosting_fields_when_off(env):
    # Merged-tree contract (2026-11-14): get_job's "budget" key is owned
    # by the job-alerts lane (always present); the job-costing lane only
    # merges its fields in when its toggle is on. Off = no JC fields.
    job = seed_job(env)
    seed_po(env)  # even a linked approved PO is invisible while off
    got = run(jobs_router.get_job(job["id"], USER))
    assert got["budget"].get("enabled") is not True
    assert "commitments" not in got["budget"]
    assert "committed_cents" not in got["budget"]


def test_profit_values_unchanged_when_nothing_configured(env):
    job = seed_job(env)
    seed_cost(env, category="labor", amount=30_000)
    seed_cost(env, category="material", amount=10_000)
    p = profit_of(env, job)
    # every pre-existing figure exactly as the base module computes it
    assert p["actual_cost_cents"] == 40_000
    assert p["estimated_cost_cents"] == 100_000
    assert p["cost_variance_cents"] == -60_000
    # burden overlay present but zero — nothing forced on anybody
    assert p["labor_burden_cents"] == 0
    assert p["burdened_cost_cents"] == 40_000


def test_config_roundtrip_and_view_validation(env):
    out = run(jc_router.patch_config_ep(
        jc_router.ConfigIn(labor_view="both", default_burden_pct=32.5), USER))
    assert out["labor_view"] == "both"
    assert out["default_burden_pct"] == 32.5
    assert out["committed_costs_enabled"] is False  # untouched
    with pytest.raises(Exception):
        jc_router.ConfigIn(labor_view="sideways")


def test_config_is_tenant_isolated(env):
    run(jc_router.patch_config_ep(
        jc_router.ConfigIn(committed_costs_enabled=True), USER))
    assert run(JC.get_config(TID))["committed_costs_enabled"] is True
    assert run(JC.get_config(TID2))["committed_costs_enabled"] is False


# ================== committed costs ==================

def test_commitment_create_blocked_when_toggle_off(env):
    seed_job(env)
    with pytest.raises(HTTPException) as ei:
        run(jc_router.create_commitment(
            "job-1", jc_router.CommitmentIn(
                vendor_name="SubCo", amount=500.0), USER))
    assert ei.value.status_code == 409


def test_approved_po_counts_committed_draft_does_not(env):
    enable_committed(env)
    job = seed_job(env)
    seed_po(env, status="approved")            # 2 x $250 = $500
    seed_po(env, status="draft", id="po-2")    # ignored
    b = run(JC.budget_view(TID, job, profit_of(env, job)))
    assert b["enabled"] is True
    assert b["committed_po_cents"] == 50_000
    assert b["committed_cents"] == 50_000
    assert b["remaining_cents"] == 100_000 - 50_000


def test_grni_account_in_base_chart():
    """FIX-IT-FIRST regression: PO receive posts against GRNI 2020, but
    the code was missing from BASE_COA, so the ledger's static line
    validation rejected every receipt on main with 'Unknown account
    2020'."""
    from lib.coa import ACCOUNT_TYPE_OF, NORMAL_BALANCE_OF
    assert ACCOUNT_TYPE_OF.get("2020") == "liability"
    assert NORMAL_BALANCE_OF.get("2020") == "credit"


def test_po_create_validates_and_stores_job_link(env):
    env.products.docs.append({"id": "prod-1", "tenant_id": TID,
                              "name": "Filter"})
    seed_job(env)
    po = run(po_router.create_po(po_router.POCreate(
        vendor="SupplyCo", job_id="job-1",
        lines=[po_router.POLineIn(product_id="prod-1", quantity=1,
                                  unit_cost=10.0)]), USER))
    assert po["job_id"] == "job-1"
    with pytest.raises(HTTPException) as ei:
        run(po_router.create_po(po_router.POCreate(
            vendor="SupplyCo", job_id="nope",
            lines=[po_router.POLineIn(product_id="prod-1", quantity=1,
                                      unit_cost=10.0)]), USER))
    assert ei.value.status_code == 404


def test_po_billed_converts_committed_to_actual_no_double_count(env, monkeypatch):
    enable_committed(env)
    job = seed_job(env)
    po = seed_po(env, status="received", received=2)  # fully received
    before = run(JC.budget_view(TID, job, profit_of(env, job)))
    assert before["committed_po_cents"] == 50_000
    assert before["billed_commitment_cents"] == 0
    total_before = before["committed_cents"] + before["actual_total_cents"]

    async def fake_create_bill(body, user):
        return {"id": "bill-1"}
    monkeypatch.setattr(bills_router, "create_bill", fake_create_bill)
    # convert_to_bill does a function-local import from routers.bills,
    # which resolves to the same module object patched above.
    run(po_router.convert_to_bill(po["id"], po_router.ConvertBody(), USER))

    stored = next(d for d in env.purchase_orders.docs if d["id"] == po["id"])
    assert stored["status"] == "billed"
    assert stored["billed_cents"] == 50_000
    after = run(JC.budget_view(TID, job, profit_of(env, job)))
    assert after["committed_po_cents"] == 0
    assert after["billed_commitment_cents"] == 50_000
    assert after["actual_total_cents"] == 50_000
    # conversion moves the money; it never counts it twice
    assert after["committed_cents"] + after["actual_total_cents"] == total_before
    assert after["remaining_cents"] == 100_000 - 50_000


def test_subcontract_commitment_fulfils_down_via_cost(env, stub_ledger):
    enable_committed(env)
    job = seed_job(env)
    com = run(jc_router.create_commitment(
        "job-1", jc_router.CommitmentIn(vendor_name="SubCo", amount=800.0),
        USER))
    b = run(JC.budget_view(TID, job, profit_of(env, job)))
    assert b["committed_subcontract_cents"] == 80_000

    run(jobs_router.add_cost("job-1", jobs_router.CostBody(
        category="subcontractor", amount=300.0, commitment_id=com["id"]),
        USER))
    b = run(JC.budget_view(TID, job, profit_of(env, job)))
    assert b["committed_subcontract_cents"] == 50_000
    # the fulfilled $300 lives in actual costs — not added a second time
    assert b["actual_cost_cents"] == 30_000
    assert b["actual_total_cents"] == 30_000

    run(jobs_router.add_cost("job-1", jobs_router.CostBody(
        category="subcontractor", amount=600.0, commitment_id=com["id"]),
        USER))
    stored = next(d for d in env.job_commitments.docs if d["id"] == com["id"])
    assert stored["fulfilled_cents"] == 80_000  # capped at the commitment
    assert stored["status"] == "fulfilled"
    b = run(JC.budget_view(TID, job, profit_of(env, job)))
    assert b["committed_subcontract_cents"] == 0


def test_commitment_ignored_when_toggle_off(env, stub_ledger):
    seed_job(env)
    # hand-seed a commitment (as if created earlier, then toggled off)
    env.job_commitments.docs.append({
        "id": "com-1", "tenant_id": TID, "job_id": "job-1",
        "vendor_name": "SubCo", "amount_cents": 80_000,
        "fulfilled_cents": 0, "status": "approved",
        "created_at": "2026-11-14T00:00:00"})
    run(jobs_router.add_cost("job-1", jobs_router.CostBody(
        category="subcontractor", amount=300.0, commitment_id="com-1"),
        USER))
    stored = env.job_commitments.docs[0]
    assert stored["fulfilled_cents"] == 0  # untouched: today's behaviour


# ================== burdened labor ==================

def test_burden_math_rounds_half_up():
    assert JC.pct_cents(10_005, 10) == 1001   # 1000.5 -> 1001
    assert JC.pct_cents(0, 40) == 0
    amts = JC.labor_amounts(10_000, 32.5)
    assert amts == {"base_cents": 10_000, "burden_pct": 32.5,
                    "burden_cents": 3250, "burdened_cents": 13_250}


def test_template_assignment_wins_over_default(env):
    run(jc_router.patch_config_ep(
        jc_router.ConfigIn(default_burden_pct=20.0), USER))
    run(jc_router.create_template(jc_router.TemplateIn(
        name="Employees", kind="employee", pct=45.0,
        member_user_ids=[TECH]), USER))
    pct, tpl = run(JC.resolve_burden(TID, TECH))
    assert pct == 45.0 and tpl["name"] == "Employees"
    pct, tpl = run(JC.resolve_burden(TID, TECH2))
    assert pct == 20.0 and tpl is None
    pct, tpl = run(JC.resolve_burden(TID, None))
    assert pct == 20.0


def test_labor_cost_records_burden_ledger_posts_base(env, stub_ledger):
    run(jc_router.patch_config_ep(
        jc_router.ConfigIn(default_burden_pct=40.0), USER))
    seed_job(env)
    doc = run(jobs_router.add_cost("job-1", jobs_router.CostBody(
        category="labor", hours=10, rate=50.0, technician_id=TECH), USER))
    assert doc["amount_cents"] == 50_000          # base wage, as before
    assert doc["burden_pct"] == 40.0
    assert doc["burden_cents"] == 20_000
    assert doc["burdened_cents"] == 70_000
    job = next(d for d in env.jobs.docs if d["id"] == "job-1")
    p = profit_of(env, job)
    assert p["actual_cost_cents"] == 50_000       # ledger truth unchanged
    assert p["labor_burden_cents"] == 20_000
    assert p["burdened_cost_cents"] == 70_000
    assert p["burdened_by_category_cents"]["labor"] == 70_000


def test_labor_cost_no_burden_configured_is_unchanged(env, stub_ledger):
    seed_job(env)
    doc = run(jobs_router.add_cost("job-1", jobs_router.CostBody(
        category="labor", hours=2, rate=25.0, technician_id=TECH), USER))
    assert doc["amount_cents"] == 5_000
    assert doc["burden_cents"] == 0
    assert doc["burdened_cents"] == 5_000
    assert "burden_overrun" not in doc


def test_preview_shows_burdened_cost_before_posting(env):
    run(jc_router.patch_config_ep(
        jc_router.ConfigIn(default_burden_pct=25.0), USER))
    seed_job(env)
    out = run(jobs_router.preview_cost("job-1", jobs_router.CostBody(
        category="labor", hours=8, rate=50.0, technician_id=TECH), USER))
    assert out["base_cents"] == 40_000
    assert out["burden_cents"] == 10_000
    assert out["burdened_cents"] == 50_000
    assert out["burden_would_tip_over"] is False
    # nothing was written
    assert env.job_costs.docs == []


def test_burden_suggestion_math_from_payroll_runs(env):
    def run_doc(period_end, gross, fica, status="posted"):
        return {"id": uuid.uuid4().hex, "tenant_id": TID,
                "status": status, "period_start": "2026-11-01",
                "period_end": period_end,
                "items": [{"gross_cents": gross, "deductions": [
                    {"label": JC.FICA_LABEL, "amount_cents": fica},
                    {"label": "Federal withholding (est.)",
                     "amount_cents": 999_999}]}],
                "created_at": "2026-11-14T00:00:00"}
    env.payroll_runs.docs.append(run_doc("2026-11-01", 100_000, 7_650))
    env.payroll_runs.docs.append(run_doc("2026-11-08", 300_000, 22_950))
    env.payroll_runs.docs.append(run_doc("2026-11-08", 500_000, 38_250,
                                         status="draft"))  # excluded
    out = run(JC.burden_suggestion(TID))
    assert out["runs_count"] == 2
    assert out["gross_cents"] == 400_000
    assert out["employer_tax_cents"] == 30_600
    assert out["suggested_burden_pct"] == 7.65


def test_burden_suggestion_empty_is_honest(env):
    out = run(JC.burden_suggestion(TID))
    assert out["suggested_burden_pct"] is None
    assert "No payroll runs" in out["note"]


def test_suggestion_is_never_silently_applied(env):
    env.payroll_runs.docs.append({
        "id": "run-1", "tenant_id": TID, "status": "posted",
        "period_start": "2026-11-01", "period_end": "2026-11-08",
        "items": [{"gross_cents": 100_000, "deductions": [
            {"label": JC.FICA_LABEL, "amount_cents": 7_650}]}],
        "created_at": "2026-11-14T00:00:00"})
    s = run(JC.burden_suggestion(TID))
    assert s["suggested_burden_pct"] == 7.65
    # reading the suggestion changes nothing in config
    assert run(JC.get_config(TID))["default_burden_pct"] == 0.0


# ================== burden overrun flag ==================

def _profit(actual, burden, est=100_000):
    return {"actual_cost_cents": actual, "labor_burden_cents": burden,
            "estimated_cost_cents": est}


def test_burden_tip_fires_once_then_dedupes(env):
    job = seed_job(env)
    out = run(JC.evaluate_burden_overrun(TID, job, _profit(95_000, 10_000)))
    assert out["tipped"] is True and out["fired_now"] is True
    notifs = [d for d in env.app_notifications.docs
              if d.get("source") == "burden"]
    assert len(notifs) == 1
    n = notifs[0]
    assert n["kind"] == "job_budget_alert" and n["level"] == "overrun"
    assert n["tenant_wide"] is True and n["read"] is False
    assert n["link"] == "/jobs" and n["dedupe_key"]
    out = run(JC.evaluate_burden_overrun(TID, job, _profit(95_000, 10_000)))
    assert out["fired_now"] is False
    assert len([d for d in env.app_notifications.docs]) == 1


def test_burden_flag_silent_when_base_already_over_or_under(env):
    job = seed_job(env)
    # base alone already over: the plain budget alert owns that flag
    out = run(JC.evaluate_burden_overrun(TID, job, _profit(110_000, 10_000)))
    assert out["tipped"] is False and out["fired_now"] is False
    # burdened still under estimate: nothing to flag
    out = run(JC.evaluate_burden_overrun(TID, job, _profit(50_000, 10_000)))
    assert out["tipped"] is False
    # no estimate: honest silence
    out = run(JC.evaluate_burden_overrun(TID, job, _profit(95_000, 10_000,
                                                          est=0)))
    assert out["tipped"] is False
    assert env.app_notifications.docs == []


def test_burden_flag_clears_and_can_refire(env):
    job = seed_job(env)
    run(JC.evaluate_burden_overrun(TID, job, _profit(95_000, 10_000)))
    # estimate raised (change order): no longer tipped -> mark clears
    out = run(JC.evaluate_burden_overrun(TID, job, _profit(95_000, 10_000,
                                                          est=200_000)))
    assert out["tipped"] is False
    # costs rise again past the new estimate via burden -> fires again
    out = run(JC.evaluate_burden_overrun(TID, job, _profit(195_000, 10_000,
                                                          est=200_000)))
    assert out["fired_now"] is True
    assert len(env.app_notifications.docs) == 2


def test_add_labor_cost_end_to_end_fires_burden_flag(env, stub_ledger):
    run(jc_router.patch_config_ep(
        jc_router.ConfigIn(default_burden_pct=20.0), USER))
    seed_job(env)  # estimate $1,000 labor
    doc = run(jobs_router.add_cost("job-1", jobs_router.CostBody(
        category="labor", hours=17, rate=50.0, technician_id=TECH), USER))
    # base $850 fits; burdened $1,020 tips it over
    assert doc["burdened_cents"] == 102_000
    assert doc["burden_overrun"]["fired_now"] is True
    assert len(env.app_notifications.docs) == 1


# ================== timesheet visibility ==================

def test_timesheet_preview_shows_burdened_figure(env):
    env.users.docs.append({
        "id": TECH, "tenant_id": TID, "name": "Tech One",
        "pay_settings": {"hourly_rate_cents": 5_000}})
    run(jc_router.patch_config_ep(
        jc_router.ConfigIn(default_burden_pct=50.0), USER))
    entry = {"id": "e1", "user_id": TECH, "hours": 4.0}
    out = run(time_router._with_labor_costs(USER, [entry]))
    lc = out[0]["labor_cost"]
    assert lc["base_cents"] == 20_000
    assert lc["burden_cents"] == 10_000
    assert lc["burdened_cents"] == 30_000


def test_timesheet_preview_absent_when_unconfigured(env):
    env.users.docs.append({
        "id": TECH, "tenant_id": TID, "name": "Tech One",
        "pay_settings": {"hourly_rate_cents": 5_000}})
    entry = {"id": "e1", "user_id": TECH, "hours": 4.0}
    out = run(time_router._with_labor_costs(USER, [entry]))
    assert "labor_cost" not in out[0]  # exactly today's response


# ================== view option ==================

def test_view_option_defaults_base_and_persists(env):
    assert run(JC.get_config(TID))["labor_view"] == "base"
    run(jc_router.patch_config_ep(jc_router.ConfigIn(labor_view="burdened"),
                                  USER))
    assert run(JC.get_config(TID))["labor_view"] == "burdened"
    run(jc_router.patch_config_ep(jc_router.ConfigIn(labor_view="both"),
                                  USER))
    assert run(JC.get_config(TID))["labor_view"] == "both"
