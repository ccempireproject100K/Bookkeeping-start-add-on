"""Recurring operating expenses (rent, booth rent, subscriptions). Owners define a monthly template;
the platform cron posts the expense once per month on/after its day. Each posting books to the journal
like any expense (via create_txn)."""

import os
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from lib.audit import audit
from lib.auth import Principal, principal_for_user, require
from lib.dates import now_iso, today_iso
from lib.db import db
from lib.money import create_txn, money
from lib.repo import Scoped

router = APIRouter()


class RecurringIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=80)
    amount: float = Field(gt=0)
    payment_method: str = "bank"
    vendor_id: str | None = None
    day_of_month: int = Field(default=1, ge=1, le=28)
    active: bool = True


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "tenant_id"}


@router.get("/recurring")
async def list_recurring(p: Principal = Depends(require("txn:read"))):
    return [_strip(d) for d in await Scoped("recurring", p).find({}, sort=[("name", 1)])]


@router.post("/recurring")
async def create_recurring(body: RecurringIn, p: Principal = Depends(require("txn:write"))):
    doc = {"id": str(uuid.uuid4()), **body.model_dump(), "amount": money(body.amount), "last_run": None, "created_at": now_iso()}
    await Scoped("recurring", p).insert(doc)
    await audit(p, "recurring.create", "recurring", doc["id"], {"name": body.name, "amount": doc["amount"]})
    return _strip(doc)


@router.delete("/recurring/{rid}")
async def delete_recurring(rid: str, p: Principal = Depends(require("txn:write"))):
    if not await Scoped("recurring", p).delete({"id": rid}):
        raise HTTPException(404, "Not found")
    return {"ok": True}


async def _post_recurring(p: Principal, r: dict, day: str) -> None:
    vendor_name = None
    if r.get("vendor_id"):
        v = await Scoped("vendors", p).find_one({"id": r["vendor_id"]})
        vendor_name = v["name"] if v else None
    await create_txn(p, "expense", r["category"], r["amount"], day, r["name"], payment_method=r.get("payment_method", "bank"),
                     capitalized=False, extra={"vendor_id": r.get("vendor_id"), "vendor_name": vendor_name, "recurring_id": r["id"]})
    await Scoped("recurring", p).update({"id": r["id"]}, {"$set": {"last_run": day[:7]}})


@router.post("/recurring/{rid}/run")
async def run_recurring_now(rid: str, p: Principal = Depends(require("txn:write"))):
    r = await Scoped("recurring", p).find_one({"id": rid})
    if not r:
        raise HTTPException(404, "Not found")
    day = today_iso(p.settings.get("timezone"))
    await _post_recurring(p, r, day)
    await audit(p, "recurring.run", "recurring", rid, {"amount": r["amount"]})
    return {"ok": True, "posted": r["amount"]}


@router.post("/cron/recurring")
async def cron_recurring(authorization: str | None = Header(default=None)):
    """Platform cron: post every active recurring expense whose day has arrived and that hasn't run this month."""
    secret = os.environ.get("WEBHOOK_CRON_SECRET", "")
    if not secret or authorization != f"Bearer {secret}":
        raise HTTPException(401, "Unauthorized")
    posted = 0
    today = today_iso("UTC")
    this_month, dom = today[:7], int(today[8:10])
    async for r in db.recurring.find({"active": True, "day_of_month": {"$lte": dom}, "last_run": {"$ne": this_month}}):
        user = await db.users.find_one({"tenant_id": r["tenant_id"], "role": "admin"}, {"_id": 0}, sort=[("created_at", 1)])
        p = await principal_for_user(user) if user else None
        if not p:
            continue
        try:
            await _post_recurring(p, r, today_iso(p.settings.get("timezone")))
            posted += 1
        except Exception:
            continue
    return {"status": "ok", "posted": posted}
