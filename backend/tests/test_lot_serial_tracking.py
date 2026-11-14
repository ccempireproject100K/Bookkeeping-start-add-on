"""Lot & serial tracking (Top-20 #17) — self-contained suite.

Boots this branch's backend against its own mongod (started by the proof
script on :27131) and drives the real HTTP API. Covers: PO + direct
receiving with serials (count enforced), duplicate-serial guard (receive
+ assign, warnings naming where the serial lives), specific-serial
consumption, lot FEFO + override, over-consumption refusal, unit story
both directions, warranty clock from install date, recall range flagging
with holder list, expiry list, scan-to-move + offline-queue idempotency,
parts-at-a-site report, non-tracked invariance, tenant isolation.
"""
import os
import subprocess
import sys
import time
import uuid

import pytest
import requests

BASE = "http://127.0.0.1:8031/api"
MONGO = "mongodb://127.0.0.1:27131"
DBNAME = "books_lotserial_test"

_proc = None


def _wait_health(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(f"{BASE}/health", timeout=3)
            if r.status_code == 200 and r.json().get("db") == "ok":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


@pytest.fixture(scope="module", autouse=True)
def backend():
    global _proc
    env = {**os.environ, "MONGO_URL": MONGO, "DB_NAME": DBNAME,
           # JWT secret lengthened: main enforces >=32 chars at startup.
           "JWT_SECRET": "lotserial-test-secret-0123456789abcdef",
           "FRONTEND_URL": "http://localhost:3000"}
    _proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", "8031"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _wait_health(), "backend did not come up"
    yield
    _proc.terminate()


def _register():
    s = requests.Session()
    email = f"t{uuid.uuid4().hex[:10]}@example.com"
    r = s.post(f"{BASE}/auth/register", json={
        "email": email, "password": "Str0ng!Pass99", "name": "Tess",
        "shop_name": "Test Shop"}, timeout=15)
    assert r.status_code == 200, r.text
    # The session cookie is Secure-flagged (not resent over plain http by
    # requests), so carry the same token as a Bearer header instead.
    raw = r.headers.get("set-cookie", "")
    token = raw.split("access_token=")[1].split(";")[0] if "access_token=" in raw else ""
    assert token, r.headers
    s.headers["Authorization"] = f"Bearer {token}"
    return s


@pytest.fixture(scope="module")
def shop():
    return _register()


@pytest.fixture(scope="module")
def other_shop():
    return _register()


def _product(s, **kw):
    body = {"name": kw.pop("name", "Widget"), "type": "retail", "price": 100,
            "unit_cost": 40, "quantity_on_hand": 0}
    body.update(kw)
    r = s.post(f"{BASE}/products", json=body, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _receive_serials(s, pid, serials, **kw):
    return s.post(f"{BASE}/inventory/receive", json={
        "product_id": pid, "quantity": len(serials), "serials": serials, **kw},
        timeout=15)


# ------------------------------------------------------------ receiving

def test_receive_serials_count_enforced(shop):
    p = _product(shop, name="HVAC Unit", tracking_mode="serial", warranty_months=24)
    r = shop.post(f"{BASE}/inventory/receive", json={
        "product_id": p["id"], "quantity": 2, "serials": ["SN-001"]}, timeout=15)
    assert r.status_code == 422, r.text
    r = _receive_serials(shop, p["id"], ["SN-001", "SN-002"])
    assert r.status_code == 200, r.text
    assert r.json()["quantity"] == 2
    prod = shop.get(f"{BASE}/products", timeout=15).json()
    mine = [x for x in prod if x["id"] == p["id"]][0]
    assert mine["quantity_on_hand"] == 2
    assert mine["tracking_mode"] == "serial" and mine["warranty_months"] == 24


def test_po_receive_captures_serials(shop):
    p = _product(shop, name="Compressor", tracking_mode="serial")
    r = shop.post(f"{BASE}/purchase-orders", json={
        "vendor": "SupplyCo", "lines": [
            {"product_id": p["id"], "quantity": 2, "unit_cost": 250}]}, timeout=15)
    assert r.status_code == 200, r.text
    po = r.json()
    shop.post(f"{BASE}/purchase-orders/{po['id']}/approve", timeout=15)
    # bad serial count refused BEFORE anything posts
    r = shop.post(f"{BASE}/purchase-orders/{po['id']}/receive", json={
        "lines": [{"product_id": p["id"], "quantity": 2, "serials": ["CP-1"]}]}, timeout=15)
    assert r.status_code == 422, r.text
    units = shop.get(f"{BASE}/inventory/units?product_id={p['id']}", timeout=15).json()
    assert units == []
    r = shop.post(f"{BASE}/purchase-orders/{po['id']}/receive", json={
        "lines": [{"product_id": p["id"], "quantity": 2,
                   "serials": ["CP-1", "CP-2"]}]}, timeout=15)
    assert r.status_code == 200, r.text
    units = shop.get(f"{BASE}/inventory/units?product_id={p['id']}", timeout=15).json()
    assert {u["serial"] for u in units} == {"CP-1", "CP-2"}
    assert all(u["received_ref"] == po["id"] for u in units)


# ------------------------------------------------- duplicate-serial guard

def test_duplicate_serial_receive_refused_names_location(shop):
    p = _product(shop, name="Thermostat", tracking_mode="serial")
    assert _receive_serials(shop, p["id"], ["DUP-1"]).status_code == 200
    r = _receive_serials(shop, p["id"], ["DUP-1"])
    assert r.status_code == 409, r.text
    assert "already in stock" in r.json()["detail"]
    # assign it, then try receiving it again — warning must name the install
    r = shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p["id"], "serials": ["DUP-1"], "job_id": "JOB-77"}, timeout=15)
    assert r.status_code == 200, r.text
    r = _receive_serials(shop, p["id"], ["dup-1"])  # case-insensitive
    assert r.status_code == 409, r.text
    assert "JOB-77" in r.json()["detail"]


