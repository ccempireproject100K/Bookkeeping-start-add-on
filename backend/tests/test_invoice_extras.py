"""Owner-approved invoice extras (2026-11-14): quote->invoice one flow,
contract+sign+deposit, job photos, early-pay discount, trade benchmark.
All on our own stack — no third-party services anywhere in these paths.
Reuses the FakeDB fixtures from test_invoice_delivery.py.
"""
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from test_invoice_delivery import (env, run, make_customer, make_invoice,  # noqa: F401
                                   stored_invoice, USER, TID)
from lib.base import Scoped

from routers import estimates as EST
from routers import invoices as INV
from routers import invoice_delivery as ID
from routers import invoice_extras as IX
from routers import benchmarks as BM
from lib import invoice_pay as IP


@pytest.fixture()
def xenv(env, monkeypatch, tmp_path):
    for mod in (EST, IX, BM):
        monkeypatch.setattr(mod, "db", env.db)
    monkeypatch.setattr(IX, "UPLOAD_ROOT", tmp_path / "invoice_photos")
    # Production tenants get account 4900 (Discounts & Promos, contra-
    # revenue) from seed_coa/full_coa; the shared test seed is a subset,
    # so add it here for the early-pay ledger lines to post against.
    run(Scoped(env.db.accounts, TID).insert_one(
        {"id": "acct-4900", "code": "4900", "name": "Discounts & Promos",
         "type": "revenue", "normal_balance": "debit", "archived": False}))
    return env


def _journal_lines(env, invoice_id):
    return [l for l in env.db.journal_lines.docs
            if l.get("tenant_id") == TID]


# ---------------- 1. Quote -> invoice one flow ----------------
def test_convert_carries_everything_and_links_both_ways(xenv):
    body = EST.EstimateBody(
        client_name="Acme Co", customer_id="cust-1",
        lines=[EST.EstLine(name="Deep clean", amount=250.0),
               EST.EstLine(name="Polish", amount=50.0)],
        tax=10.0, notes="Gate code 4411", terms="Due on receipt")
    est = run(EST.create_estimate(body, USER))
    assert est["total_cents"] == 31000

    inv = run(EST.convert_to_invoice(est["id"], USER))
    assert inv["total_cents"] == 31000           # totals carry, to the cent
    assert inv["terms"] == "Due on receipt"      # terms carry
    assert inv["client_name"] == "Acme Co"
    assert [l["name"] for l in inv["lines"]] == ["Deep clean", "Polish"]
    assert inv["estimate_id"] == est["id"]       # invoice links back

    stored_est = run(Scoped(xenv.db.estimates, TID).find_one({"id": est["id"]}))
    assert stored_est["status"] == "converted"   # estimate marked converted
    assert stored_est["invoice_id"] == inv["id"]
    stored_inv = stored_invoice(xenv, inv["id"])
    assert stored_inv["notes"] == "Gate code 4411"

    with pytest.raises(HTTPException) as ei:     # never converts twice
        run(EST.convert_to_invoice(est["id"], USER))
    assert ei.value.status_code == 409


# ---------------- 2. Contract + sign + deposit ----------------
class _Req:
    headers = {"x-forwarded-for": "203.0.113.9",
               "user-agent": "TestBrowser/1.0"}
    client = SimpleNamespace(host="198.51.100.2")


def _signed_setup(xenv):
    cust = make_customer(xenv)
    inv = make_invoice(xenv, amount=400.0, customer_id=cust["id"])
    run(ID.split_invoice(inv["id"], ID.SplitBody(parts=[
        ID.SplitPart(label="Deposit", amount=100.0),
        ID.SplitPart(label="Balance", amount=300.0)]), USER))
    parent = stored_invoice(xenv, inv["id"])
    dep_id = parent["split_children"][0]
    bal_id = parent["split_children"][1]
    return cust, inv, dep_id, bal_id


