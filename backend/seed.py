"""Seed demo workspaces for The Barber's Ledger. Run: cd /app/backend && python seed.py
Wipes demo collections and recreates Fade & Co. (rich data) + Sharp Lines (isolation)."""
import asyncio
import os
import random
from datetime import date, timedelta

os.environ.setdefault("PYTHONPATH", ".")

from db import db
from lib.base import Scoped, new_id, now_iso
from lib.auth import hash_password
from lib.coa import seed_coa
from lib import ledger as L
from lib import posting as P

random.seed(42)

COLLECTIONS = ["tenants", "users", "accounts", "journal_entries", "journal_lines",
               "fiscal_periods", "products", "sales", "invoices", "cash_closes",
               "counters", "audit_log", "migration_markers", "bills", "gift_cards",
               "commission_payouts", "customers", "jobs", "job_costs"]

SERVICES = [("service", "AC Diagnostic", 12900, "4300"),
            ("service", "Filter Replacement", 4900, "4020"),
            ("service", "AC Tune-Up", 18900, "4300"),
            ("service", "Thermostat Install", 24900, "4300")]


async def make_user(tenant_id, email, name, role, pw, rate=0):
    u = {"id": new_id(), "tenant_id": tenant_id, "email": email.lower(),
         "password_hash": hash_password(pw), "name": name, "role": role,
         "commission_rate": rate, "created_at": now_iso()}
    await db.users.insert_one(u)
    return u


async def make_product(tenant_id, name, ptype, price_c, cost_c, qty, code, img=None):
    doc = {"id": new_id(), "tenant_id": tenant_id, "name": name, "type": ptype,
           "revenue_code": code, "price_cents": price_c, "unit_cost_cents": cost_c,
           "quantity_on_hand": qty, "commissionable": True, "image_url": img,
           "created_at": now_iso()}
    await db.products.insert_one(doc)
    return doc


async def do_sale(tenant_id, owner, d, barbers, products, tax_rate):
    svc = random.choice(SERVICES)
    rev_by_code = {svc[3]: svc[2]}
    service_subtotal = svc[2]
    product_subtotal = 0
    cogs = 0
    if random.random() < 0.3 and products:
        p = random.choice(products)
        res = await db.products.update_one(
            {"id": p["id"], "tenant_id": tenant_id, "quantity_on_hand": {"$gte": 1}},
            {"$inc": {"quantity_on_hand": -1}})
        if res.matched_count:
            product_subtotal += p["price_cents"]
            cogs += p["unit_cost_cents"]
            rev_by_code[p["revenue_code"]] = rev_by_code.get(p["revenue_code"], 0) + p["price_cents"]
    subtotal = service_subtotal + product_subtotal
    pm = random.choice(["cash", "cash", "card"])
    taxable = product_subtotal
    tax = round(taxable * tax_rate)
    barber = random.choice(barbers)
    commission = round(service_subtotal * barber["commission_rate"])
    lines_in = [{"revenue_code": c, "amount_cents": a} for c, a in rev_by_code.items()]
    sale_id = new_id()
    entry = await L.post_journal_entry(db, tenant_id, owner, P.build_sale(
        lines_in=lines_in, payment_method=pm, subtotal_cents=subtotal, discount_cents=0,
        tax_cents=tax, cogs_cents=cogs, commission_cents=commission,
        entry_date=d.isoformat(), source_ref=sale_id, memo="Quick Sell"),
        status="posted", idempotency_key=f"sale:{sale_id}")
    total = subtotal + tax
    await db.sales.insert_one({
        "id": sale_id, "tenant_id": tenant_id, "cart": [{"name": svc[1], "qty": 1, "amount_cents": svc[2]}],
        "payment_method": pm, "subtotal_cents": subtotal, "discount_cents": 0, "tax_cents": tax,
        "total_cents": total, "cogs_cents": cogs, "commission_cents": commission,
        "barber_id": barber["id"], "entry_id": entry["id"], "entry_number": entry["number"],
        "created_at": d.isoformat()})