def test_assign_already_installed_refused(shop):
    p = _product(shop, name="Furnace", tracking_mode="serial")
    assert _receive_serials(shop, p["id"], ["F-1"]).status_code == 200
    body = {"product_id": p["id"], "serials": ["F-1"], "customer_id": "CUST-9"}
    assert shop.post(f"{BASE}/inventory/assign", json=body, timeout=15).status_code == 200
    r = shop.post(f"{BASE}/inventory/assign", json=body, timeout=15)
    assert r.status_code == 409, r.text
    assert "CUST-9" in r.json()["detail"]


# ------------------------------------------------------------- lot flows

def _lot_product_with_stock(s):
    p = _product(s, name="Filter Pack", tracking_mode="lot")
    r = s.post(f"{BASE}/inventory/receive", json={
        "product_id": p["id"], "quantity": 5, "lot_number": "LOT-LATE",
        "expiry_date": "2027-06-01"}, timeout=15)
    assert r.status_code == 200, r.text
    r = s.post(f"{BASE}/inventory/receive", json={
        "product_id": p["id"], "quantity": 5, "lot_number": "LOT-EARLY",
        "expiry_date": "2026-12-01"}, timeout=15)
    assert r.status_code == 200, r.text
    return p


def test_lot_fefo_default_and_override(shop):
    p = _lot_product_with_stock(shop)
    r = shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p["id"], "quantity": 3, "job_id": "JOB-FEFO"}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["lot"]["lot_number"] == "LOT-EARLY"  # earliest expiry first
    assert r.json()["lot"]["quantity_on_hand"] == 2
    lots = {l["lot_number"]: l for l in
            shop.get(f"{BASE}/inventory/lots?product_id={p['id']}", timeout=15).json()}
    r = shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p["id"], "quantity": 1, "lot_id": lots["LOT-LATE"]["id"],
        "job_id": "JOB-OVR"}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["lot"]["lot_number"] == "LOT-LATE"


def test_overconsumption_refused(shop):
    p = _lot_product_with_stock(shop)
    r = shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p["id"], "quantity": 99, "job_id": "JOB-X"}, timeout=15)
    assert r.status_code == 409, r.text
    p2 = _product(shop, name="Motor", tracking_mode="serial")
    assert _receive_serials(shop, p2["id"], ["M-1"]).status_code == 200
    r = shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p2["id"], "serials": ["M-404"], "job_id": "JOB-X"}, timeout=15)
    assert r.status_code == 404, r.text


# ------------------------------------------------------- story + warranty