def test_agreement_rides_to_deposit_and_gates_payment(xenv):
    cust, inv, dep_id, bal_id = _signed_setup(xenv)
    # agreement set on the parent BEFORE the split in real life — here we
    # set it on a fresh invoice and split again to prove the ride-along:
    inv2 = make_invoice(xenv, amount=200.0, customer_id=cust["id"])
    run(IX.set_agreement(inv2["id"], IX.AgreementBody(
        agreement_text="We guarantee the work for 12 months."), USER))
    run(ID.split_invoice(inv2["id"], ID.SplitBody(parts=[
        ID.SplitPart(label="Deposit", amount=50.0),
        ID.SplitPart(label="Balance", amount=150.0)]), USER))
    parent2 = stored_invoice(xenv, inv2["id"])
    dep2 = stored_invoice(xenv, parent2["split_children"][0])
    bal2 = stored_invoice(xenv, parent2["split_children"][1])
    assert dep2["agreement_text"] == "We guarantee the work for 12 months."
    assert not bal2.get("agreement_text")  # only the deposit is gated

    token = run(IP.ensure_client_token(TID, cust["id"], "Acme Co"))
    page = run(ID.client_page(token))
    dep_pub = next(i for i in page["open"] if i["id"] == dep2["id"])
    assert dep_pub["agreement"]["required"] is True
    assert dep_pub["agreement"]["signed"] is False

    # payment (the deposit step) is BLOCKED before signing
    with pytest.raises(HTTPException) as ei:
        run(ID.client_pay(token, dep2["id"],
                          ID.ClientPayBody(method="ach", origin_url="http://x")))
    assert ei.value.status_code == 409

    # consent is required
    with pytest.raises(HTTPException) as ei:
        run(IX.client_sign(token, dep2["id"], IX.SignBody(
            signer_name="Amy Acme", signature_type="typed",
            signature_data="Amy Acme", consent=False), _Req()))
    assert ei.value.status_code == 422

    out = run(IX.client_sign(token, dep2["id"], IX.SignBody(
        signer_name="Amy Acme", signature_type="typed",
        signature_data="Amy Acme", consent=True), _Req()))
    assert out["ok"] is True

    signed = stored_invoice(xenv, dep2["id"])
    sig = signed["signature"]
    assert sig["signer_name"] == "Amy Acme"          # audit trail written
    assert sig["ip"] == "203.0.113.9"
    assert sig["user_agent"] == "TestBrowser/1.0"
    assert sig["signed_at"] and sig["consent"] is True
    assert "not a third-party" in sig["method_note"]  # honest label stored
    tl = run(ID.invoice_timeline(dep2["id"], USER))
    events = (tl.get("timeline") or tl.get("events") or []) \
        if isinstance(tl, dict) else tl
    assert any(e["event"] == "signed" for e in events)

    # signed text can no longer be changed
    with pytest.raises(HTTPException) as ei:
        run(IX.set_agreement(dep2["id"], IX.AgreementBody(
            agreement_text="Changed after signing"), USER))
    assert ei.value.status_code == 409


