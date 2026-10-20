"""Payroll flow tests: neutral role labels (keys stable), employer FICA match
posting, and the full draft->approve->post flow — updated for the payroll
TRACKING engine (real Pub 15-T gross-to-net; see test_payroll_tracking.py for
calculation goldens).

No live MongoDB needed (fully mocked). Run from the backend directory with:
    python -m pytest tests/test_payroll_polish.py

What this covers:
  - TEAM_ROLES keeps the structural "barber" key (auth/niches/routers depend on
    it); the neutral "Staff" label lives in the frontend (roles.barber i18n key)
    and is not asserted here — role keys must simply pass through unchanged.
  - employer FICA match (7.65%) is computed per item, summed into
    totals["employer_tax_cents"], and posted Dr 6120 / Cr 2120 together with
    the accrued FUTA (0.6% net of credit on the first $7,000).
  - the disclaimer states the tracking scope plainly: calculates and tracks,
    but does not pay employees, deposit taxes, or file returns.
  - end-to-end: draft (hourly pre-filled from approved time entries + salary),
    approve locks it, post writes a balanced journal entry.
"""
import asyncio
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from routers import payroll
from routers.payroll import RunIn, RunItemIn, _build_item, _totals

# Dedicated loop: asyncio.run() would unset the thread's current event loop and
# break legacy asyncio.get_event_loop() users in other test modules.
_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


# ---------------- in-memory fakes ----------------

class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, length=None):
        docs = self._docs[:length] if length else list(self._docs)
        return [dict(d) for d in docs]


