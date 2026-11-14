"""Receipt inbox: snap or upload receipt photos, AI-extract the details in your
trade's language, match to the bank feed, and post as expenses.

Flow: POST /receipts/upload -> POST /receipts/{id}/scan -> review ->
POST /receipts/{id}/post. The AI step needs OPENAI_API_KEY; everything else
works without it (manual entry fallback).
"""
import asyncio
import base64
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

from db import db
from lib import receipt_storage
from lib.audit import audit
from lib.auth import require, get_current_user
from lib.pii import scrub_secrets
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib import ledger as L
from lib import posting as P
from lib import receiptmatch as RM
from lib import jobphases as JP
from lib.coa import ACCOUNT_TYPE_OF, NAME_OF
from lib.niches import get_pack
from routers.jobs import CATEGORY_ACCOUNT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/receipts", tags=["receipts"])

# ---------------- Job tagging (field capture, job-costing #3) ----------------
_ACCOUNT_TO_JOB_CATEGORY = {acct: cat for cat, acct in CATEGORY_ACCOUNT.items()}


def job_category_for_receipt(category_code: str, category_name: str = "") -> str:
    """Map a receipt's expense category onto a job-cost category.

    Direct hit when the expense account IS a job-cost account; otherwise a
    keyword read of the category name; otherwise 'other'. Receipts are
    overwhelmingly materials in the field, but an unrecognized category is
    honestly 'other' rather than a guessed 'material'.
    """
    if category_code in _ACCOUNT_TO_JOB_CATEGORY:
        return _ACCOUNT_TO_JOB_CATEGORY[category_code]
    n = (category_name or "").lower()
    if any(w in n for w in ("material", "suppl", "parts")):
        return "material"
    if "equipment" in n or "tool" in n:
        return "equipment"
    if "subcontract" in n:
        return "subcontractor"
    if "labor" in n or "labour" in n:
        return "labor"
    return "other"


async def _resolve_job(user, job_id):
    if not job_id:
        return None
    return await Scoped(db.jobs, user["tenant_id"]).find_one({"id": job_id})

UPLOAD_ROOT = Path(__file__).parent / "uploads" / "receipts"
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_BYTES = 10 * 1024 * 1024


def receipts(user):
    return Scoped(db.receipts, user["tenant_id"])


def _store():
    """The receipt file store: local disk today, Cloudflare R2 when
    BOOKS_STORAGE_BACKEND=r2 (lib/receipt_storage.py, STORAGE.md)."""
    return receipt_storage.get_receipt_storage()


def _key(user, stored: str) -> str:
    """Storage key for a receipt file — the tenant-scoped shape the local
    layout has always used (<tenant_id>/<stored>), on either backend."""
    return f"{user['tenant_id']}/{stored}"


async def _pack_for_user(user) -> dict:
    t = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    return get_pack(t.get("niche_id"))


def _trade_categories(pack: dict) -> list:
    """Expense account choices in the workspace's trade language."""
    cats = []
    for a in pack.get("trade_expense_accounts", []) or []:
        cats.append(f"{a['code']} {a['name']}")
    # plus the generic expense accounts everyone gets
    for code, name in sorted(NAME_OF.items()):
        if code.startswith("5") and ACCOUNT_TYPE_OF.get(code) == "expense":
            label = f"{code} {name}"
            if label not in cats:
                cats.append(label)
    return cats[:40]


# ---------------- Upload & inbox ----------------
async def ingest_receipt(user: dict, filename: str, content_type: str,
                         data: bytes, source: str = "upload",
                         job_id: str = None) -> dict:
    """Shared ingestion path: validate, store the file, create the inbox doc.

    Used by the manual upload endpoint and by the inbound-email webhook, so
    both flows file receipts identically. Raises HTTPException(422) on bad
    input.
    """
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(422, f"Unsupported file type: {content_type}. Use JPG, PNG, WEBP or PDF.")
    if job_id and not await _resolve_job(user, job_id):
        raise HTTPException(404, "Job not found")
    if len(data) > MAX_BYTES:
        raise HTTPException(422, "File too large (10 MB max).")
    if not data:
        raise HTTPException(422, "Empty file.")
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
           "application/pdf": "pdf"}[content_type]
    rid = new_id()
    stored = f"{rid}.{ext}"
    _store().save(_key(user, stored), data, content_type)
    doc = {"id": rid, "filename": filename or stored, "stored": stored,
           "content_type": content_type, "size_bytes": len(data),
           "status": "uploaded", "extracted": None, "expense_id": None,
           "source": source, "job_id": job_id or None, "created_at": now_iso()}
    await receipts(user).insert_one(doc)
    await audit(user, f"receipt.{source}", "receipt", rid, {"filename": doc["filename"]})
    doc.pop("_id", None)
    return doc


