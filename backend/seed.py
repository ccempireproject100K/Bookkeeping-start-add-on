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
               "counters", "audit_log", "migration_markers"]

SERVICES = [("haircut", "Signature Haircut", 3500, "4000"),
            ("beard", "Beard Trim & Line-up", 2000, "4010"),
            ("service", "Hot Towel Shave", 4000, "4020"),
            ("haircut", "Skin Fade", 4000, "4000")]


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
    await db.tenants.insert_one({"id": tid, "name": "Fade & Co. Barbers",
        "settings": {"inventory_enabled": True, "sales_tax_rate": 0.0725, "double_entry_mode": "primary"},
        "created_at": now_iso()})
    await seed_coa(Scoped(db.accounts, tid))
    owner = await make_user(tid, "onelovellc@outlook.com", "Darnell Price", "owner", "Owner123!")
    await make_user(tid, "accountant@fadeco.com", "Priya Shah", "accountant", "Books123!")
    await make_user(tid, "bookkeeper@fadeco.com", "Sam Reed", "bookkeeper", "Books123!")
    b1 = await make_user(tid, "marcus@fadeco.com", "Marcus Vance", "barber", "Barber123!", 0.10)
    b2 = await make_user(tid, "tasha@fadeco.com", "Tasha Lane", "barber", "Barber123!", 0.12)
    barbers = [b1, b2]

    products = [
        await make_product(tid, "Matte Clay Pomade", "retail", 2200, 900, 160, "4100"),
        await make_product(tid, "Beard Oil — Cedar", "retail", 1800, 700, 140, "4100"),
        await make_product(tid, "Styling Cream", "retail", 2000, 800, 120, "4100"),
        await make_product(tid, "Barbicide Disinfectant", "supply", 0, 400, 50, "4100"),
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
