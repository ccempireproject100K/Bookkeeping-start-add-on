"""Procurement sync (AI Office -> Books): supplier bills into AP + job costs.

Addendum 2026-11-05 to INTEGRATION_CONTRACT.md (v1.1, see the addendum
section in that file). AI Office's parts/procurement system pushes a
supplier bill here; Books lands it as an open accounts-payable bill
(posting exactly ONE journal entry, the bill's) and, for lines linked
to a job, records job_costs rows that reference that same entry. The
materials spend therefore feeds Books job costing without a second
posting — the jobs module's rule ("every cost/revenue posts exactly
ONE journal entry") is preserved: the job_costs rows carry the bill's
entry number and post nothing of their own.

Auth and idempotency follow the frozen sync surface exactly
(lib/integration_auth.get_sync_actor + lib/idempotency, the same
pattern as routers/customers.py create_customer).
"""
import hashlib
from collections import OrderedDict
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib import idempotency as idem
from lib import ledger as L
from lib import posting as P
from lib.integration_auth import actor_label, get_sync_actor

router = APIRouter(prefix="/integrations/procurement", tags=["integrations"])

SOURCE_DEFAULT = "aioffice"
JOB_MATERIAL_CODE = "5000"   # jobs.CATEGORY_ACCOUNT["material"]
DEFAULT_EXPENSE_CODE = "5700"  # bills.py default


def _norm_name(s):
    """Normalize a vendor name for comparison: trim, collapse
    whitespace, lowercase — same rule as customers._norm_name."""
    return " ".join((s or "").split()).lower()


def _bills(user):
    return Scoped(db.bills, user["tenant_id"])


def _vendors(user):
    return Scoped(db.vendors, user["tenant_id"])


def _jobs(user):
    return Scoped(db.jobs, user["tenant_id"])


def _jobcosts(user):
    return Scoped(db.job_costs, user["tenant_id"])


class ProcurementBillLine(BaseModel):
    description: str = ""
    amount: float
    expense_code: Optional[str] = None
    job_ref: Optional[str] = None
    job_number: Optional[str] = None
    customer_name: Optional[str] = None


class ProcurementBillBody(BaseModel):
    source: str = SOURCE_DEFAULT
    source_bill_id: str
    source_po_id: Optional[str] = None
    supplier_invoice_no: Optional[str] = None
    vendor_name: str
    vendor_external_id: Optional[str] = None
    vendor_is_1099: bool = False
    bill_date: Optional[str] = None
    due_date: Optional[str] = None
    lines: List[ProcurementBillLine]


async def _resolve_vendor(user, body: ProcurementBillBody) -> dict:
    """Match by external_ref first, else by normalized name, else create.

    A matched vendor's 1099 flag is only ever set (never unset) when the
    payload asserts it. New fields follow the task contract
    (``external_ref``, ``form_1099``, ``kind``) while also writing the
    vendor module's canonical flag name (``is_1099``, routers/vendors.py)
    so existing 1099 consumers keep seeing the vendor.
    """
    vendors = _vendors(user)
    vendor = None
    if body.vendor_external_id:
        cand = await vendors.find_one({"external_ref": body.vendor_external_id})
        if cand and not cand.get("deleted_at"):
            vendor = cand
    if vendor is None:
        target = _norm_name(body.vendor_name)
        for cand in await vendors.find_list(limit=500):
            if cand.get("deleted_at"):
                continue
            if _norm_name(cand.get("name")) == target:
                vendor = cand
                break
    if vendor is None:
        doc = {
            "id": new_id(), "name": body.vendor_name,
            "email": None, "phone": None, "address": None,
            "kind": "supplier",
            "external_ref": body.vendor_external_id,
            "is_1099": bool(body.vendor_is_1099),
            "form_1099": bool(body.vendor_is_1099),
            "default_expense_code": DEFAULT_EXPENSE_CODE,
            "tax_id_enc": "", "tax_id_last4": "",
            "source": "aioffice_procurement",
            "created_at": now_iso(),
        }
        await vendors.insert_one(doc)
        return doc
    updates = {}
    if body.vendor_external_id and not vendor.get("external_ref"):
        updates["external_ref"] = body.vendor_external_id
    if body.vendor_is_1099 and not (vendor.get("is_1099") or vendor.get("form_1099")):
        updates["is_1099"] = True
        updates["form_1099"] = True
    if updates:
        await vendors.update_one({"id": vendor["id"]}, {"$set": updates})
        vendor = {**vendor, **updates}
    return vendor


