from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

import os
import logging
from fastapi import FastAPI, APIRouter
from starlette.middleware.cors import CORSMiddleware

from db import db, get_client
from routers import (auth, ledger, products, pos, invoices, cashclose, dashboard,
                     bills, aging, giftcards, commissions, customers, jobs)

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
          customers.router, jobs.router):
    api_router.include_router(r)

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.accounts.create_index([("tenant_id", 1), ("code", 1)], unique=True)
    await db.journal_entries.create_index([("tenant_id", 1), ("status", 1), ("entry_date", 1)])
    await db.journal_lines.create_index([("tenant_id", 1), ("account_code", 1), ("entry_date", 1)])
    await db.fiscal_periods.create_index([("tenant_id", 1), ("key", 1)], unique=True)
    logger.info("Barber's Ledger started; indexes ensured.")


@app.on_event("shutdown")
async def shutdown():
    get_client().close()