@router.post("/upload")
async def upload_receipt(file: UploadFile = File(...),
                         job_id: Optional[str] = Form(default=None),
                         user: dict = Depends(require("ops:manage"))):
    data = await file.read()
    return await ingest_receipt(user, file.filename, file.content_type, data,
                                source="upload", job_id=job_id)

class JobTagBody(BaseModel):
    job_id: Optional[str] = None     # null clears the tag
    phase_id: Optional[str] = None   # optional phase refinement of the tag


@router.post("/{rid}/job")
@router.patch("/{rid}/job-tag")
async def tag_receipt_job(rid: str, body: JobTagBody,
                          user: dict = Depends(require("ops:manage"))):
    """Tag (or untag) a receipt to a job from the review screen. The cost
    lands on the job when the receipt is posted; retagging after posting is
    not allowed (reverse the expense instead)."""
    doc = await receipts(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(404, "Receipt not found")
    if doc.get("status") == "posted":
        raise HTTPException(409, "Already posted — the job tag is locked")
    if body.job_id and not await _resolve_job(user, body.job_id):
        raise HTTPException(404, "Job not found")
    if body.phase_id:
        await _validate_job_phase(user, body.job_id, body.phase_id)
    await receipts(user).update_one(
        {"id": rid},
        {"$set": {"job_id": body.job_id or None, "phase_id": body.phase_id}})
    await audit(user, "receipt.job_tag", "receipt", rid,
                {"job_id": body.job_id, "phase_id": body.phase_id})
    doc["job_id"] = body.job_id or None
    doc["phase_id"] = body.phase_id
    return doc


@router.get("")
async def list_receipts(status: str = "", user: dict = Depends(require("ledger:read"))):
    q = {"status": status} if status else {}
    return await receipts(user).find_list(q, sort=[("created_at", -1)], limit=200)


@router.get("/{rid}")
async def get_receipt(rid: str, user: dict = Depends(require("ledger:read"))):
    doc = await receipts(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(404, "Receipt not found")
    return doc


@router.get("/{rid}/image")
async def receipt_image(rid: str, user: dict = Depends(require("ledger:read"))):
    doc = await receipts(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(404, "Receipt not found")
    store = _store()
    key = _key(user, doc["stored"])
    if not store.exists(key):
        raise HTTPException(404, "Image file missing")
    # Bytes are served by the app on either backend — the R2 bucket is
    # never public (same pattern as Trade Circle's /media route).
    filename = str(doc["filename"]).replace('"', "").replace("\r", "").replace("\n", "")
    return Response(content=store.read(key), media_type=doc["content_type"],
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.delete("/{rid}")
async def delete_receipt(rid: str, user: dict = Depends(require("ops:manage"))):
    doc = await receipts(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(404, "Receipt not found")
    if doc.get("status") == "posted":
        raise HTTPException(409, "Already posted as an expense — reverse the expense instead.")
    store = _store()
    key = _key(user, doc["stored"])
    if store.exists(key):
        store.delete(key)
    # Scoped has no delete() method — the house idiom is .c.delete_one
    # with an explicit tenant filter (cf. routers/banking.py). The old
    # receipts(user).delete(...) call raised AttributeError, so this
    # endpoint 500'd on main after removing the file; fixed here because
    # the delete flow is part of the storage layer's surface.
    await receipts(user).c.delete_one({"tenant_id": user["tenant_id"], "id": rid})
    await audit(user, "receipt.delete", "receipt", rid, {})
    return {"ok": True}


# ---------------- AI extraction ----------------
def _vision_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Receipt AI is off. Add OPENAI_API_KEY to the backend .env to enable scanning — "
                                 "or enter the details manually.")
    return key


@router.post("/{rid}/scan")
async def scan_receipt(rid: str, user: dict = Depends(require("ops:manage"))):
    """Send the receipt image to a vision model; store vendor/date/total/category."""
    import json
    doc = await receipts(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(404, "Receipt not found")
    if doc["content_type"] == "application/pdf":
        # Standout extra A: invoice2data template extraction on the PDF
        # text layer (MIT library, our templates). Only when no template
        # matches do we fall back to the old manual-entry answer.
        from lib.pdf_intake import extract_pdf_invoice
        found = extract_pdf_invoice(_store().read(_key(user, doc["stored"])))
        if not found:
            raise HTTPException(422, "No invoice template matched this PDF — enter the details manually, "
                                     "or upload a JPG/PNG photo for AI scan.")
        extracted = {"vendor": found["vendor"], "date": found["date"],
                     "amount": found["amount"], "tax": found["tax"],
                     "category_code": "", "category_name": "",
                     "description": f"PDF invoice {found['invoice_number']}".strip(),
                     "trade": "", "extraction_source": "invoice2data",
                     "needs_review": True}
        await receipts(user).update_one({"id": rid},
                                        {"$set": {"extracted": extracted, "status": "scanned"}})
        await audit(user, "receipt.scan", "receipt", rid,
                    {"vendor": extracted["vendor"], "amount": extracted["amount"],
                     "source": "invoice2data"})
        return extracted
    key = _vision_key()
    pack = await _pack_for_user(user)
    cats = _trade_categories(pack)
    trade = pack.get("name", "your trade")
    b64 = base64.b64encode(_store().read(_key(user, doc["stored"]))).decode()
    data_url = f"data:{doc['content_type']};base64,{b64}"
    prompt = (
        f"This is a business expense receipt for a {trade} business. Reply with ONLY compact JSON: "
        '{"vendor":string,"date":"YYYY-MM-DD","amount":number,"tax":number,'
        f'"category":one of [{", ".join(cats[:20])}],"description":string}}. '
        "Use the grand total for amount. Pick the category that best matches the trade language."
    )
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("RECEIPT_AI_MODEL", "gpt-4o-mini")
    payload = {"model": model, "max_tokens": 500, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}}]}]}
    def _do():
        return requests.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json=payload, timeout=60)

    try:
        r = await asyncio.to_thread(_do)
    except Exception as exc:
        logger.error("receipt vision request failed: %s", exc)
        raise HTTPException(502, "The AI service could not be reached — enter it manually.")
    if r.status_code != 200:
        logger.error("receipt vision error %s: %s", r.status_code, scrub_secrets(r.text[:300]))
        raise HTTPException(502, f"Vision API error ({r.status_code})")
    try:
        text = r.json()["choices"][0]["message"]["content"]
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Receipt read but could not be parsed — enter it manually.")
    code = (data.get("category") or "").split(" ")[0]
    extracted = {"vendor": (data.get("vendor") or "").strip(),
                 "date": (data.get("date") or "").strip(),
                 "amount": float(data.get("amount") or 0),
                 "tax": float(data.get("tax") or 0),
                 "category_code": code if code in ACCOUNT_TYPE_OF else "",
                 "category_name": NAME_OF.get(code, ""),
                 "description": (data.get("description") or "").strip(),
                 "trade": trade}
    await receipts(user).update_one({"id": rid},
                                    {"$set": {"extracted": extracted, "status": "scanned"}})
    await audit(user, "receipt.scan", "receipt", rid, {"vendor": extracted["vendor"], "amount": extracted["amount"]})
    return extracted