async def _resolve_job(user, job_ref: str, job_number, customer_name) -> dict:
    """Resolve a Books job by external_ref, else create a minimal shell."""
    jobs = _jobs(user)
    job = await jobs.find_one({"external_ref": job_ref})
    if job:
        return job
    number = job_number or ("EXT-" + hashlib.sha256(
        job_ref.encode("utf-8")).hexdigest()[:10].upper())
    clash = await jobs.find_one({"job_number": number})
    if clash and clash.get("external_ref") != job_ref:
        number = f"{number}-" + hashlib.sha256(
            job_ref.encode("utf-8")).hexdigest()[:6].upper()
    doc = {
        "id": new_id(), "job_number": number,
        "customer_id": None, "customer_name": customer_name or "AI Office job",
        "service_location": "", "job_type": "installation",
        "description": "", "status": "active",
        "estimated_revenue_cents": 0, "estimated_costs_cents": {},
        "contract_amount_cents": 0, "change_orders": [],
        "external_ref": job_ref, "source": "aioffice_procurement",
        "created_at": now_iso(),
    }
    await jobs.insert_one(doc)
    return doc


def _response_for_bill(bill: dict, job_cost_rows: list, deduplicated: bool) -> dict:
    return {
        "bill_id": bill["id"],
        "entry_number": bill.get("entry_number") or bill.get("issue_entry"),
        "total_cents": bill["amount_cents"],
        "vendor_id": bill.get("vendor_id"),
        "job_costs": [
            {"job_id": r["job_id"], "job_cost_id": r["id"],
             "amount_cents": r["amount_cents"]} for r in job_cost_rows],
        "deduplicated": deduplicated,
    }


async def _existing_response(user, bill: dict) -> dict:
    rows = await _jobcosts(user).find_list({"bill_id": bill["id"]})
    return _response_for_bill(bill, rows, deduplicated=True)