async def seed_fade():
    tid = new_id()
    await db.tenants.insert_one({"id": tid, "name": "Mind at Ease Solutions HVAC",
        "settings": {"inventory_enabled": True, "sales_tax_rate": 0.0725, "double_entry_mode": "primary",
                     "industry": "hvac"},
        "created_at": now_iso()})
    await seed_coa(Scoped(db.accounts, tid))
    owner = await make_user(tid, "onelovellc@outlook.com", "Darnell Price", "owner", "Owner123!")
    await make_user(tid, "accountant@fadeco.com", "Priya Shah", "accountant", "Books123!")
    await make_user(tid, "bookkeeper@fadeco.com", "Sam Reed", "bookkeeper", "Books123!")
    b1 = await make_user(tid, "marcus@fadeco.com", "Marcus Vance", "barber", "Barber123!", 0.10)
    b2 = await make_user(tid, "tasha@fadeco.com", "Tasha Lane", "barber", "Barber123!", 0.12)
    barbers = [b1, b2]

    products = [
        await make_product(tid, "Air Filter 20x25x1 (MERV 11)", "retail", 2400, 900, 160, "4100"),
        await make_product(tid, "Capacitor 45/5 MFD", "retail", 4500, 1800, 140, "4100"),
        await make_product(tid, "Programmable Thermostat", "retail", 13900, 6200, 120, "4100"),
        await make_product(tid, "R-410A Refrigerant (per lb)", "supply", 0, 900, 50, "4100"),
    ]
    retail = [p for p in products if p["type"] == "retail"]

    # opening balances migration (books inventory as an asset before trading begins)
    today = date.today()
    open_date = (today - timedelta(days=91)).isoformat()
    inv_retail = sum(p["unit_cost_cents"] * p["quantity_on_hand"] for p in products if p["type"] == "retail")
    inv_supply = sum(p["unit_cost_cents"] * p["quantity_on_hand"] for p in products if p["type"] == "supply")
    snapshot = {"1010": 50000, "1020": 500000, "1300": inv_retail, "1310": inv_supply}
    ob = await L.post_journal_entry(db, tid, owner, P.build_opening_balances(
        snapshot=snapshot, entry_date=open_date,
        memo="Opening balances migration (seed). Equity is the balancing plug."), status="posted")
    await db.migration_markers.insert_one({"tenant_id": tid, "type": "opening_balances",
        "status": "complete", "entry_id": ob["id"], "at": now_iso()})

    # ~90 days of sales
    for i in range(90, 0, -1):
        d = today - timedelta(days=i)
        if d.weekday() == 6:  # closed Sundays
            continue
        for _ in range(random.randint(2, 5)):
            await do_sale(tid, owner, d, barbers, retail, 0.0725)

    # expenses: shop rent + utilities (manual posted entries, monthly)
    for m in range(3, 0, -1):
        d = (today.replace(day=1) - timedelta(days=30 * (m - 1))).replace(day=1)
        await L.post_journal_entry(db, tid, owner, {"entry_date": d.isoformat(), "source": "manual",
            "memo": "Shop rent", "lines": [P.dr("5300", 180000, "Monthly rent"), P.cr("1020", 180000)]},
            status="posted")
        await L.post_journal_entry(db, tid, owner, {"entry_date": d.isoformat(), "source": "manual",
            "memo": "Utilities", "lines": [P.dr("5400", 42000, "Electricity & water"), P.cr("1020", 42000)]},
            status="posted")

    # invoices: one paid, one open
    inv1 = new_id()
    e = await L.post_journal_entry(db, tid, owner, P.build_invoice_issue(
        revenue_lines=[{"revenue_code": "4200", "amount_cents": 60000}], tax_cents=0, cogs_cents=0,
        gross_cents=60000, entry_date=(today - timedelta(days=20)).isoformat(),
        source_ref=inv1, memo="Chair rent — booth 3"), status="posted")
    await db.invoices.insert_one({"id": inv1, "tenant_id": tid, "client_name": "Leo Sterling (Booth Rent)",
        "lines": [{"kind": "service", "name": "Monthly booth rent", "revenue_code": "4200", "amount": 600.0}],
        "subtotal_cents": 60000, "tax_cents": 0, "total_cents": 60000, "status": "issued",
        "due_date": today.isoformat(), "issue_entry": e["number"], "paid_entry": None, "created_at": now_iso()})

    inv2 = new_id()
    e2 = await L.post_journal_entry(db, tid, owner, P.build_invoice_issue(
        revenue_lines=[{"revenue_code": "4020", "amount_cents": 25000}], tax_cents=0, cogs_cents=0,
        gross_cents=25000, entry_date=(today - timedelta(days=15)).isoformat(),
        source_ref=inv2, memo="Event grooming package"), status="posted")
    e2b = await L.post_journal_entry(db, tid, owner, P.build_invoice_paid(
        amount_cents=25000, payment_method="bank", entry_date=(today - timedelta(days=10)).isoformat(),
        source_ref=inv2, memo="Event grooming package"), status="posted")
    await db.invoices.insert_one({"id": inv2, "tenant_id": tid, "client_name": "Northside Films LLC",
        "lines": [{"kind": "service", "name": "On-set grooming", "revenue_code": "4020", "amount": 250.0}],
        "subtotal_cents": 25000, "tax_cents": 0, "total_cents": 25000, "status": "paid",
        "due_date": None, "issue_entry": e["number"], "paid_entry": e2b["number"], "created_at": now_iso()})

    print(f"  Fade & Co.: owner+4 staff, {len(products)} products, ~90 days of sales, 2 invoices")

    # gift cards (deferred revenue)
    for amt, who in [(10000, "Jordan M."), (5000, "Alex R.")]:
        gc_id = new_id()
        e = await L.post_journal_entry(db, tid, owner, {
            "entry_date": (today - timedelta(days=12)).isoformat(), "source": "giftcard_issue", "source_ref": gc_id,
            "memo": f"Gift card — {who}", "lines": [P.dr("1050", amt, "Gift card sold"), P.cr("2200", amt, "Unearned revenue")],
        }, status="posted")
        await db.gift_cards.insert_one({"id": gc_id, "tenant_id": tid, "code": "GC-" + gc_id[:8].upper(),
            "recipient": who, "initial_cents": amt, "balance_cents": amt, "status": "active",
            "issue_entry": e["number"], "created_at": now_iso()})

    # vendor bills (accounts payable), one overdue
    for vendor, amt, days, code in [("Clipper Supply Co.", 32000, -20, "5700"), ("Fresh Towel Service", 8500, 10, "5700")]:
        b_id = new_id()
        bdate = (today - timedelta(days=max(days + 30, 5))).isoformat()
        due = (today + timedelta(days=days)).isoformat()
        e = await L.post_journal_entry(db, tid, owner, {
            "entry_date": bdate, "source": "bill", "source_ref": b_id, "memo": f"Bill — {vendor}",
            "lines": [P.dr(code, amt, vendor), P.cr("2010", amt, "Accounts payable")],
        }, status="posted")
        await db.bills.insert_one({"id": b_id, "tenant_id": tid, "vendor": vendor, "amount_cents": amt,
            "expense_code": code, "bill_date": bdate, "due_date": due, "status": "open",
            "issue_entry": e["number"], "paid_entry": None, "created_at": now_iso()})

    # ---- HVAC job costing demo ----
    cust = {}
    for name, loc in [("Northgate Office Park", "4400 Northgate Blvd, Suite 200"),
                      ("Rosa Martinez", "812 Elm Street"),
                      ("Belmont Retail Center", "15 Belmont Ave")]:
        cid = new_id()
        await db.customers.insert_one({"id": cid, "tenant_id": tid, "name": name, "email": None,
            "phone": None, "locations": [{"label": "Primary", "address": loc}], "created_at": now_iso()})
        cust[name] = cid

    async def mk_job(num_seq, customer, jtype, desc, est_rev, est_costs, status, days_ago):
        jid = new_id()
        job = {"id": jid, "tenant_id": tid, "job_number": f"JOB-{num_seq:04d}",
            "customer_id": cust[customer], "customer_name": customer, "service_location": "Primary",
            "job_type": jtype, "description": desc, "status": status,
            "estimated_revenue_cents": est_rev,
            "estimated_costs_cents": {c: est_costs.get(c, 0) for c in ["material", "labor", "equipment", "subcontractor", "other"]},
            "change_orders": [], "created_at": (today - timedelta(days=days_ago)).isoformat()}
        await db.jobs.insert_one(job)
        return job

    async def add_cost(job, category, desc, amt, tech=None, days_ago=5):
        debit = {"material": "5000", "labor": "5005", "equipment": "5006", "subcontractor": "5007", "other": "5008"}[category]
        e = await L.post_journal_entry(db, tid, owner, {"entry_date": (today - timedelta(days=days_ago)).isoformat(),
            "source": "job_cost", "source_ref": job["id"], "memo": f"{job['job_number']} {category}",
            "lines": [P.dr(debit, amt, desc), P.cr("2010", amt, "Job cost")]}, status="posted")
        await db.job_costs.insert_one({"id": new_id(), "tenant_id": tid, "job_id": job["id"],
            "job_number": job["job_number"], "category": category, "description": desc, "amount_cents": amt,
            "hours": 0, "rate": 0, "technician_id": tech, "entry_number": e["number"], "created_at": now_iso()})

    async def add_job_invoice(job, amount_cents, days_ago=3):
        inv_id = new_id()
        e = await L.post_journal_entry(db, tid, owner, P.build_invoice_issue(
            revenue_lines=[{"revenue_code": "4300", "amount_cents": amount_cents}], tax_cents=0, cogs_cents=0,
            gross_cents=amount_cents, entry_date=(today - timedelta(days=days_ago)).isoformat(),
            source_ref=inv_id, memo=f"{job['job_number']} — {job['customer_name']}"), status="posted")
        await db.invoices.insert_one({"id": inv_id, "tenant_id": tid, "job_id": job["id"],
            "client_name": job["customer_name"], "lines": [{"kind": "job", "name": f"{job['job_number']} contract",
            "revenue_code": "4300", "amount": amount_cents / 100}], "subtotal_cents": amount_cents, "tax_cents": 0,
            "total_cents": amount_cents, "status": "issued", "due_date": None, "issue_entry": e["number"],
            "paid_entry": None, "created_at": now_iso()})

    jA = await mk_job(1, "Northgate Office Park", "installation", "5-ton rooftop AC unit install",
        800000, {"material": 250000, "labor": 150000, "equipment": 100000, "subcontractor": 50000}, "completed", 18)
    await add_cost(jA, "material", "Condenser, coil, ductwork", 250000, days_ago=16)
    await add_cost(jA, "labor", "Install crew (24 hrs)", 150000, tech=b1["id"], days_ago=15)
    await add_cost(jA, "equipment", "Crane rental", 100000, days_ago=15)
    await add_cost(jA, "subcontractor", "Electrical hookup", 50000, days_ago=14)
    await add_job_invoice(jA, 800000, days_ago=12)

    jB = await mk_job(2, "Rosa Martinez", "repair", "Compressor replacement",
        120000, {"material": 30000, "labor": 60000}, "active", 6)
    await add_cost(jB, "material", "Compressor + refrigerant", 30000, days_ago=4)
    await add_cost(jB, "labor", "Repair (6 hrs)", 60000, tech=b2["id"], days_ago=4)
    await add_job_invoice(jB, 120000, days_ago=2)

    jC = await mk_job(3, "Belmont Retail Center", "maintenance", "Seasonal tune-up (overran)",
        60000, {"labor": 40000}, "completed", 10)
    await add_cost(jC, "labor", "Tune-up + unexpected coil clean (overrun)", 55000, tech=b1["id"], days_ago=8)
    await add_cost(jC, "material", "Coil cleaner + belts", 10000, days_ago=8)
    await add_job_invoice(jC, 40000, days_ago=6)


async def seed_sharp():
    tid = new_id()
    await db.tenants.insert_one({"id": tid, "name": "Sharp Lines Studio",
        "settings": {"inventory_enabled": True, "sales_tax_rate": 0.06, "double_entry_mode": "primary"},
        "created_at": now_iso()})
    await seed_coa(Scoped(db.accounts, tid))
    await make_user(tid, "owner@sharplines.com", "Jordan Cole", "owner", "Owner123!")
    print("  Sharp Lines Studio: isolation workspace (owner only)")


async def main():
    if os.environ.get("APP_ENV") == "production":
        print("Refusing to seed in production."); return
    for c in COLLECTIONS:
        await db[c].delete_many({})
    print("Cleared demo collections. Seeding...")
    await seed_fade()
    await seed_sharp()
    print("seeded")


if __name__ == "__main__":
    asyncio.run(main())