# ---------------- Bank-feed matching ----------------
async def claimed_bank_txn_ids(user, exclude_receipt_id=None) -> set:
    """Bank transactions already claimed by a receipt of this tenant —
    posted (matched_bank_txn), linked by the matcher (match_link), or
    recorded on an expense. One bank line can back exactly one receipt;
    this set is what enforces it."""
    ids = set()
    for r in await receipts(user).find_list({}, limit=5000):
        if exclude_receipt_id and r.get("id") == exclude_receipt_id:
            continue
        if r.get("status") == "posted" and r.get("matched_bank_txn"):
            ids.add(r["matched_bank_txn"])
        link = r.get("match_link") or {}
        if link.get("txn_id"):
            ids.add(link["txn_id"])
    for e in await Scoped(db.expenses, user["tenant_id"]).find_list(
            {}, limit=5000):
        if e.get("bank_transaction_id"):
            ids.add(e["bank_transaction_id"])
    return ids


async def scored_candidates(user, doc, claimed=None) -> list:
    """Every bank line inside the candidate window, scored by
    lib/receiptmatch (amount tiers + date distance + vendor similarity),
    best first. Transactions already claimed by another receipt are not
    offered again."""
    ex = doc.get("extracted") or {}
    amt = to_cents(ex.get("amount") or 0)
    rdate = (ex.get("date") or "")[:10]
    if not amt or not rdate:
        return []
    if claimed is None:
        claimed = await claimed_bank_txn_ids(
            user, exclude_receipt_id=doc.get("id"))
    txns = await Scoped(db.bank_transactions, user["tenant_id"]).find_list(
        {"txn_date": {"$gte": _shift(rdate, -RM.CANDIDATE_WINDOW_DAYS),
                      "$lte": _shift(rdate, RM.CANDIDATE_WINDOW_DAYS)}},
        sort=[("txn_date", -1)], limit=200)
    out = []
    for t in txns:
        if t["id"] in claimed:
            continue
        s = RM.score_candidate(ex, t)
        s.update({"id": t["id"], "txn_date": t["txn_date"],
                  "description": t.get("description", ""),
                  "account": t.get("bank_account_id", "")})
        out.append(s)
    out.sort(key=lambda s: s["score"], reverse=True)
    return out