# ---------------- 3. Job photos ----------------
def test_photo_attach_limits_and_client_page_render(xenv):
    cust = make_customer(xenv)
    inv = make_invoice(xenv, amount=100.0, customer_id=cust["id"])

    def up(name, data, ctype="image/jpeg", kind="job", caption=""):
        f = UploadFile(filename=name, file=BytesIO(data),
                       headers={"content-type": ctype})
        return run(IX.upload_photo(inv["id"], f, caption, kind, USER))

    p1 = up("before.jpg", b"\xff\xd8fake-jpeg", kind="before",
            caption="Before")
    assert p1["kind"] == "before" and p1["size_bytes"] > 0

    with pytest.raises(HTTPException) as ei:      # type allowlist
        up("anim.gif", b"GIF89a", ctype="image/gif")
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:      # size limit
        up("big.jpg", b"x" * (IX.MAX_PHOTO_BYTES + 1))
    assert ei.value.status_code == 422

    token = run(IP.ensure_client_token(TID, cust["id"], "Acme Co"))
    page = run(ID.client_page(token))
    pub = next(i for i in page["open"] if i["id"] == inv["id"])
    assert [p["id"] for p in pub["photos"]] == [p1["id"]]  # renders on page
    assert pub["photos"][0]["caption"] == "Before"

    resp = run(IX.client_get_photo(token, inv["id"], p1["id"]))
    assert resp.path.exists()                       # token-gated serve works
    with pytest.raises(HTTPException) as ei:        # wrong token = no photo
        run(IX.client_get_photo("bogus-token", inv["id"], p1["id"]))
    assert ei.value.status_code == 404

    # per-invoice count limit
    stored = stored_invoice(xenv, inv["id"])
    run(Scoped(xenv.db.invoices, TID).update_one(
        {"id": inv["id"]},
        {"$set": {"photos": [{"id": f"p{i}", "stored": "x.jpg",
                              "content_type": "image/jpeg",
                              "filename": "x.jpg", "size_bytes": 1,
                              "caption": "", "kind": "job",
                              "created_at": "2026-01-01"}
                             for i in range(IX.MAX_PHOTOS_PER_INVOICE)]}}))
    with pytest.raises(HTTPException) as ei:
        up("one-more.jpg", b"\xff\xd8x")
    assert ei.value.status_code == 422
    assert stored["id"] == inv["id"]


# ---------------- 4. Early-pay discount ----------------
def test_early_pay_discount_math_and_ledger_line(xenv):
    cust = make_customer(xenv)
    inv = run(INV.issue_invoice(INV.InvoiceBody(
        client_name="Acme Co", customer_id=cust["id"],
        lines=[INV.InvLine(name="Job", amount=1000.0)],
        early_pay_percent=2.0, early_pay_days=10), USER))
    assert inv["early_pay"] == {"percent": 2.0, "days": 10}

    # client page shows the discounted total + deadline next to full price
    token = run(IP.ensure_client_token(TID, cust["id"], "Acme Co"))
    page = run(ID.client_page(token))
    pub = next(i for i in page["open"] if i["id"] == inv["id"])
    assert pub["balance_cents"] == 100000
    assert pub["early_pay"]["discount_cents"] == 2000
    assert pub["early_pay"]["discounted_balance_cents"] == 98000
    assert pub["early_pay"]["active"] is True
    assert pub["early_pay"]["deadline"]

    # paying the discounted amount in-window settles it, honestly booked
    out = run(ID.record_online_payment(TID, inv["id"], 98000, "ach",
                                       "cs_test", "invoice_checkout"))
    assert out["status"] == "paid"
    stored = stored_invoice(xenv, inv["id"])
    assert stored["paid_cents"] == 100000
    assert stored["discount_applied_cents"] == 2000
    pmt = stored["payments"][-1]
    assert pmt["cash_cents"] == 98000 and pmt["discount_cents"] == 2000
    lines = _journal_lines(xenv, inv["id"])
    assert any(l.get("account_code") == "4900"
               and l.get("debit", 0) == 2000 for l in lines)  # discount line
    deb = sum(l.get("debit", 0) for l in xenv.db.journal_lines.docs
              if l.get("tenant_id") == TID)
    cred = sum(l.get("credit", 0) for l in xenv.db.journal_lines.docs
               if l.get("tenant_id") == TID)
    assert deb == cred  # books still balance


def test_early_pay_default_off_and_config_default(xenv):
    inv = make_invoice(xenv, amount=100.0)   # no override, no config
    assert inv["early_pay"] == {"percent": 0.0, "days": 0}
    run(ID.write_pay_config(ID.PayConfigBody(
        early_pay_percent=5.0, early_pay_days=7), USER))
    inv2 = make_invoice(xenv, amount=100.0)  # inherits the tenant default
    assert inv2["early_pay"] == {"percent": 5.0, "days": 7}
    inv3 = run(INV.issue_invoice(INV.InvoiceBody(   # per-invoice opt-out
        client_name="Acme Co",
        lines=[INV.InvLine(name="Job", amount=100.0)],
        early_pay_percent=0.0, early_pay_days=0,
        allow_duplicate=True), USER))  # third identical fixture invoice, deliberate
    assert inv3["early_pay"] == {"percent": 0.0, "days": 0}


