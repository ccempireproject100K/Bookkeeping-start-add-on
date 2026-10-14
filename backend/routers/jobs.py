"""HVAC Job Costing engine — jobs, estimates, cost tracking, change orders, profitability.
Every cost/revenue posts exactly ONE journal entry into the shared double-entry engine (no duplicates).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/jobs", tags=["jobs"])

CATEGORY_ACCOUNT = {
    "material": "5000", "labor": "5005", "equipment": "5006",
    "subcontractor": "5007", "other": "5008",
}
CATEGORIES = list(CATEGORY_ACCOUNT.keys())


def jobs(user):
    return Scoped(db.jobs, user["tenant_id"])


def jobcosts(user):
    return Scoped(db.job_costs, user["tenant_id"])


# ---------------- Profitability ----------------
async def _profit(user, job):
    tenant_id = user["tenant_id"]
    costs = await jobcosts(user).find_list({"job_id": job["id"]})
    actual_by_cat = {c: 0 for c in CATEGORIES}
    for c in costs:
        actual_by_cat[c["category"]] = actual_by_cat.get(c["category"], 0) + c["amount_cents"]
    actual_cost = sum(actual_by_cat.values())

    invoices = await Scoped(db.invoices, tenant_id).find_list({"job_id": job["id"]})
    actual_revenue = sum(i.get("subtotal_cents", 0) for i in invoices)

    # estimate incl. approved change orders
    est_rev = job.get("estimated_revenue_cents", 0)
    est_cost = sum(job.get("estimated_costs_cents", {}).values())
    for co in job.get("change_orders", []):
        if co.get("approved"):
            est_rev += co.get("revenue_delta_cents", 0)
            est_cost += co.get("cost_delta_cents", 0)

    gp = actual_revenue - actual_cost
    margin = round(gp / actual_revenue * 100, 2) if actual_revenue else 0.0
    est_gp = est_rev - est_cost
    est_margin = round(est_gp / est_rev * 100, 2) if est_rev else 0.0
    return {
        "actual_revenue_cents": actual_revenue, "actual_cost_cents": actual_cost,
        "actual_by_category": {k: cents_to_dollars(v) for k, v in actual_by_cat.items()},
        "actual_by_category_cents": actual_by_cat,
        "gross_profit_cents": gp, "gross_margin_pct": margin,
        "estimated_revenue_cents": est_rev, "estimated_cost_cents": est_cost,
        "estimated_gp_cents": est_gp, "estimated_margin_pct": est_margin,
        "cost_variance_cents": actual_cost - est_cost,
        "revenue": cents_to_dollars(actual_revenue), "cost": cents_to_dollars(actual_cost),
        "gross_profit": cents_to_dollars(gp),
        "estimated_revenue": cents_to_dollars(est_rev), "estimated_cost": cents_to_dollars(est_cost),
        "is_unprofitable": gp < 0,
        "invoice_count": len(invoices),
    }


# ---------------- CRUD ----------------
class JobBody(BaseModel):
    customer_id: Optional[str] = None
    customer_name: str
    service_location: str = ""
    job_type: str = "installation"        # installation | maintenance | repair
    description: str = ""
    estimated_revenue: float = 0
    estimated_costs: dict = {}            # {material, labor, equipment, subcontractor, other} dollars


@router.get("")
async def list_jobs(user: dict = Depends(get_current_user)):
    rows = await jobs(user).find_list(sort=[("created_at", -1)], limit=500)
    out = []
    for j in rows:
        p = await _profit(user, j)
        out.append({**j, "profit": p})
    return out


@router.get("/reports/profitability")
async def profitability_report(group_by: str = "job", user: dict = Depends(require("ledger:read"))):
    rows = await jobs(user).find_list(sort=[("created_at", -1)], limit=1000)
    enriched = [{**j, "profit": await _profit(user, j)} for j in rows]

    if group_by == "job":
        items = [{
            "key": j["job_number"], "label": f"{j['job_number']} — {j['customer_name']}",
            "revenue": j["profit"]["revenue"], "cost": j["profit"]["cost"],
            "gross_profit": j["profit"]["gross_profit"], "margin": j["profit"]["gross_margin_pct"],
            "unprofitable": j["profit"]["is_unprofitable"], "status": j["status"],
        } for j in enriched]
        return {"group_by": group_by, "items": items}

    buckets = {}
    tech_names = {}
    if group_by == "technician":
        team = await db.users.find({"tenant_id": user["tenant_id"]}, {"_id": 0, "id": 1, "name": 1}).to_list(200)
        tech_names = {t["id"]: t["name"] for t in team}
        costs = await jobcosts(user).find_list({"category": "labor"})
        for c in costs:
            tid = c.get("technician_id") or "unassigned"
            b = buckets.setdefault(tid, {"label": tech_names.get(tid, "Unassigned"), "cost_cents": 0, "hours": 0, "jobs": set()})
            b["cost_cents"] += c["amount_cents"]
            b["hours"] += c.get("hours", 0) or 0
            b["jobs"].add(c["job_id"])
        items = [{"key": k, "label": v["label"], "labor_cost": cents_to_dollars(v["cost_cents"]),
                  "hours": round(v["hours"], 1), "jobs": len(v["jobs"])} for k, v in buckets.items()]
        return {"group_by": group_by, "items": items}

    # customer or service_type
    field = "customer_name" if group_by == "customer" else "job_type"
    for j in enriched:
        key = j.get(field, "—")
        b = buckets.setdefault(key, {"revenue": 0, "cost": 0, "gp": 0, "count": 0})
        b["revenue"] += j["profit"]["actual_revenue_cents"]
        b["cost"] += j["profit"]["actual_cost_cents"]
        b["gp"] += j["profit"]["gross_profit_cents"]
        b["count"] += 1
    items = [{"key": k, "label": k, "revenue": cents_to_dollars(v["revenue"]),
              "cost": cents_to_dollars(v["cost"]), "gross_profit": cents_to_dollars(v["gp"]),
              "margin": round(v["gp"] / v["revenue"] * 100, 2) if v["revenue"] else 0.0,
              "count": v["count"]} for k, v in buckets.items()]
    return {"group_by": group_by, "items": items}


@router.get("/{job_id}")
async def get_job(job_id: str, user: dict = Depends(get_current_user)):
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    j = {k: v for k, v in j.items() if k != "_id"}
    j["profit"] = await _profit(user, j)
    j["costs"] = await jobcosts(user).find_list({"job_id": job_id}, sort=[("created_at", -1)])
    j["invoices"] = await Scoped(db.invoices, user["tenant_id"]).find_list({"job_id": job_id}, sort=[("created_at", -1)])
    return j


@router.post("")
async def create_job(body: JobBody, user: dict = Depends(require("ops:manage"))):
    seq = await Scoped(db.jobs, user["tenant_id"]).next_seq(db.counters, "job")
    est_costs = {c: to_cents(body.estimated_costs.get(c, 0)) for c in CATEGORIES}
    doc = {
        "id": new_id(), "job_number": f"JOB-{seq:04d}",
        "customer_id": body.customer_id, "customer_name": body.customer_name,
        "service_location": body.service_location, "job_type": body.job_type,
        "description": body.description, "status": "estimate",
        "estimated_revenue_cents": to_cents(body.estimated_revenue),
        "estimated_costs_cents": est_costs, "change_orders": [],
        "created_at": now_iso(),
    }
    await jobs(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


class StatusBody(BaseModel):
    status: str   # estimate | active | completed | closed


@router.patch("/{job_id}")
async def update_status(job_id: str, body: StatusBody, user: dict = Depends(require("ops:manage"))):
    if body.status not in ("estimate", "active", "completed", "closed"):
        raise HTTPException(status_code=422, detail="Invalid status")
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    await jobs(user).update_one({"id": job_id}, {"$set": {"status": body.status}})
    await L.audit(db, user["tenant_id"], user, "job.status", {"job": j["job_number"], "status": body.status})
    return {"status": body.status}


# ---------------- Costs ----------------
class CostBody(BaseModel):
    category: str
    description: str = ""
    amount: float = 0
    hours: float = 0
    rate: float = 0
    product_id: Optional[str] = None
    qty: int = 1
    technician_id: Optional[str] = None
    paid: bool = False    # True -> cash; False -> payable


@router.post("/{job_id}/costs")
async def add_cost(job_id: str, body: CostBody, user: dict = Depends(require("ops:manage"))):
    tenant_id = user["tenant_id"]
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    if j["status"] == "estimate":
        raise HTTPException(status_code=409, detail="Convert the estimate to an active job before logging costs")
    if body.category not in CATEGORY_ACCOUNT:
        raise HTTPException(status_code=422, detail="Invalid category")

    debit_code = CATEGORY_ACCOUNT[body.category]
    credit_code = "1020" if body.paid else "2010"
    hours = body.hours
    product_id = body.product_id

    if body.category == "material" and product_id:
        prod = await Scoped(db.products, tenant_id).find_one({"id": product_id})
        if not prod:
            raise HTTPException(status_code=404, detail="Product not found")
        res = await Scoped(db.products, tenant_id).update_one(
            {"id": product_id, "quantity_on_hand": {"$gte": body.qty}},
            {"$inc": {"quantity_on_hand": -body.qty}})
        if res.matched_count == 0:
            raise HTTPException(status_code=409, detail=f"{prod['name']} is out of stock")
        amount_cents = prod["unit_cost_cents"] * body.qty
        credit_code = "1300"   # relieve inventory
    elif body.category == "labor":
        amount_cents = to_cents(body.hours * body.rate) if body.hours else to_cents(body.amount)
        credit_code = "1020" if body.paid else "2320"   # wages payable
    else:
        amount_cents = to_cents(body.amount)

    if amount_cents <= 0:
        raise HTTPException(status_code=422, detail="Cost amount must be positive")

    cost_id = new_id()
    entry = await L.post_journal_entry(db, tenant_id, user, {
        "entry_date": today_str(), "source": "job_cost", "source_ref": cost_id,
        "memo": f"{j['job_number']} {body.category}: {body.description}".strip(),
        "lines": [P.dr(debit_code, amount_cents, body.description or body.category),
                  P.cr(credit_code, amount_cents, "Job cost")],
    }, status="posted")

    doc = {
        "id": cost_id, "job_id": job_id, "job_number": j["job_number"],
        "category": body.category, "description": body.description,
        "amount_cents": amount_cents, "hours": hours, "rate": to_cents(body.rate),
        "product_id": product_id, "qty": body.qty, "technician_id": body.technician_id,
        "entry_number": entry["number"], "created_at": now_iso(),
    }
    await jobcosts(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


# ---------------- Change orders ----------------
class ChangeOrderBody(BaseModel):
    description: str
    revenue_delta: float = 0
    cost_delta: float = 0
    approved: bool = True


@router.post("/{job_id}/change-orders")
async def add_change_order(job_id: str, body: ChangeOrderBody, user: dict = Depends(require("ops:manage"))):
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    co = {
        "id": new_id(), "description": body.description,
        "revenue_delta_cents": to_cents(body.revenue_delta),
        "cost_delta_cents": to_cents(body.cost_delta),
        "approved": body.approved, "created_at": now_iso(),
    }
    await jobs(user).update_one({"id": job_id}, {"$push": {"change_orders": co}})
    return co


# ---------------- Job invoice (revenue) ----------------
class JobInvoiceBody(BaseModel):
    amount: float
    tax: float = 0


@router.post("/{job_id}/invoice")
async def invoice_job(job_id: str, body: JobInvoiceBody, user: dict = Depends(require("ops:manage"))):
    tenant_id = user["tenant_id"]
    j = await jobs(user).find_one({"id": job_id})
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    subtotal = to_cents(body.amount)
    tax_cents = to_cents(body.tax)
    gross = subtotal + tax_cents
    inv_id = new_id()
    entry = await L.post_journal_entry(db, tenant_id, user, P.build_invoice_issue(
        revenue_lines=[{"revenue_code": "4300", "amount_cents": subtotal}],
        tax_cents=tax_cents, cogs_cents=0, gross_cents=gross,
        entry_date=today_str(), source_ref=inv_id, memo=f"{j['job_number']} — {j['customer_name']}",
    ), status="posted")
    doc = {
        "id": inv_id, "job_id": job_id, "client_name": j["customer_name"],
        "lines": [{"kind": "job", "name": f"{j['job_number']} contract", "revenue_code": "4300", "amount": body.amount}],
        "subtotal_cents": subtotal, "tax_cents": tax_cents, "total_cents": gross,
        "status": "issued", "due_date": None, "issue_entry": entry["number"],
        "paid_entry": None, "created_at": now_iso(),
    }
    await Scoped(db.invoices, tenant_id).insert_one(doc)
    doc.pop("_id", None)
    return doc
