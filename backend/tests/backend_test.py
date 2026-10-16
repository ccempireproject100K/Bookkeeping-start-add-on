"""Backend API tests for The Barber's Ledger."""
import os
import pytest
import requests
from datetime import datetime, timezone

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
API = f"{BASE_URL}/api"

OWNER = {"email": "onelovellc@outlook.com", "password": "Owner123!"}
ACCT = {"email": "accountant@fadeco.com", "password": "Books123!"}
BOOK = {"email": "bookkeeper@fadeco.com", "password": "Books123!"}
BARBER = {"email": "marcus@fadeco.com", "password": "Staff123!"}
SHARP = {"email": "owner@sharplines.com", "password": "Owner123!"}


def mk_session(creds):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json=creds, timeout=15)
    assert r.status_code == 200, f"login failed {creds['email']} -> {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def owner():
    return mk_session(OWNER)


@pytest.fixture(scope="module")
def accountant():
    return mk_session(ACCT)


@pytest.fixture(scope="module")
def bookkeeper():
    return mk_session(BOOK)


@pytest.fixture(scope="module")
def barber():
    return mk_session(BARBER)


@pytest.fixture(scope="module")
def sharp_owner():
    return mk_session(SHARP)


# ---------------- Auth ----------------
class TestAuth:
    def test_health(self):
        r = requests.get(f"{API}/health", timeout=15)
        assert r.status_code == 200
        assert r.json().get("db") == "ok"

    def test_login_bad(self):
        r = requests.post(f"{API}/auth/login", json={"email": OWNER["email"], "password": "wrong"}, timeout=15)
        assert r.status_code == 401

    def test_me(self, owner):
        r = owner.get(f"{API}/auth/me", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == OWNER["email"]
        assert data["role"] == "owner"
        assert data.get("tenant")

    def test_team(self, owner):
        r = owner.get(f"{API}/auth/team", timeout=15)
        assert r.status_code == 200
        emails = [u["email"] for u in r.json()]
        assert OWNER["email"] in emails
        assert BARBER["email"] in emails

    def test_switch_role(self, owner):
        # Note: switches session on this cookie jar; use a dedicated session
        s = mk_session(OWNER)
        r = s.post(f"{API}/auth/switch-role", json={"role": "bookkeeper"}, timeout=15)
        assert r.status_code == 200
        assert r.json()["role"] == "bookkeeper"


# ---------------- Dashboard ----------------
class TestDashboard:
    def test_dashboard(self, owner):
        r = owner.get(f"{API}/dashboard", timeout=15)
        assert r.status_code == 200
        data = r.json()
        for k in ("cash", "revenue", "expenses", "net_income", "ar", "ap", "inventory"):
            assert k in data["tiles"]
        assert "recent_entries" in data


# ---------------- Chart of Accounts ----------------
class TestCOA:
    def test_list_accounts(self, owner):
        r = owner.get(f"{API}/ledger/accounts", timeout=15)
        assert r.status_code == 200
        accs = r.json()
        assert len(accs) > 10
        types = {a["type"] for a in accs}
        assert {"asset", "liability", "equity", "revenue", "expense"} <= types

    def test_create_and_archive_custom_account(self, owner):
        import random
        code = f"8{random.randint(100,999)}"
        r = owner.post(f"{API}/ledger/accounts", json={"code": code, "name": "TEST_Custom", "type": "expense"}, timeout=15)
        assert r.status_code == 200
        # archive
        r2 = owner.patch(f"{API}/ledger/accounts/{code}", json={"archived": True}, timeout=15)
        assert r2.status_code == 200
        assert r2.json()["archived"] is True


# ---------------- Journal Entries ----------------
class TestJournal:
    def test_unbalanced_rejected(self, owner):
        r = owner.post(f"{API}/ledger/journal-entries", json={
            "memo": "TEST unbalanced",
            "lines": [{"account_code": "1010", "debit": 100}, {"account_code": "4010", "credit": 50}],
            "post": True,
        }, timeout=15)
        assert r.status_code in (400, 422)

    def test_post_and_reverse(self, owner):
        r = owner.post(f"{API}/ledger/journal-entries", json={
            "memo": "TEST entry",
            "lines": [{"account_code": "1010", "debit": 100},
                      {"account_code": "4010", "credit": 100}],
            "post": True,
        }, timeout=15)
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["status"] == "posted"
        assert entry.get("number")
        eid = entry["id"]
        r2 = owner.post(f"{API}/ledger/journal-entries/{eid}/reverse", timeout=15)
        assert r2.status_code == 200
        rev = r2.json()
        assert rev["status"] == "posted"
        # original reversed
        r3 = owner.get(f"{API}/ledger/journal-entries/{eid}", timeout=15)
        assert r3.json()["status"] == "reversed"

    def test_bookkeeper_cannot_post(self, bookkeeper):
        r = bookkeeper.post(f"{API}/ledger/journal-entries", json={
            "memo": "TEST bookkeeper",
            "lines": [{"account_code": "1010", "debit": 10},
                      {"account_code": "4010", "credit": 10}],
            "post": True,
        }, timeout=15)
        # Should be allowed but status forced to draft
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "draft"

    def test_accountant_cannot_create(self, accountant):
        r = accountant.post(f"{API}/ledger/journal-entries", json={
            "memo": "TEST acct",
            "lines": [{"account_code": "1010", "debit": 10}, {"account_code": "4010", "credit": 10}],
            "post": False,
        }, timeout=15)
        assert r.status_code == 403


# ---------------- Reports ----------------
class TestReports:
    def test_trial_balance(self, owner):
        r = owner.get(f"{API}/ledger/trial-balance", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["balanced"] is True
        assert d["total_debit"] == d["total_credit"]

    def test_balance_sheet(self, owner):
        r = owner.get(f"{API}/ledger/balance-sheet", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["balanced"] is True
        assert d["assets_total"] == d["liabilities_equity_total"]

    def test_pnl(self, owner):
        r = owner.get(f"{API}/ledger/pnl", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "revenue_total" in d and "expense_total" in d and "net_income" in d

    def test_general_ledger(self, owner):
        r = owner.get(f"{API}/ledger/general-ledger", params={"account": "1010"}, timeout=15)
        assert r.status_code == 200


# ---------------- POS ----------------
class TestPOS:
    def test_sell_service(self, owner):
        # Find a barber
        team = owner.get(f"{API}/auth/team").json()
        barber = next(u for u in team if u["role"] == "barber")
        r = owner.post(f"{API}/pos/sell", json={
            "barber_id": barber["id"], "payment_method": "cash", "discount": 0,
            "product_items": [],
            "service_items": [{"service_type": "haircut", "name": "Haircut", "price": 30.0}],
        }, timeout=15)
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["total_cents"] >= 3000
        assert s["commission_cents"] > 0

    def test_sell_product_out_of_stock(self, owner):
        prods = owner.get(f"{API}/products").json()
        if not prods:
            pytest.skip("no products")
        p = prods[0]
        r = owner.post(f"{API}/pos/sell", json={
            "product_items": [{"product_id": p["id"], "qty": 999999}],
            "service_items": [],
        }, timeout=15)
        assert r.status_code == 409

    def test_barber_can_sell(self, barber):
        r = barber.post(f"{API}/pos/sell", json={
            "service_items": [{"service_type": "haircut", "name": "Haircut", "price": 25.0}],
        }, timeout=15)
        assert r.status_code == 200


# ---------------- Products ----------------
class TestProducts:
    def test_create_and_restock_adjust(self, owner):
        r = owner.post(f"{API}/products", json={
            "name": "TEST_Pomade", "type": "retail", "revenue_code": "4100",
            "price": 20, "unit_cost": 8, "quantity_on_hand": 0,
        }, timeout=15)
        assert r.status_code == 200
        pid = r.json()["id"]
        r2 = owner.post(f"{API}/products/{pid}/restock", json={"quantity": 10, "unit_cost": 8}, timeout=15)
        assert r2.status_code == 200
        assert r2.json()["quantity_on_hand"] == 10
        r3 = owner.post(f"{API}/products/{pid}/adjust", json={"quantity_delta": -1, "reason": "shrinkage"}, timeout=15)
        assert r3.status_code == 200
        assert r3.json()["quantity_on_hand"] == 9


# ---------------- Invoices ----------------
class TestInvoices:
    def test_issue_and_pay(self, owner):
        r = owner.post(f"{API}/invoices", json={
            "client_name": "TEST_Client",
            "lines": [{"kind": "service", "name": "Event cut", "revenue_code": "4020", "amount": 150}],
            "tax": 0,
        }, timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["status"] == "issued"
        r2 = owner.post(f"{API}/invoices/{inv['id']}/pay", json={"payment_method": "bank"}, timeout=15)
        assert r2.status_code == 200
        assert r2.json()["status"] == "paid"


# ---------------- Cash Close ----------------
class TestCashClose:
    def test_preview_and_close(self, owner):
        r = owner.get(f"{API}/cash-close/preview", timeout=15)
        assert r.status_code == 200
        pre = r.json()
        r2 = owner.post(f"{API}/cash-close", json={
            "opening_float": 0, "counted_cash": pre["cash_sales_cents"] / 100.0 + 1.0,
            "paid_in": 0, "paid_out": 0, "notes": "TEST close",
        }, timeout=15)
        assert r2.status_code == 200
        d = r2.json()
        assert d["variance_cents"] != 0


# ---------------- Fiscal Periods / lock -> 423 ----------------
class TestPeriods:
    def test_lock_prevents_posting(self, owner):
        # pick a year/month far in the past unlikely to collide
        key = "2000-01"
        r = owner.post(f"{API}/ledger/periods/{key}/status", json={"status": "locked"}, timeout=15)
        assert r.status_code == 200
        # try posting in locked period
        r2 = owner.post(f"{API}/ledger/journal-entries", json={
            "entry_date": "2000-01-15", "memo": "TEST locked",
            "lines": [{"account_code": "1010", "debit": 5}, {"account_code": "4010", "credit": 5}],
            "post": True,
        }, timeout=15)
        assert r2.status_code == 423, f"expected 423 got {r2.status_code}: {r2.text}"
        # reopen
        owner.post(f"{API}/ledger/periods/{key}/status", json={"status": "open"}, timeout=15)


# ---------------- Tenant isolation ----------------
class TestIsolation:
    def test_sharp_lines_sees_no_fadeco_data(self, sharp_owner, owner):
        # Fade & Co accounts count
        fade_accts = owner.get(f"{API}/ledger/accounts").json()
        fade_entries = owner.get(f"{API}/ledger/journal-entries").json()
        sharp_entries = sharp_owner.get(f"{API}/ledger/journal-entries").json()
        # No shared entry IDs
        fade_ids = {e["id"] for e in fade_entries}
        sharp_ids = {e["id"] for e in sharp_entries}
        assert fade_ids.isdisjoint(sharp_ids)
        # Sharp Lines team contains only sharp owner
        team = sharp_owner.get(f"{API}/auth/team").json()
        emails = {u["email"] for u in team}
        assert OWNER["email"] not in emails
