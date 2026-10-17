from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

import os
import logging
from fastapi import FastAPI, APIRouter
from starlette.middleware.cors import CORSMiddleware

from db import db, get_client
from lib.ratelimit import RateLimitMiddleware
from lib.security_headers import ContentSecurityPolicyMiddleware, SecurityHeadersMiddleware
from routers import plans
from routers import (auth, ledger, products, pos, invoices, cashclose, dashboard,
                     bills, aging, giftcards, commissions, customers, jobs,
                     banking, vendors, reports, estimates,
                     recurring_invoices, reminders, backup, bankfeed, copilot,
                     time, portal, payroll, budget, tax, sales_tax, edition, totp, audit,
                     webauthn, alerts, invites, receipts, mileage, collections,
                     simulator, subcompliance, cpa, pulse, pricing, taxforecast,
                     playground, combo, kpis, benchmarks, voice, taxposture,
                     whitelabel, entities, cleanup, purchase_orders,
                     fx, market_packs, cfdi)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("barbers_ledger")

app = FastAPI(title="Mind at Ease Solutions — Books API")
api_router = APIRouter(prefix="/api")


@api_router.get("/healthz")
async def healthz():
    return {"status": "ok"}


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
          invites.router, receipts.router, mileage.router, collections.router,
          simulator.router, subcompliance.router, cpa.router, pulse.router,
          pricing.router, taxforecast.router, playground.router, combo.router,
          kpis.router, benchmarks.router, voice.router, taxposture.router,
          whitelabel.router, entities.router, cleanup.router,
          purchase_orders.router, fx.router, fx.currencies_router,
          market_packs.router, cfdi.router):
    api_router.include_router(r)
api_router.include_router(plans.router)

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
# --- security-hardening (2026-10-16): CSP middleware, strictly additive block.
# Keep this block intact; other agents may add their own middleware above/below.
app.add_middleware(ContentSecurityPolicyMiddleware)
# --- end security-hardening


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
        (db.rates, [("base", 1), ("quote", 1), ("date", 1)], {"unique": True}),
    ]
    for coll, keys, opts in indexes:
        try:
            await coll.create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"Index on {coll.name} skipped: {e}")
    try:
        from lib.currency_migration import backfill_currency_fields
        report = await backfill_currency_fields(db)
        stamped = sum(report.values())
        if stamped:
            logger.info(f"Currency backfill stamped {stamped} doc(s): {report}")
    except Exception as e:
        logger.warning(f"Currency backfill skipped: {e}")
    try:
        from lib import scheduler as _sched
        await _sched.start()
    except Exception as e:
        logger.warning(f"Scheduler not started: {e}")
    logger.info("Barber's Ledger started; indexes ensured.")


@app.on_event("shutdown")
async def shutdown():
    try:
        from lib import scheduler as _sched
        await _sched.stop()
    except Exception:
        pass
    get_client().close()
