from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

import os
import logging
from fastapi import FastAPI, APIRouter
from starlette.middleware.cors import CORSMiddleware

from db import db, get_client
from lib.ratelimit import RateLimitMiddleware
from lib.security_headers import SecurityHeadersMiddleware
from routers import (auth, ledger, products, pos, invoices, cashclose, dashboard,
                     bills, aging, giftcards, commissions, customers, jobs,
                     banking, vendors, reports, estimates,
                     recurring_invoices, reminders, backup, bankfeed, copilot,
                     time, portal, payroll, budget, tax, sales_tax, edition, totp, audit,
                     webauthn, alerts, invites, receipts, mileage)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("barbers_ledger")

app = FastAPI(title="The Barber's Ledger")
api_router = APIRouter(prefix="/api")


@api_router.get("/health")
async def health():
    try:
        await db.command("ping")
        pending = await db.journal_entries.count_documents({"status": "draft"})
        return {"db": "ok", "pending_ops": pending}
    except Exception as e:
        return {"db": "error", "detail": str(e)}


for r in (auth.router, ledger.router, products.router, pos.router,
          invoices.router, cashclose.router, dashboard.router,
          bills.router, aging.router, giftcards.router, commissions.router,
          customers.router, jobs.router, banking.router, vendors.router,
          reports.router, estimates.router,
          recurring_invoices.router, reminders.router, backup.router,
          bankfeed.router, copilot.router, time.router, portal.router,
          payroll.router, budget.router, tax.router, sales_tax.router, edition.router,
          edition.public_router,
          totp.router, audit.router, webauthn.router, alerts.router,
          invites.router, receipts.router, mileage.router):
    api_router.include_router(r)

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.on_event("startup")
async def startup():
    indexes = [
        (db.users, "email", {"unique": True}),
        (db.accounts, [("tenant_id", 1), ("code", 1)], {"unique": True}),
        (db.journal_entries, [("tenant_id", 1), ("status", 1), ("entry_date", 1)], {}),
        (db.journal_lines, [("tenant_id", 1), ("account_code", 1), ("entry_date", 1)], {}),
        (db.fiscal_periods, [("tenant_id", 1), ("key", 1)], {"unique": True}),
        (db.jobs, [("tenant_id", 1), ("job_number", 1)], {"unique": True}),
        (db.bank_transactions, [("tenant_id", 1), ("dedupe_hash", 1)], {"unique": True}),
        (db.bank_accounts, [("tenant_id", 1), ("gl_code", 1)], {}),
    ]
    for coll, keys, opts in indexes:
        try:
            await coll.create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"Index on {coll.name} skipped: {e}")
    logger.info("Barber's Ledger started; indexes ensured.")


@app.on_event("shutdown")
async def shutdown():
    get_client().close()