def test_unit_story_both_directions_and_warranty_clock(shop):
    p = _product(shop, name="Heat Pump", tracking_mode="serial", warranty_months=12)
    assert _receive_serials(shop, p["id"], ["HP-100"]).status_code == 200
    r = shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p["id"], "serials": ["HP-100"], "job_id": "JOB-W",
        "customer_id": "CUST-W"}, timeout=15)
    assert r.status_code == 200, r.text
    unit = r.json()["units"][0]
    # warranty clock starts the install (assignment) day, not the receive day
    from lib.base import today_str  # noqa: E402  (same clock the server uses)
    assert unit["warranty_start"] == unit["assigned_date"]
    assert unit["warranty_until"] > unit["warranty_start"]
    assert unit["warranty_status"] == "covered"
    # direction 1: where is serial X
    r = shop.get(f"{BASE}/inventory/serial/HP-100", timeout=15)
    assert r.status_code == 200
    story_types = [m["type"] for m in r.json()["story"]]
    assert story_types == ["receive", "assign"]
    # direction 2: what's at this job
    r = shop.get(f"{BASE}/inventory/at?job_id=JOB-W", timeout=15)
    assert r.status_code == 200
    assert [u["serial"] for u in r.json()["serial_units"]] == ["HP-100"]
    assert r.json()["serial_units"][0]["warranty_status"] == "covered"
    # CSV export works
    r = shop.get(f"{BASE}/inventory/at?job_id=JOB-W&format=csv", timeout=15)
    assert r.status_code == 200 and "HP-100" in r.text
    # warranty report carries it as covered
    r = shop.get(f"{BASE}/inventory/warranty", timeout=15)
    mine = [u for u in r.json()["units"] if u["serial"] == "HP-100"]
    assert mine and mine[0]["warranty_status"] == "covered"
    # return clears the clock and the site
    r = shop.post(f"{BASE}/inventory/return", json={
        "product_id": p["id"], "serials": ["HP-100"]}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["units"][0]["warranty_start"] is None
    r = shop.get(f"{BASE}/inventory/at?job_id=JOB-W", timeout=15)
    assert r.json()["serial_units"] == []


def test_warranty_statuses():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from routers.tracking import warranty_status
    from datetime import date, timedelta
    today = date.today()
    soon = (today + timedelta(days=10)).isoformat()
    past = (today - timedelta(days=1)).isoformat()
    far = (today + timedelta(days=90)).isoformat()
    t = today.isoformat()
    assert warranty_status({"warranty_start": None}, t) == "not_installed"
    assert warranty_status({"warranty_start": t, "warranty_until": far}, t) == "covered"
    assert warranty_status({"warranty_start": t, "warranty_until": soon}, t) == "expiring_soon"
    assert warranty_status({"warranty_start": t, "warranty_until": past}, t) == "out_of_warranty"
    assert warranty_status({"warranty_start": t, "warranty_until": None}, t) == "no_warranty"


# ------------------------------------------------------------ recalls

def test_recall_range_flags_units_and_lists_holders(shop):
    p = _product(shop, name="Water Heater", tracking_mode="serial")
    serials = [f"WH-10{i}" for i in range(5)]  # WH-100..WH-104
    assert _receive_serials(shop, p["id"], serials).status_code == 200
    shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p["id"], "serials": ["WH-101"], "job_id": "JOB-R1",
        "customer_id": "CUST-R1"}, timeout=15)
    shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p["id"], "serials": ["WH-103"], "customer_id": "CUST-R2"},
        timeout=15)
    r = shop.post(f"{BASE}/inventory/recalls", json={
        "product_id": p["id"], "serial_from": "WH-101", "serial_to": "WH-103",
        "reason": "Manufacturer recall: valve defect"}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected_count"] == 3
    holders = {u["serial"]: u for u in body["affected_units"]}
    assert holders["WH-101"]["job_id"] == "JOB-R1"
    assert holders["WH-103"]["customer_id"] == "CUST-R2"
    assert holders["WH-102"]["status"] == "in_stock"
    # flagged units can't be assigned
    r = shop.post(f"{BASE}/inventory/assign", json={
        "product_id": p["id"], "serials": ["WH-102"], "job_id": "JOB-Z"}, timeout=15)
    assert r.status_code == 409, r.text
    # recall is fetchable with the same holder list
    r = shop.get(f"{BASE}/inventory/recalls/{body['recall']['id']}", timeout=15)
    assert r.status_code == 200
    assert len(r.json()["affected_units"]) == 3
    # lot recall flags the lot
    p2 = _product(shop, name="Pipe Dope", tracking_mode="lot")
    shop.post(f"{BASE}/inventory/receive", json={
        "product_id": p2["id"], "quantity": 4, "lot_number": "DOPE-9"}, timeout=15)
    r = shop.post(f"{BASE}/inventory/recalls", json={
        "lot_number": "DOPE-9", "reason": "Contamination"}, timeout=15)
    assert r.status_code == 200, r.text
    assert len(r.json()["affected_lots"]) == 1


def test_expiring_list(shop):
    p = _product(shop, name="Sealant", tracking_mode="lot")
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=5)).isoformat()
    gone = (date.today() - timedelta(days=2)).isoformat()
    far = (date.today() + timedelta(days=300)).isoformat()
    for lot, exp in (("SEA-SOON", soon), ("SEA-GONE", gone), ("SEA-FAR", far)):
        r = shop.post(f"{BASE}/inventory/receive", json={
            "product_id": p["id"], "quantity": 2, "lot_number": lot,
            "expiry_date": exp}, timeout=15)
        assert r.status_code == 200, r.text
    rows = shop.get(f"{BASE}/inventory/expiring?days=30", timeout=15).json()
    names = {r["lot_number"]: r for r in rows if r["product_id"] == p["id"]}
    assert set(names) == {"SEA-SOON", "SEA-GONE"}
    assert names["SEA-GONE"]["expired"] is True
    assert names["SEA-SOON"]["days_left"] == 5