@router.get("/{rid}/matches")
async def receipt_matches(rid: str, user: dict = Depends(require("ledger:read"))):
    """Bank transactions that could be this receipt, scored (amount tiers,
    date distance, vendor similarity) with a plain-words reason each.
    Extends the original exact-amount/+-4-day suggestion list."""
    doc = await receipts(user).find_one({"id": rid})
    if not doc or not doc.get("extracted"):
        raise HTTPException(404, "Scan the receipt first")
    scored = await scored_candidates(user, doc)
    return [s for s in scored if s["score"] >= 0.35][:20]


def _shift(iso_date: str, days: int) -> str:
    from datetime import date, timedelta
    d = date.fromisoformat(iso_date[:10])
    return (d + timedelta(days=days)).isoformat()


# ---------------- Post as expense ----------------
class PostBody(BaseModel):
    vendor: str = ""
    category_code: str = "5700"
    amount: float = 0
    expense_date: Optional[str] = None
    description: str = ""
    payment_method: str = "bank"
    bank_transaction_id: Optional[str] = None
    # Optional job / phase tag (job-costing asks #3/#4): when set — here
    # or pre-tagged on the receipt — the posted amount also lands in the
    # job's actuals under job_category, carrying the phase tag.
    job_id: Optional[str] = None
    phase_id: Optional[str] = None
    job_category: str = "material"


async def _validate_job_phase(user, job_id, phase_id):
    """Resolve a job tag (+ optional phase refinement) for receipt posting.
    Phase requires a job and must exist on that job."""
    if phase_id and not job_id:
        raise HTTPException(422, "phase_id requires a job_id")
    if not job_id:
        return None
    job = await Scoped(db.jobs, user["tenant_id"]).find_one({"id": job_id})
    if not job:
        raise HTTPException(422, "Job not found")
    if phase_id and not JP.phase_of(job, phase_id):
        raise HTTPException(422, "Phase not found on this job")
    return job



