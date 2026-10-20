"""Vendors, direct expenses (with receipt metadata), and vendor credits.
All expense/credit events post exactly one balanced journal entry into the shared ledger.

1099 reporting lives in ONE place: the tax module (routers/tax.py) and its
contractor records. ``GET /vendors/1099`` below is a thin adapter over that
system kept for the Expenses UI — it does not compute its own totals.

Vendor tax IDs follow the same rule as contractor tax IDs in tax.py: Fernet-
encrypted at rest (``tax_id_enc``), only the last 4 (``tax_id_last4``) stored
in cleartext, write-only through the API. No response ever returns a full
TIN. Vendor docs written before this change hold a plaintext ``tax_id``;
reads tolerate that, expose only the mask, and lazily re-encrypt in place
(the owner-only POST /auth/migrate/encrypt-pii sweep covers them too)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib.coa import ACCOUNT_TYPE_OF, NAME_OF
from lib.pii import last4, masked, pii_encrypt
from lib import ledger as L
from lib import posting as P

router = APIRouter(prefix="/vendors", tags=["vendors"])


# ---------------- vendor TIN handling (mirrors routers/tax.py) ----------------
def _tin_fields(tax_id: Optional[str]) -> dict:
    """Encrypt a vendor TIN for storage; only the last 4 is kept in cleartext."""
    tax_id = (tax_id or "").strip()
    if not tax_id:
        return {"tax_id_enc": "", "tax_id_last4": ""}
    return {"tax_id_enc": pii_encrypt(tax_id), "tax_id_last4": last4(tax_id)}


def _tin_last4(v: dict) -> str:
    """Last 4 of a vendor's TIN, from the canonical field or a legacy
    plaintext ``tax_id`` on pre-encryption docs."""
    return v.get("tax_id_last4") or last4(v.get("tax_id") or "")


def public_vendor(v: dict) -> dict:
    """Response shape for a vendor doc: full TIN and ciphertext never leave
    the server — only ``tax_id_last4`` / ``tax_id_masked`` do."""
    out = {k: val for k, val in v.items() if k not in ("tax_id", "tax_id_enc")}
    l4 = _tin_last4(v)
    out["tax_id_last4"] = l4
    out["tax_id_masked"] = masked(l4)
    return out


async def _migrate_legacy_tin(v: dict) -> dict:
    """Best-effort lazy migration: a pre-encryption doc holding a plaintext
    ``tax_id`` is re-encrypted in place and the plaintext unset. Never fails
    a read — if the encryption key is unavailable the doc is returned as-is
    (public_vendor still masks it) and a later read or the auth migration
    sweep retries."""
    plain = (v.get("tax_id") or "").strip()
    if not plain:
        return v
    try:
        fields = _tin_fields(plain)
    except HTTPException:
        return v
    try:
        await db.vendors.update_one(
            {"id": v.get("id"), "tenant_id": v.get("tenant_id")},
            {"$set": fields, "$unset": {"tax_id": ""}})
    except Exception:
        pass
    out = {**v, **fields}
    out.pop("tax_id", None)
    return out


def vendors(user):
    return Scoped(db.vendors, user["tenant_id"])


def expenses(user):
    return Scoped(db.expenses, user["tenant_id"])


def credits(user):
    return Scoped(db.vendor_credits, user["tenant_id"])


# ---------------- Vendors ----------------
class VendorBody(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    is_1099: bool = False
    tax_id: Optional[str] = None
    default_expense_code: str = "5700"


@router.get("")
async def list_vendors(user: dict = Depends(get_current_user)):
    rows = await vendors(user).find_list(sort=[("name", 1)], limit=500)
    return [public_vendor(await _migrate_legacy_tin(v)) for v in rows]


@router.post("")
async def create_vendor(body: VendorBody, user: dict = Depends(require("ops:manage"))):
    data = body.model_dump()
    tax_id = data.pop("tax_id", None)
    doc = {"id": new_id(), **data, **_tin_fields(tax_id), "created_at": now_iso()}
    await vendors(user).insert_one(doc)
    doc.pop("_id", None)
    return public_vendor(doc)


@router.patch("/{vendor_id}")
async def update_vendor(vendor_id: str, body: VendorBody, user: dict = Depends(require("ops:manage"))):
    v = await vendors(user).find_one({"id": vendor_id})
    if not v:
        raise HTTPException(status_code=404, detail="Vendor not found")
    data = body.model_dump()
    tax_id = data.pop("tax_id", None)
    if (tax_id or "").strip():
        # Only overwrite the encrypted TIN when a new one is supplied
        # (same write-only rule as contractor tax IDs in tax.py).
        data.update(_tin_fields(tax_id))
    elif v.get("tax_id"):
        # Form submitted without a TIN: keep the stored one, but if it is a
        # legacy plaintext value, encrypt it rather than leaving it behind.
        data.update(_tin_fields(v["tax_id"]))
        await vendors(user).update_one({"id": vendor_id}, {"$unset": {"tax_id": ""}})
    await vendors(user).update_one({"id": vendor_id}, {"$set": data})
    fresh = await vendors(user).find_one({"id": vendor_id})
    return public_vendor(fresh or {"id": vendor_id, **data})


@router.delete("/{vendor_id}")
async def delete_vendor(vendor_id: str, force: bool = False,
                        user: dict = Depends(require("ops:manage"))):
    """Delete a vendor. Refused (409) while the vendor still has bills or recorded
    expenses, unless ?force=true. Expenses already recorded keep their vendor name."""
    v = await vendors(user).find_one({"id": vendor_id})
    if not v:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if not force:
        q = {"$or": [{"vendor_id": vendor_id}, {"vendor": v["name"]}]}
        bill_refs = await Scoped(db.bills, user["tenant_id"]).count(q)
        exp_refs = await expenses(user).count(q)
        if bill_refs or exp_refs:
            raise HTTPException(
                status_code=409,
                detail=(f"Vendor has {bill_refs} bill(s) and {exp_refs} expense(s) on record. "
                        "Delete anyway? Re-submit with ?force=true."),
            )
    await db.vendors.delete_one({"id": vendor_id, "tenant_id": user["tenant_id"]})
    return {"ok": True, "deleted_id": vendor_id}


# ---------------- Direct expenses ----------------
class ReceiptMeta(BaseModel):
    merchant: Optional[str] = None
    receipt_date: Optional[str] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    filename: Optional[str] = None


class ExpenseBody(BaseModel):
    vendor: str
    vendor_id: Optional[str] = None
    category_code: str = "5700"
    amount: float
    payment_method: str = "bank"          # cash | card | bank
    expense_date: Optional[str] = None
    description: str = ""
    billable_job_id: Optional[str] = None
    receipt: Optional[ReceiptMeta] = None


@router.get("/expenses/list")
async def list_expenses(user: dict = Depends(require("ledger:read"))):
    return await expenses(user).find_list(sort=[("created_at", -1)], limit=500)


@router.post("/expenses")
async def create_expense(body: ExpenseBody, user: dict = Depends(require("ops:manage"))):
    if body.category_code not in ACCOUNT_TYPE_OF:
        raise HTTPException(status_code=422, detail="Unknown category account")
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    cash = P._cash_account(body.payment_method)
    exp_id = new_id()
    edate = body.expense_date or today_str()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": edate, "source": "expense", "source_ref": exp_id,
        "memo": f"Expense — {body.vendor}: {body.description}".strip(),
        "lines": [P.dr(body.category_code, amount, body.description or body.vendor),
                  P.cr(cash, amount, "Paid")],
    }, status="posted")
    doc = {
        "id": exp_id, "vendor": body.vendor, "vendor_id": body.vendor_id,
        "category_code": body.category_code, "category_name": NAME_OF.get(body.category_code),
        "amount_cents": amount, "payment_method": body.payment_method, "expense_date": edate,
        "description": body.description, "billable_job_id": body.billable_job_id,
        "receipt": body.receipt.model_dump() if body.receipt else None,
        "entry_number": entry["number"], "created_at": now_iso(),
    }
    await expenses(user).insert_one(doc)
    # if tied to a job, record it as a job cost too (no extra GL posting; cost already hit the expense account)
    if body.billable_job_id:
        job = await Scoped(db.jobs, user["tenant_id"]).find_one({"id": body.billable_job_id})
        if job:
            await Scoped(db.job_costs, user["tenant_id"]).insert_one({
                "id": new_id(), "job_id": body.billable_job_id, "job_number": job["job_number"],
                "category": "other", "description": f"{body.vendor}: {body.description}",
                "amount_cents": amount, "hours": 0, "rate": 0, "product_id": None, "qty": 1,
                "technician_id": None, "entry_number": entry["number"], "created_at": now_iso(),
            })
    doc.pop("_id", None)
    return doc


# ---------------- Vendor credits ----------------
class CreditBody(BaseModel):
    vendor: str
    vendor_id: Optional[str] = None
    amount: float
    expense_code: str = "5700"
    memo: str = ""


@router.get("/credits/list")
async def list_credits(user: dict = Depends(require("ledger:read"))):
    return await credits(user).find_list(sort=[("created_at", -1)], limit=300)


@router.post("/credits")
async def create_credit(body: CreditBody, user: dict = Depends(require("ops:manage"))):
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    cid = new_id()
    # reduce AP, reverse the expense (vendor owes you / future offset)
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": today_str(), "source": "vendor_credit", "source_ref": cid,
        "memo": f"Vendor credit — {body.vendor}: {body.memo}".strip(),
        "lines": [P.dr("2010", amount, "AP reduced by credit"),
                  P.cr(body.expense_code, amount, "Expense reversed")],
    }, status="posted")
    doc = {"id": cid, "vendor": body.vendor, "vendor_id": body.vendor_id,
           "amount_cents": amount, "remaining_cents": amount, "expense_code": body.expense_code,
           "memo": body.memo, "entry_number": entry["number"], "created_at": now_iso()}
    await credits(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


# ---------------- 1099 report ----------------
@router.get("/1099")
async def report_1099(year: str, user: dict = Depends(require("ledger:read"))):
    """1099 totals — served from the ONE canonical 1099 system, the tax
    module's contractor records (routers/tax.py): totals are that module's
    recorded contractor payments for the year, the $600 threshold and the
    exempt-entity rule come from tax.py's ``_strip``, and TINs are already
    encrypted there — this adapter only ever exposes the mask.

    This endpoint previously ran a second, divergent computation over bills
    and direct expenses against plaintext vendor TINs; that path is removed.
    Response shape is unchanged for the Expenses UI."""
    from routers import tax as tax_router

    y = int(year)
    out = []
    for c in await tax_router.contractors(user).find_list(sort=[("name", 1)]):
        total = await tax_router._year_total_cents(user, c["id"], y)
        row = tax_router._strip(c, total)
        if total <= 0 and not row["needs_1099"]:
            continue
        out.append({"vendor_id": c["id"], "name": c["name"],
                    "tax_id_masked": masked(row["tax_id_last4"]),
                    "paid_cents": total, "paid": cents_to_dollars(total),
                    "reportable": row["needs_1099"]})
    out.sort(key=lambda x: -x["paid_cents"])
    return {"year": year, "threshold": cents_to_dollars(tax_router.THRESHOLD_CENTS),
            "vendors": out,
            "total": cents_to_dollars(sum(v["paid_cents"] for v in out))}
