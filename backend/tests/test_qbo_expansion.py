"""QBO-Plus expansion: Banking, Vendors/Bills, Expenses, Estimates, Invoices (partial/overpay),
Customer Statements, Reports (Cash Flow, Budget vs Actual, Income by Customer, Expenses by Vendor),
and ACCOUNTING INTEGRITY (trial balance balanced, no duplicate postings)."""
import os
import pytest
import requests

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or
            open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].split()[0]).rstrip("/")
API = f"{BASE_URL}/api"

OWNER = {"email": "onelovellc@outlook.com", "password": "Owner123!"}


def mk_session(creds):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json=creds, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def owner():
    return mk_session(OWNER)


def tb(sess):
    r = sess.get(f"{API}/ledger/trial-balance", timeout=15)
    assert r.status_code == 200
    return r.json()


def je_count(sess):
    r = sess.get(f"{API}/ledger/journal-entries", params={"limit": 10000}, timeout=15)
    assert r.status_code == 200
    return len(r.json())


# ---------------- Banking ----------------
class TestBanking:
    def test_seeded_bank_accounts(self, owner):
        r = owner.get(f"{API}/banking/accounts", timeout=15)
        assert r.status_code == 200
        accs = r.json()
        names = {a["name"] for a in accs}
        assert "Operating Checking" in names
        assert "Fuel & Supplies Card" in names
        for a in accs:
            assert "gl_balance" in a
            assert "unreconciled_count" in a

    def test_import_csv_with_duplicates(self, owner):
        accs = owner.get(f"{API}/banking/accounts").json()
        checking = next(a for a in accs if a["name"] == "Operating Checking")
        csv_text = (
            "Date,Description,Amount\n"
            "2026-01-05,Shell Fuel,-45.00\n"
            "2026-01-06,Home Depot,-120.50\n"
            "2026-01-05,Shell Fuel,-45.00\n"  # dup
            "2026-01-07,Deposit from customer,500.00\n"
        )
        r = owner.post(f"{API}/banking/import",
                       json={"bank_account_id": checking["id"], "csv_text": csv_text}, timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] == 4
        assert d["imported"] == 3
        assert d["duplicates"] == 1

    def test_categorize_posts_balanced_entry(self, owner):
        accs = owner.get(f"{API}/banking/accounts").json()
        checking = next(a for a in accs if a["name"] == "Operating Checking")
        before_tb = tb(owner)
        before_je = je_count(owner)
        txns = owner.get(f"{API}/banking/transactions",
                         params={"bank_account_id": checking["id"], "status": "unmatched"}).json()
        assert len(txns) >= 1
        t = txns[0]
        r = owner.post(f"{API}/banking/transactions/{t['id']}/categorize",
                       json={"category_code": "5700"}, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cleared"
        # exactly one new entry
        assert je_count(owner) == before_je + 1
        # TB stays balanced
        after = tb(owner)
        assert after["balanced"] is True
        assert after["total_debit"] == after["total_credit"]

    def test_create_rule(self, owner):
        r = owner.post(f"{API}/banking/rules",
                       json={"contains": "Shell", "category_code": "5700"}, timeout=15)
        assert r.status_code == 200
        assert r.json()["category_code"] == "5700"

    def test_transfer_between_accounts_balanced(self, owner):
        accs = owner.get(f"{API}/banking/accounts").json()
        checking = next(a for a in accs if a["name"] == "Operating Checking")
        card = next(a for a in accs if a["name"] == "Fuel & Supplies Card")
        before_je = je_count(owner)
        r = owner.post(f"{API}/banking/transfer",
                       json={"from_account_id": checking["id"], "to_account_id": card["id"],
                             "amount": 100.0, "memo": "TEST transfer"}, timeout=15)
        assert r.status_code == 200, r.text
        assert "entry_number" in r.json()
        assert je_count(owner) == before_je + 1
        assert tb(owner)["balanced"] is True

    def test_reconcile(self, owner):
        accs = owner.get(f"{API}/banking/accounts").json()
        checking = next(a for a in accs if a["name"] == "Operating Checking")
        r = owner.post(f"{API}/banking/reconciliations",
                       json={"bank_account_id": checking["id"], "statement_date": "2026-01-31",
                             "statement_ending_balance": 0.0, "txn_ids": []}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["bank_account_id"] == checking["id"]
        assert "status" in d


# ---------------- Vendors / Expenses / Credits / 1099 ----------------
class TestVendorsExpenses:
    def test_create_vendor_1099(self, owner):
        r = owner.post(f"{API}/vendors",
                       json={"name": "TEST_Contractor LLC", "is_1099": True, "tax_id": "12-3456789"},
                       timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["is_1099"] is True
        assert d["tax_id"] == "12-3456789"

    def test_create_expense_posts_balanced(self, owner):
        before_tb = tb(owner)
        before_je = je_count(owner)
        r = owner.post(f"{API}/vendors/expenses",
                       json={"vendor": "TEST_Shell", "category_code": "5700", "amount": 55.25,
                             "payment_method": "bank", "description": "Fuel",
                             "receipt": {"merchant": "Shell", "total": 55.25}}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["amount_cents"] == 5525
        assert d["entry_number"]
        assert je_count(owner) == before_je + 1
        after = tb(owner)
        assert after["balanced"] is True

    def test_create_vendor_credit(self, owner):
        before_je = je_count(owner)
        r = owner.post(f"{API}/vendors/credits",
                       json={"vendor": "TEST_Shell", "amount": 20.0, "memo": "Refund"}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["amount_cents"] == 2000
        assert d["remaining_cents"] == 2000
        assert je_count(owner) == before_je + 1
        assert tb(owner)["balanced"] is True

    def test_1099_report(self, owner):
        r = owner.get(f"{API}/vendors/1099", params={"year": "2026"}, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["year"] == "2026"
        assert isinstance(d["vendors"], list)
        # seeded 1099 vendors should appear
        names = {v["name"] for v in d["vendors"]}
        assert "Ferguson HVAC Supply" in names


# ---------------- Bills: partial pay & recurring ----------------
class TestBills:
    @pytest.fixture(scope="class")
    def bill_id(self, owner):
        r = owner.post(f"{API}/bills",
                       json={"vendor": "TEST_Vendor_B", "amount": 300.0, "expense_code": "5700",
                             "recurring": True, "frequency": "monthly"}, timeout=15)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def test_partial_payment(self, owner, bill_id):
        before_je = je_count(owner)
        r = owner.post(f"{API}/bills/{bill_id}/pay",
                       json={"payment_method": "bank", "amount": 100.0}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "partial"
        assert d["paid_cents"] == 10000
        assert d["balance_cents"] == 20000
        assert je_count(owner) == before_je + 1
        assert tb(owner)["balanced"] is True

    def test_remainder_pay(self, owner, bill_id):
        r = owner.post(f"{API}/bills/{bill_id}/pay",
                       json={"payment_method": "bank", "amount": 200.0}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "paid"
        assert d["balance_cents"] == 0
        assert tb(owner)["balanced"] is True

    def test_cannot_overpay_bill(self, owner, bill_id):
        r = owner.post(f"{API}/bills/{bill_id}/pay",
                       json={"payment_method": "bank", "amount": 10.0}, timeout=15)
        assert r.status_code == 409

    def test_generate_recurring(self, owner, bill_id):
        before_je = je_count(owner)
        r = owner.post(f"{API}/bills/{bill_id}/generate-next", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["vendor"] == "TEST_Vendor_B"
        assert d["status"] == "open"
        assert je_count(owner) == before_je + 1
        assert tb(owner)["balanced"] is True


# ---------------- Invoices: partial, overpay -> customer credit ----------------
class TestInvoices:
    def test_partial_and_overpay(self, owner):
        r = owner.post(f"{API}/invoices",
                       json={"client_name": "TEST_Overpay Co",
                             "lines": [{"kind": "service", "name": "Install",
                                        "revenue_code": "4020", "amount": 200.0}],
                             "tax": 0}, timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        inv_id = inv["id"]

        # Partial payment: $50
        r = owner.post(f"{API}/invoices/{inv_id}/pay",
                       json={"payment_method": "bank", "amount": 50.0}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "partial"
        assert d["paid_cents"] == 5000
        assert d["balance_cents"] == 15000
        assert tb(owner)["balanced"] is True

        # Overpayment: pay $200 (balance was $150) -> expect $50 overpayment -> customer credit
        credits_before = owner.get(f"{API}/invoices/customer-credits").json()
        before_count = len(credits_before)
        r = owner.post(f"{API}/invoices/{inv_id}/pay",
                       json={"payment_method": "bank", "amount": 200.0}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "paid"
        assert d["overpayment_cents"] == 5000, f"expected $50 overpay, got {d}"
        credits_after = owner.get(f"{API}/invoices/customer-credits").json()
        assert len(credits_after) == before_count + 1
        new_credit = next(c for c in credits_after if c["client_name"] == "TEST_Overpay Co")
        assert new_credit["remaining_cents"] == 5000
        assert tb(owner)["balanced"] is True


# ---------------- Estimates: convert + recurring ----------------
class TestEstimates:
    def test_create_send_convert(self, owner):
        r = owner.post(f"{API}/estimates",
                       json={"client_name": "TEST_Estimate Co",
                             "lines": [{"kind": "service", "name": "Quote",
                                        "revenue_code": "4020", "amount": 500.0}]}, timeout=15)
        assert r.status_code == 200, r.text
        est = r.json()
        est_id = est["id"]
        assert est["status"] == "draft"
        assert est["total_cents"] == 50000

        # mark sent, accepted
        for st in ("sent", "accepted"):
            r = owner.patch(f"{API}/estimates/{est_id}", json={"status": st}, timeout=15)
            assert r.status_code == 200
            assert r.json()["status"] == st

        # convert
        before_je = je_count(owner)
        r = owner.post(f"{API}/estimates/{est_id}/convert", timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["status"] == "issued"
        assert inv["total_cents"] == 50000
        assert je_count(owner) == before_je + 1
        # cannot double convert
        r2 = owner.post(f"{API}/estimates/{est_id}/convert", timeout=15)
        assert r2.status_code == 409
        assert tb(owner)["balanced"] is True

    def test_recurring_template(self, owner):
        r = owner.post(f"{API}/estimates/recurring",
                       json={"client_name": "TEST_Monthly Service",
                             "lines": [{"kind": "service", "name": "Maintenance",
                                        "revenue_code": "4020", "amount": 150.0}],
                             "frequency": "monthly"}, timeout=15)
        assert r.status_code == 200, r.text
        rec_id = r.json()["id"]
        before_je = je_count(owner)
        r = owner.post(f"{API}/estimates/recurring/{rec_id}/generate", timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["total_cents"] == 15000
        assert je_count(owner) == before_je + 1
        assert tb(owner)["balanced"] is True


# ---------------- Customer statement ----------------
class TestCustomerStatement:
    def test_statement_renders(self, owner):
        customers = owner.get(f"{API}/customers").json()
        if not customers:
            pytest.skip("no customers seeded")
        cid = customers[0]["id"]
        r = owner.get(f"{API}/customers/{cid}/statement", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "events" in d
        assert "outstanding" in d
        assert "outstanding_cents" in d


# ---------------- Reports ----------------
class TestReports:
    def test_cash_flow(self, owner):
        r = owner.get(f"{API}/reports/cash-flow", timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("operating", "investing", "financing", "opening", "closing", "net_change"):
            assert k in d

    def test_budget_vs_actual(self, owner):
        r = owner.get(f"{API}/reports/budget-vs-actual", params={"year": "2026"}, timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["year"] == "2026"
        assert len(d["rows"]) >= 1
        row = d["rows"][0]
        for k in ("code", "budget", "actual", "variance"):
            assert k in row

    def test_income_by_customer(self, owner):
        r = owner.get(f"{API}/reports/income-by-customer", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["rows"], list)

    def test_expenses_by_vendor(self, owner):
        r = owner.get(f"{API}/reports/expenses-by-vendor", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["rows"], list)

    def test_csv_exports(self, owner):
        for ep in ("cash-flow.csv", "income-by-customer.csv", "expenses-by-vendor.csv"):
            r = owner.get(f"{API}/reports/{ep}", timeout=15)
            assert r.status_code == 200, f"{ep}: {r.status_code}"
            assert "text/csv" in r.headers.get("content-type", "")
        r = owner.get(f"{API}/reports/budget-vs-actual.csv", params={"year": "2026"}, timeout=15)
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")


# ---------------- Final integrity check ----------------
class TestIntegrity:
    def test_trial_balance_final(self, owner):
        d = tb(owner)
        assert d["balanced"] is True
        assert d["total_debit"] == d["total_credit"]

    def test_no_duplicate_journal_entries(self, owner):
        r = owner.get(f"{API}/ledger/journal-entries", params={"limit": 10000}).json()
        nums = [e.get("number") for e in r if e.get("number")]
        assert len(nums) == len(set(nums)), "Duplicate journal entry numbers found"

    def test_each_entry_balanced(self, owner):
        r = owner.get(f"{API}/ledger/journal-entries", params={"limit": 10000}).json()
        for e in r[:200]:
            if e.get("status") != "posted":
                continue
            det = owner.get(f"{API}/ledger/journal-entries/{e['id']}").json()
            lines = det.get("lines", [])
            dr = sum(l.get("debit", 0) for l in lines)
            cr = sum(l.get("credit", 0) for l in lines)
            assert dr == cr, f"Entry {e.get('number')} unbalanced: dr={dr} cr={cr}"