# ------------------------------------------------------------------ scan

def test_scan_lookup_receive_assign_and_idempotency(shop):
    p = _product(shop, name="Furnace Board", tracking_mode="serial")
    r = shop.post(f"{BASE}/inventory/scan", json={
        "code": "FB-1", "action": "receive", "product_id": p["id"],
        "client_key": "fc_scan_1"}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["quantity"] == 1
    # retry of the same queued scan returns the stored result, no duplicate
    r2 = shop.post(f"{BASE}/inventory/scan", json={
        "code": "FB-1", "action": "receive", "product_id": p["id"],
        "client_key": "fc_scan_1"}, timeout=15)
    assert r2.json().get("duplicate") is True
    units = shop.get(f"{BASE}/inventory/units?product_id={p['id']}", timeout=15).json()
    assert len(units) == 1
    r = shop.post(f"{BASE}/inventory/scan", json={
        "code": "FB-1", "action": "lookup"}, timeout=15)
    assert r.json()["result"]["found"] == "serial"
    r = shop.post(f"{BASE}/inventory/scan", json={
        "code": "FB-1", "action": "assign", "product_id": p["id"],
        "job_id": "JOB-SCAN"}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["units"][0]["status"] == "assigned"


# ------------------------------------------------------ invariance + scope

def test_nontracked_products_unchanged(shop):
    p = _product(shop, name="Plain Widget")  # no tracking_mode -> none
    assert "tracking_mode" not in p or p.get("tracking_mode") in (None, "none")
    r = shop.post(f"{BASE}/products/{p['id']}/restock",
                  json={"quantity": 3, "unit_cost": 10}, timeout=15)
    assert r.status_code == 200 and r.json()["quantity_on_hand"] == 3
    r = shop.post(f"{BASE}/inventory/receive", json={
        "product_id": p["id"], "quantity": 1, "serials": ["X-1"]}, timeout=15)
    assert r.status_code == 400, r.text  # tracked lane refuses; core untouched
    units = shop.get(f"{BASE}/inventory/units?product_id={p['id']}", timeout=15).json()
    assert units == []


def test_tenant_isolation(shop, other_shop):
    p = _product(shop, name="Isolated", tracking_mode="serial")
    assert _receive_serials(shop, p["id"], ["ISO-1"]).status_code == 200
    r = other_shop.get(f"{BASE}/inventory/serial/ISO-1", timeout=15)
    assert r.status_code == 404
    r = other_shop.get(f"{BASE}/inventory/units", timeout=15)
    assert all(u["serial"] != "ISO-1" for u in r.json())
    # the other tenant can receive the same serial in ITS OWN books
    p2 = _product(other_shop, name="Isolated", tracking_mode="serial")
    assert _receive_serials(other_shop, p2["id"], ["ISO-1"]).status_code == 200