def test_early_pay_manual_path_records_discount(xenv):
    inv = run(INV.issue_invoice(INV.InvoiceBody(
        client_name="Acme Co",
        lines=[INV.InvLine(name="Job", amount=500.0)],
        early_pay_percent=10.0, early_pay_days=30), USER))
    out = run(INV.pay_invoice(inv["id"], INV.PayBody(
        payment_method="bank", amount=450.0), USER))
    assert out["status"] == "paid"
    assert out["discount_applied_cents"] == 5000


# ---------------- 5. Trade benchmark line ----------------
def _seed_pool(xenv, n_invoices, niche="plumbing", opt_in=True):
    run(xenv.db.tenants.update_one(
        {"id": TID}, {"$set": {"niche_id": niche,
                               "benchmark_opt_in": opt_in}}))
    run(xenv.db.tenants.insert_one(
        {"id": "tenant-2", "name": "Other Shop", "niche_id": niche,
         "benchmark_opt_in": True}))
    for i in range(n_invoices):
        tid = TID if i % 2 == 0 else "tenant-2"
        run(Scoped(xenv.db.invoices, tid).insert_one({
            "id": f"pool-inv-{i}", "client_name": "Someone",
            "total_cents": 10000 + i * 1000, "paid_cents": 0,
            "status": "issued", "payments": [], "lines": [],
            "created_at": "2026-11-01T00:00:00+00:00"}))


def test_benchmark_floor_suppresses_below_threshold(xenv):
    _seed_pool(xenv, BM.MIN_TRADE_INVOICES - 1)
    out = run(BM.trade_line(USER))
    assert out["available"] is False       # below the floor: NOTHING shown
    assert out["line"] is None
    assert out["median_cents"] is None
    assert out["sample"] == BM.MIN_TRADE_INVOICES - 1


def test_benchmark_line_when_floor_met(xenv):
    _seed_pool(xenv, 12)
    out = run(BM.trade_line(USER))
    assert out["available"] is True
    assert out["sample"] == 12
    # totals 10000..21000 step 1000 -> median = (15000+16000)/2
    assert out["median_cents"] == 15500
    assert "Others in your trade" in out["line"]
    assert "$155.00" in out["line"]


def test_benchmark_pool_requires_opt_in(xenv):
    _seed_pool(xenv, 12, opt_in=False)     # our tenant never opted in;
    # the other tenant alone contributes only half the pool
    out = run(BM.trade_line(USER))
    assert out["sample"] == 6
    assert out["available"] is False


def test_early_pay_discount_never_touches_platform_fees(xenv):
    """Owner framing rule: the discount is the CONTRACTOR's money out of
    the contractor's own invoice total. Platform fee config must be
    byte-identical before and after an early-pay payment."""
    before = run(IP.get_pay_config(TID))
    inv = run(INV.issue_invoice(INV.InvoiceBody(
        client_name="Acme Co",
        lines=[INV.InvLine(name="Job", amount=200.0)],
        early_pay_percent=5.0, early_pay_days=10), USER))
    run(ID.record_online_payment(TID, inv["id"], 19000, "ach",
                                 "cs_test2", "invoice_checkout"))
    after = run(IP.get_pay_config(TID))
    for k in ("fee_percent", "fee_fixed_cents", "fee_methods",
              "surcharge_enabled", "surcharge_percent",
              "surcharge_fixed_cents"):
        assert after[k] == before[k]
    stored = stored_invoice(xenv, inv["id"])
    assert stored["discount_applied_cents"] == 1000  # contractor's 4900
    assert all(p.get("platform_fee_cents", 0) == 0
               for p in stored["payments"])
