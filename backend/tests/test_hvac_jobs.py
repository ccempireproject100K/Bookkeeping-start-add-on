"""HVAC Phase 1 — Jobs, costs, change orders, invoices, profitability, accounting integrity."""
import os
import pytest
import requests

from dotenv import load_dotenv
load_dotenv("/app/frontend/.env")
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not set"
API = f"{BASE_URL}/api"

OWNER = {"email": "onelovellc@outlook.com", "password": "Owner123!"}
ACCT = {"email": "accountant@fadeco.com", "password": "Books123!"}
BOOK = {"email": "bookkeeper@fadeco.com", "password": "Books123!"}
BARBER = {"email": "marcus@fadeco.com", "password": "Staff123!"}


def mk_sess(creds):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json=creds, timeout=15)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def owner():
    return mk_sess(OWNER)


@pytest.fixture(scope="module")
def accountant():
    return mk_sess(ACCT)


@pytest.fixture(scope="module")
def bookkeeper():
    return mk_sess(BOOK)


@pytest.fixture(scope="module")
def barber():
    return mk_sess(BARBER)


def _je_count(sess):
    r = sess.get(f"{API}/ledger/journal-entries?limit=10000", timeout=15)
    assert r.status_code == 200, r.text
    return len(r.json())


def _trial_balance_balanced(sess):
    r = sess.get(f"{API}/ledger/trial-balance", timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    td = j.get("total_debit", j.get("total_debit_cents"))
    tc = j.get("total_credit", j.get("total_credit_cents"))
    return td == tc, td, tc


# ---------------- Seeded jobs ----------------
class TestSeededJobs:
    def test_list(self, owner):
        r = owner.get(f"{API}/jobs", timeout=15)
        assert r.status_code == 200
        js = r.json()
        nums = {j["job_number"]: j for j in js}
        for jn in ("JOB-0001", "JOB-0002", "JOB-0003"):
            assert jn in nums, f"missing {jn}"
        assert nums["JOB-0001"]["job_type"] == "installation"
        assert nums["JOB-0002"]["job_type"] == "repair"
        assert nums["JOB-0003"]["job_type"] == "maintenance"

    def test_job1_profit_spec(self, owner):
        r = owner.get(f"{API}/jobs", timeout=15)
        j1 = next(j for j in r.json() if j["job_number"] == "JOB-0001")
        p = j1["profit"]
        assert p["revenue"] == 8000.0, p
        assert p["cost"] == 5500.0, p
        assert p["gross_profit"] == 2500.0, p
        assert p["gross_margin_pct"] == 31.25, p
        assert p["is_unprofitable"] is False

    def test_job3_unprofitable(self, owner):
        r = owner.get(f"{API}/jobs", timeout=15)
        j3 = next(j for j in r.json() if j["job_number"] == "JOB-0003")
        assert j3["profit"]["is_unprofitable"] is True
        assert j3["profit"]["gross_profit_cents"] < 0


# ---------------- Full roundtrip ----------------
class TestJobFlow:
    def test_full_flow(self, owner):
        tb0_ok, td0, tc0 = _trial_balance_balanced(owner)
        assert tb0_ok, f"TB unbalanced at start {td0} vs {tc0}"
        je0 = _je_count(owner)

        # Create estimate
        payload = {
            "customer_name": "TEST_HVAC_Client",
            "service_location": "123 Test Ln",
            "job_type": "installation",
            "estimated_revenue": 10000,
            "estimated_costs": {"material": 3000, "labor": 2000, "equipment": 1000},
            "description": "pytest HVAC",
        }
        r = owner.post(f"{API}/jobs", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        job = r.json()
        assert job["status"] == "estimate"
        jid = job["id"]
        assert job["job_number"].startswith("JOB-")

        # Cost on estimate -> 409
        r = owner.post(f"{API}/jobs/{jid}/costs",
                       json={"category": "labor", "amount": 100, "description": "nope"}, timeout=15)
        assert r.status_code == 409, r.text

        # Convert to active
        r = owner.patch(f"{API}/jobs/{jid}", json={"status": "active"}, timeout=15)
        assert r.status_code == 200, r.text

        # Labor by hours*rate
        r = owner.post(f"{API}/jobs/{jid}/costs",
                       json={"category": "labor", "hours": 4, "rate": 75, "description": "install hrs"},
                       timeout=15)
        assert r.status_code == 200, r.text
        c1 = r.json()
        assert c1["amount_cents"] == 30000, c1
        assert c1.get("entry_number"), c1
        je_after_c1 = _je_count(owner)
        assert je_after_c1 == je0 + 1, "labor cost must produce exactly ONE JE"

        # Material cost by amount
        r = owner.post(f"{API}/jobs/{jid}/costs",
                       json={"category": "material", "amount": 500, "description": "ductwork"}, timeout=15)
        assert r.status_code == 200, r.text
        c2 = r.json()
        assert c2["amount_cents"] == 50000
        je_after_c2 = _je_count(owner)
        assert je_after_c2 == je_after_c1 + 1

        # Verify job updated
        r = owner.get(f"{API}/jobs/{jid}", timeout=15)
        assert r.status_code == 200
        jd = r.json()
        assert jd["profit"]["actual_cost_cents"] == 80000
        assert jd["profit"]["gross_profit_cents"] == -80000  # no revenue yet

        # Change order
        r = owner.post(f"{API}/jobs/{jid}/change-orders",
                       json={"description": "add vents", "revenue_delta": 2000, "cost_delta": 500}, timeout=15)
        assert r.status_code == 200, r.text
        r = owner.get(f"{API}/jobs/{jid}", timeout=15)
        jd = r.json()
        assert jd["profit"]["estimated_revenue_cents"] == 1200000, jd["profit"]
        assert jd["profit"]["estimated_cost_cents"] == 650000, jd["profit"]

        # Invoice (revenue)
        je_before_inv = _je_count(owner)
        r = owner.post(f"{API}/jobs/{jid}/invoice", json={"amount": 10000, "tax": 0}, timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["subtotal_cents"] == 1000000
        assert inv.get("issue_entry"), inv
        je_after_inv = _je_count(owner)
        assert je_after_inv == je_before_inv + 1, "invoice must produce exactly ONE JE"

        # Final profit
        r = owner.get(f"{API}/jobs/{jid}", timeout=15)
        jd = r.json()
        assert jd["profit"]["actual_revenue_cents"] == 1000000
        assert jd["profit"]["gross_profit_cents"] == 1000000 - 80000

        # Books still balanced
        ok, td, tc = _trial_balance_balanced(owner)
        assert ok, f"TB unbalanced after flow: {td} vs {tc}"


# ---------------- Profitability report ----------------
class TestProfitabilityReport:
    @pytest.mark.parametrize("gb", ["job", "customer", "service_type", "technician"])
    def test_groups(self, owner, gb):
        r = owner.get(f"{API}/jobs/reports/profitability", params={"group_by": gb}, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["group_by"] == gb
        assert isinstance(data["items"], list)
        assert len(data["items"]) >= 1


# ---------------- Permissions ----------------
class TestPermissions:
    def test_accountant_can_read(self, accountant):
        r = accountant.get(f"{API}/jobs", timeout=15)
        assert r.status_code == 200
        r = accountant.get(f"{API}/jobs/reports/profitability?group_by=job", timeout=15)
        assert r.status_code == 200

    def test_bookkeeper_can_read(self, bookkeeper):
        r = bookkeeper.get(f"{API}/jobs", timeout=15)
        assert r.status_code == 200

    def test_accountant_cannot_create_job(self, accountant):
        r = accountant.post(f"{API}/jobs",
                            json={"customer_name": "TEST_x", "job_type": "repair"}, timeout=15)
        assert r.status_code in (401, 403), r.status_code

    def test_barber_cannot_create_job(self, barber):
        r = barber.post(f"{API}/jobs",
                        json={"customer_name": "TEST_x", "job_type": "repair"}, timeout=15)
        assert r.status_code in (401, 403), r.status_code


# ---------------- Customers ----------------
class TestCustomers:
    def test_list_and_create(self, owner):
        r = owner.get(f"{API}/customers", timeout=15)
        assert r.status_code == 200
        before = len(r.json())
        r = owner.post(f"{API}/customers", json={
            "name": "TEST_Customer_HVAC",
            "locations": [{"label": "Primary", "address": "999 Pytest Blvd"}]}, timeout=15)
        assert r.status_code == 200, r.text
        c = r.json()
        assert c["name"] == "TEST_Customer_HVAC"
        assert c["locations"][0]["address"] == "999 Pytest Blvd"
        r = owner.get(f"{API}/customers", timeout=15)
        assert len(r.json()) == before + 1


# ---------------- Rebrand ----------------
class TestRebrand:
    def test_tenant_name(self, owner):
        r = owner.get(f"{API}/auth/me", timeout=15)
        assert r.status_code == 200
        me = r.json()
        name = (me.get("tenant") or {}).get("name") or me.get("tenant_name") or ""
        assert "Mind at Ease" in name or "HVAC" in name, me


# ---------------- Accounting integrity (final) ----------------
class TestAccountingIntegrity:
    def test_tb_balanced(self, owner):
        ok, td, tc = _trial_balance_balanced(owner)
        assert ok, f"{td} vs {tc}"

    def test_balance_sheet_balanced(self, owner):
        r = owner.get(f"{API}/ledger/balance-sheet", timeout=15)
        assert r.status_code == 200, r.text
        bs = r.json()
        assert bs.get("balanced") is True, bs
        assert bs["assets_total"] == bs["liabilities_equity_total"], bs