@router.post("/bills")
async def create_procurement_bill(body: ProcurementBillBody,
                                  user: dict = Depends(get_sync_actor),
                                  request: Request = None):
    # Sync surface: same pattern as customers.create_customer — an
    # ops:manage user session OR a books:sync integration credential,
    # with Idempotency-Key dedupe when the caller sends one.
    idem_key = request.headers.get("Idempotency-Key") if request else None
    sess = await idem.begin(
        tenant_id=user["tenant_id"], key=idem_key,
        endpoint="POST /integrations/procurement/bills",
        req_hash=idem.request_hash(
            "POST",
            request.url.path if request else "/api/integrations/procurement/bills",
            body.model_dump()),
        actor=actor_label(user))
    if sess.is_replay:
        return sess.replay_response()
    try:
        if not body.source_bill_id:
            raise HTTPException(status_code=422, detail="source_bill_id is required")
        if not body.lines:
            raise HTTPException(status_code=422, detail="At least one line is required")
        if idem_key:
            # Crash-window guard: a previous attempt with this key
            # created the bill but its response was never recorded.
            stamped = await _bills(user).find_one({"sync_key": idem_key})
            if stamped:
                out = await _existing_response(user, stamped)
                return await sess.complete(out, resource_id=stamped["id"])
        if not body.lines or all(to_cents(ln.amount) <= 0 for ln in body.lines):
            # Polish fix (2026-11-05, Claude #7's Books half): a zero-line
            # or zero-total bill never lands as an AP bill with a
            # zero-value journal entry. (Per-line non-positive amounts
            # are still rejected individually below; this catches the
            # all-zero / empty shapes.)
            raise HTTPException(
                status_code=422,
                detail="Bill has no positive lines; zero-total bills are not recorded")
        # Natural dedupe anchor: one Books bill per (source, source_bill_id).
        existing = await _bills(user).find_one(
            {"source": body.source, "source_bill_id": body.source_bill_id})
        if existing:
            out = await _existing_response(user, existing)
            return await sess.complete(out, resource_id=existing["id"])

        bdate = body.bill_date or today_str()
        try:
            date.fromisoformat(bdate)
            if body.due_date:
                date.fromisoformat(body.due_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="bill_date/due_date must be YYYY-MM-DD")

        vendor = await _resolve_vendor(user, body)

        line_items = []
        for ln in body.lines:
            amount_cents = to_cents(ln.amount)
            if amount_cents <= 0:
                raise HTTPException(status_code=422, detail="Line amounts must be positive")
            code = ln.expense_code or (JOB_MATERIAL_CODE if ln.job_ref
                                       else DEFAULT_EXPENSE_CODE)
            line_items.append({
                "expense_code": code, "description": ln.description,
                "amount_cents": amount_cents,
                "job_ref": ln.job_ref, "job_number": ln.job_number,
                "customer_name": ln.customer_name,
            })
        total = sum(li["amount_cents"] for li in line_items)

        # ONE journal entry for the whole spend — the bill's (bills.py
        # discipline): Dr per-line expense / Cr 2010 Accounts payable.
        bill_id = new_id()
        jl = [P.dr(li["expense_code"], li["amount_cents"], li["description"])
              for li in line_items]
        jl.append(P.cr("2010", total, "Accounts payable"))
        entry = await L.post_journal_entry(db, user["tenant_id"], user, {
            "entry_date": bdate, "source": "bill", "source_ref": bill_id,
            "memo": f"Bill — {body.vendor_name}", "lines": jl,
        }, status="posted")

        doc = {
            "id": bill_id, "kind": "procurement",
            "source": body.source, "source_bill_id": body.source_bill_id,
            "source_po_id": body.source_po_id,
            "supplier_invoice_no": body.supplier_invoice_no,
            "bill_number": body.supplier_invoice_no,
            "vendor": body.vendor_name, "vendor_id": vendor["id"],
            "amount_cents": total, "currency": "USD",
            "expense_code": line_items[0]["expense_code"],
            "line_items": line_items,
            "bill_date": bdate, "due_date": body.due_date, "status": "open",
            "paid_cents": 0, "payments": [],
            "recurring": False, "frequency": None,
            "issue_entry": entry["number"], "entry_number": entry["number"],
            "created_at": now_iso(),
        }
        if idem_key:
            doc["sync_key"] = idem_key
        await _bills(user).insert_one(doc)
        doc.pop("_id", None)

        # Job costs: one row per job_ref group, referencing the bill's
        # single entry — NO additional journal entry is posted.
        groups = OrderedDict()
        for ln in body.lines:
            if not ln.job_ref:
                continue
            g = groups.setdefault(ln.job_ref, {
                "amount_cents": 0, "job_number": ln.job_number,
                "customer_name": ln.customer_name})
            g["amount_cents"] += to_cents(ln.amount)
        job_cost_rows = []
        for job_ref, g in groups.items():
            job = await _resolve_job(user, job_ref, g["job_number"],
                                     g["customer_name"])
            row = {
                "id": new_id(), "job_id": job["id"],
                "job_number": job["job_number"],
                "category": "material",
                "description": (f"Supplier bill "
                                f"{body.supplier_invoice_no or body.source_bill_id}"
                                f" — {body.vendor_name}"),
                "amount_cents": g["amount_cents"],
                "date": bdate, "bill_id": bill_id,
                "entry_number": entry["number"],
                "vendor_name": body.vendor_name,
                "source": "aioffice_procurement",
                "external_ref": job_ref,
                "hours": 0, "rate": 0, "product_id": None, "qty": 1,
                "technician_id": None,
                "created_at": now_iso(),
            }
            await _jobcosts(user).insert_one(row)
            row.pop("_id", None)
            job_cost_rows.append(row)

        out = _response_for_bill(doc, job_cost_rows, deduplicated=False)
        return await sess.complete(out, resource_id=bill_id)
    except HTTPException as e:
        await sess.fail(e.status_code, str(e.detail))
        raise