class _FakeColl:
    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    @staticmethod
    def _matches(doc, q):
        for k, v in (q or {}).items():
            if isinstance(v, dict):
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
                if "$ne" in v and doc.get(k) == v["$ne"]:
                    return False
                if "$gte" in v and not (doc.get(k) is not None and doc[k] >= v["$gte"]):
                    return False
                if "$lte" in v and not (doc.get(k) is not None and doc[k] <= v["$lte"]):
                    return False
            elif doc.get(k) != v:
                return False
        return True

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if self._matches(d, q):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([d for d in self.docs if self._matches(d, q)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update):
        for d in self.docs:
            if self._matches(d, q):
                for k, v in update.get("$set", {}).items():
                    d[k] = v
                return

    async def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if self._matches(d, q):
                del self.docs[i]
                return


class _FakeLedger:
    """Stands in for lib.ledger: balances journal drafts like the real
    _validate_lines and returns a posted entry stub."""

    def __init__(self):
        self.audits = []
        self.posted = []

    async def audit(self, db, tenant_id, user, action, detail=None):
        self.audits.append((action, detail))

    async def post_journal_entry(self, db, tenant_id, user, draft, status="posted"):
        lines = draft["lines"]
        dr = sum(l.get("debit", 0) or 0 for l in lines)
        cr = sum(l.get("credit", 0) or 0 for l in lines)
        assert dr == cr and dr > 0, f"unbalanced JE: dr={dr} cr={cr}"
        self.posted.append(draft)
        return {"id": "je-test-1", "number": "JE-TEST-1", "status": status, "lines": lines}


def _setup_db():
    users = _FakeColl([
        {"id": "owner1", "tenant_id": "t1", "name": "Owner", "email": "o@x.com",
         "role": "owner", "commission_rate": 0, "pay_settings": {}},
        {"id": "u1", "tenant_id": "t1", "name": "Alex", "email": "a@x.com",
         "role": "barber", "commission_rate": 0,
         "pay_settings": {"hourly_rate_cents": 2500}},
        {"id": "u2", "tenant_id": "t1", "name": "Sam", "email": "s@x.com",
         "role": "barber", "commission_rate": 0,
         "pay_settings": {"salary_monthly_cents": 400000}},
    ])
    entries = _FakeColl([
        {"id": "e1", "tenant_id": "t1", "user_id": "u1", "date": "2026-10-05",
         "hours": 8.0, "status": "approved", "billable": True, "job_id": None},
    ])
    return SimpleNamespace(
        users=users,
        time_entries=entries,
        pay_schedules=_FakeColl(),
        payroll_runs=_FakeColl(),
        payroll_settings=_FakeColl(),
        payroll_liabilities=_FakeColl(),
        payroll_pay_records=_FakeColl(),
        accounts=_FakeColl(),
        tenants=_FakeColl([{"id": "t1", "tenant_id": "t1", "name": "Test Shop"}]),
    )


@contextmanager
def _env():
    fake_db = _setup_db()
    fake_L = _FakeLedger()
    with patch.object(payroll, "db", fake_db), patch.object(payroll, "L", fake_L):
        yield fake_db, fake_L


def _user():
    return {"id": "owner1", "tenant_id": "t1", "role": "owner", "email": "o@x.com"}


# ---------------- role keys stay structural; labels are frontend ----------------

def test_team_roles_keys_stable():
    # "barber" is structural across auth/niches/routers — it must NOT be renamed.
    # The neutral "Staff" label is rendered by the frontend via roles.barber.
    assert payroll.TEAM_ROLES == ["owner", "barber"]


def test_list_team_passes_role_keys_through():
    with _env():
        team = _run(payroll.list_team(_user()))
    roles = {m["id"]: m["role"] for m in team}
    assert roles == {"owner1": "owner", "u1": "barber", "u2": "barber"}


# ---------------- pay math: hourly / salary / employer FICA match ----------------

def test_build_item_hourly_math_with_employer_match():
    item = _build_item(None, "monthly", RunItemIn(hours=8.0, rate=25.0), 0)
    assert item["gross_cents"] == 20000          # 8h x $25
    assert item["employer_fica_cents"] == 1530   # 7.65% of 20000
    fica = sum(d["amount_cents"] for d in item["deductions"]
               if d["label"].startswith("FICA"))
    assert fica == 1530                          # SS 1240 + Medicare 290
    # Pub 15-T: $200/mo annualizes to $2,400 — below any withholding.
    assert item["federal_withholding_cents"] == 0
    assert item["net_cents"] == 20000 - 1530


def test_build_item_salary_math_with_employer_match():
    member = {"salary_monthly_cents": 400000, "hourly_rate_cents": 0,
              "fed_withholding_pct": 10}
    item = _build_item(member, "monthly", RunItemIn(), 0)
    assert item["gross_cents"] == 400000
    assert item["employer_fica_cents"] == 30600  # 7.65% of 400000
    fed = next(d for d in item["deductions"] if d["label"].startswith("Federal"))
    assert fed["amount_cents"] == 40000
    assert item["net_cents"] == 400000 - 40000 - 30600


def test_totals_include_employer_tax():
    a = _build_item(None, "monthly", RunItemIn(hours=8.0, rate=25.0), 0)
    b = _build_item({"salary_monthly_cents": 400000, "hourly_rate_cents": 0}, "monthly",
                    RunItemIn(), 0)
    t = _totals([a, b])
    assert t["gross_cents"] == 420000
    # employee taxes: a FICA 1530 (+ $0 fed); b FICA 30600 + Pub 15-T fed
    # withholding 29833 ($48,000/yr single: (5800 + 22% x 11,500)/12 = $298.33).
    assert t["tax_deductions_cents"] == 1530 + 30600 + 29833
    assert t["employer_tax_cents"] == 1530 + 30600     # employer FICA match
    assert t["employer_futa_cents"] == 120 + 2400      # 0.6% net FUTA, first $7k
    assert t["net_cents"] == a["net_cents"] + b["net_cents"]


# ---------------- disclaimers stay honest ----------------

def test_disclaimer_states_tracking_scope_plainly():
    d = payroll.DISCLAIMER
    assert "does not pay employees" in d
    assert "does not deposit taxes" in d
    assert "does not file returns" in d
    assert "calculates real withholding" in d


# ---------------- account bootstrap ----------------

def test_ensure_payroll_accounts_creates_6120():
    with _env() as (fake_db, _):
        _run(payroll._ensure_payroll_accounts(_user()))
    codes = {d["code"] for d in fake_db.accounts.docs}
    assert {"6110", "6120", "2120"} <= codes
    acct = next(d for d in fake_db.accounts.docs if d["code"] == "6120")
    assert acct["name"] == "Payroll Tax Expense"
    assert acct["type"] == "expense" and acct["normal_balance"] == "debit"


# ---------------- end-to-end: draft -> approve -> post ----------------

def _draft_run():
    body = RunIn(period_start="2026-10-01", period_end="2026-10-31",
                 items=[RunItemIn(user_id="u1"), RunItemIn(user_id="u2")])
    return _run(payroll.create_run(body, _user()))


def test_e2e_draft_computes_gross_net_and_time_entry_prefill():
    with _env():
        doc = _draft_run()
    assert doc["status"] == "draft"
    assert payroll.DISCLAIMER in doc["disclaimer"] or doc["disclaimer"] == payroll.DISCLAIMER

    by_id = {i["user_id"]: i for i in doc["items"]}
    hourly, salary = by_id["u1"], by_id["u2"]

    # hourly member: pre-filled from approved time entries (PR #123 wiring)
    assert hourly["hours"] == 8.0
    assert hourly["hours_source"] == "time_entries"
    assert hourly["time_entry_ids"] == ["e1"]
    assert hourly["gross_cents"] == 20000
    assert hourly["net_cents"] == 20000 - 1530

    # salary member: monthly salary; real Pub 15-T withholding now applies
    # ($4,000/mo single -> $298.33/mo federal) on top of FICA $306.00.
    assert salary["gross_cents"] == 400000
    assert salary["federal_withholding_cents"] == 29833
    assert salary["net_cents"] == 400000 - 30600 - 29833

    t = doc["totals"]
    assert t["gross_cents"] == 420000
    assert t["employer_tax_cents"] == 32130
    assert t["net_cents"] == 18470 + 339567


def test_e2e_approve_locks_then_post_writes_balanced_je():
    with _env() as (fake_db, fake_L):
        doc = _draft_run()
        rid = doc["id"]

        posted = _run(payroll.approve_run(rid, _user()))
        assert posted["status"] == "approved"

        with pytest.raises(HTTPException) as exc:   # approve twice -> locked
            _run(payroll.approve_run(rid, _user()))
        assert exc.value.status_code == 409

        done = _run(payroll.post_run(rid, _user()))
        assert done["status"] == "posted"
        assert done["entry_number"] == "JE-TEST-1"

    assert len(fake_L.posted) == 1
    lines = fake_L.posted[0]["lines"]

    def amt(code, side):
        return sum(l.get(side, 0) for l in lines if l["account_code"] == code)

    assert amt("6110", "debit") == 420000            # gross wages
    # employer taxes: FICA match 32130 + FUTA accrual 2520 (0.6% net on the
    # run's gross, all under the $7,000 FUTA wage base).
    assert amt("6120", "debit") == 32130 + 2520
    # employee taxes withheld: FICA 32130 + federal withholding 29833.
    assert amt("2120", "credit") == (32130 + 29833) + (32130 + 2520)
    assert amt("2320", "credit") == 358037           # net pay owed
    memos = [l.get("memo", "") for l in lines]
    assert any("FICA/FUTA/SUTA" in m for m in memos)


def test_e2e_post_requires_approve():
    with _env():
        rid = _draft_run()["id"]
        with pytest.raises(HTTPException) as exc:
            _run(payroll.post_run(rid, _user()))
        assert exc.value.status_code == 409


def test_e2e_posted_run_cannot_be_deleted():
    with _env() as (fake_db, _):
        rid = _draft_run()["id"]
        _run(payroll.approve_run(rid, _user()))
        _run(payroll.post_run(rid, _user()))
        with pytest.raises(HTTPException) as exc:
            _run(payroll.delete_run(rid, _user()))
        assert exc.value.status_code == 409
        assert fake_db.payroll_runs.docs  # still there


def test_e2e_payslip_carries_disclaimer():
    with _env():
        doc = _draft_run()
        rid = doc["id"]
        item_id = doc["items"][0]["id"]
        slip = _run(payroll.get_payslip(rid, item_id, _user()))
    assert "does not pay employees" in slip["disclaimer"]
    assert "does not file returns" in slip["disclaimer"]
    assert slip["employee"]["name"] == "Alex"
    assert slip["gross_cents"] == 20000