@router.post("/{rid}/post")
async def post_receipt(rid: str, body: PostBody, user: dict = Depends(require("ops:manage"))):
    """Turn a scanned receipt into a posted expense (one balanced journal entry).

    Field capture: when the receipt is job-tagged, the same amount also lands
    on the job's actuals as a job_costs row pointing at THIS expense's journal
    entry — still exactly one journal entry, no double count in the GL.
    """
    doc = await receipts(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(404, "Receipt not found")
    if doc.get("status") == "posted":
        raise HTTPException(409, "Already posted")
    if body.category_code not in ACCOUNT_TYPE_OF:
        raise HTTPException(422, "Unknown category account")
    amount = to_cents(body.amount)
    if amount <= 0:
        raise HTTPException(422, "Amount must be positive")
    # The bank transaction this expense claims: the one picked in the
    # review dialog, or the matcher's recorded link when none was picked.
    txn_id = body.bank_transaction_id or \
        (doc.get("match_link") or {}).get("txn_id")
    if txn_id:
        # FIX-IT-FIRST (receipt-matching screen, 2026-11-14): posting had
        # NO uniqueness check — the same bank transaction could be posted
        # against two receipts, double-counting the expense. One bank
        # line backs exactly one receipt.
        claimed = await claimed_bank_txn_ids(user, exclude_receipt_id=rid)
        if txn_id in claimed:
            raise HTTPException(
                409, "That bank transaction is already linked to another "
                     "posted receipt — unlink it there first, or pick a "
                     "different transaction.")
        txn = await Scoped(db.bank_transactions, user["tenant_id"]).find_one(
            {"id": txn_id})
        if not txn:
            raise HTTPException(422, "Bank transaction not found")

    job_id = body.job_id if body.job_id is not None else doc.get("job_id")
    phase_id = body.phase_id if body.phase_id is not None else doc.get("phase_id")
    job = await _validate_job_phase(user, job_id, phase_id) if job_id else None
    if job is not None and "job_category" in body.model_fields_set and \
            body.job_category not in ("material", "labor", "equipment",
                                      "subcontractor", "other"):
        raise HTTPException(422, "Unknown job category")
    cash = P._cash_account(body.payment_method)
    exp_id = new_id()
    edate = body.expense_date or today_str()
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": edate, "source": "expense", "source_ref": exp_id,
        "memo": f"Expense — {body.vendor}: {body.description}".strip() or "Receipt expense",
        "lines": [P.dr(body.category_code, amount, body.description or body.vendor),
                  P.cr(cash, amount, "Paid")],
    }, status="posted")
    exp_doc = {"id": exp_id, "vendor": body.vendor, "category_code": body.category_code,
               "category_name": NAME_OF.get(body.category_code), "amount_cents": amount,
               "payment_method": body.payment_method, "expense_date": edate,
               "description": body.description, "entry_number": entry["number"],
               "receipt_id": rid, "bank_transaction_id": txn_id,
               "job_id": job["id"] if job else None,
               "created_at": now_iso()}
    await Scoped(db.expenses, user["tenant_id"]).insert_one(exp_doc)
    if job:
        extracted = doc.get("extracted") or {}
        if "job_category" in body.model_fields_set:
            category = body.job_category
        else:
            category = job_category_for_receipt(
                body.category_code,
                NAME_OF.get(body.category_code, "") or extracted.get("category_name", ""))
        cost_id = new_id()
        await Scoped(db.job_costs, user["tenant_id"]).insert_one({
            "id": cost_id, "job_id": job["id"], "job_number": job["job_number"],
            "category": category,
            "description": (body.description or body.vendor or "Receipt").strip(),
            "amount_cents": amount, "hours": 0, "rate": 0,
            "product_id": None, "qty": 1, "technician_id": None,
            "entry_number": entry["number"], "journal_entry_id": entry.get("id"),
            "source": "receipt", "source_ref": rid,
            "receipt_id": rid, "expense_id": exp_id,
            "burden_percent": None, "burdened_amount_cents": None,
            "phase_id": phase_id,
            "created_at": now_iso()})
        exp_doc["job_cost_id"] = cost_id
    await receipts(user).update_one(
        {"id": rid},
        {"$set": {"status": "posted", "expense_id": exp_id,
                  "job_id": job["id"] if job else doc.get("job_id"),
                  "phase_id": phase_id,
                  "matched_bank_txn": txn_id}})
    await audit(user, "receipt.post", "receipt", rid,
                {"expense_id": exp_id, "amount": amount, "vendor": body.vendor,
                 "job_id": job["id"] if job else None})
    exp_doc.pop("_id", None)
    return exp_doc
