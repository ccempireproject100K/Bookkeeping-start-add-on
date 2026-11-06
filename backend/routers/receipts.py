"""Receipt inbox: snap or upload receipt photos, AI-extract the details in your
trade's language, match to the bank feed, and post as expenses.

Flow: POST /receipts/upload -> POST /receipts/{id}/scan -> review ->
POST /receipts/{id}/post.

Scan routing (intake-engine wiring, books_adapter/ADAPTER.md): when
INTAKE_ENGINE_URL is set, scan sends the stored file to the Empire
Intake Engine (local parse + rules mapper — works on PDFs and images,
no API key, returns line_items[] + a reconciliation block). The
gpt-4o-mini vision prompt survives only as optional enrichment over
the engine's output when OPENAI_API_KEY exists. When INTAKE_ENGINE_URL
is not set, the legacy paths below run unchanged (invoice2data
templates for PDFs, vision for images) so an unconfigured deployment
behaves exactly as before. Nothing ever auto-posts: scan persists a
draft extraction for human review.
"""
import asyncio
import base64
import logging
import os
import uuid
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
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
from lib.coa import ACCOUNT_TYPE_OF, NAME_OF
from lib.niches import get_pack

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/receipts", tags=["receipts"])

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
                         data: bytes, source: str = "upload") -> dict:
    """Shared ingestion path: validate, store the file, create the inbox doc.

    Used by the manual upload endpoint and by the inbound-email webhook, so
    both flows file receipts identically. Raises HTTPException(422) on bad
    input.
    """
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(422, f"Unsupported file type: {content_type}. Use JPG, PNG, WEBP or PDF.")
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
           "source": source, "created_at": now_iso()}
    await receipts(user).insert_one(doc)
    await audit(user, f"receipt.{source}", "receipt", rid, {"filename": doc["filename"]})
    doc.pop("_id", None)
    return doc


@router.post("/upload")
async def upload_receipt(file: UploadFile = File(...),
                         user: dict = Depends(require("ops:manage"))):
    data = await file.read()
    return await ingest_receipt(user, file.filename, file.content_type, data,
                                source="upload")


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


async def _vision_extract(user: dict, doc: dict, pack: dict) -> dict:
    """The gpt-4o-mini vision prompt, unchanged from its days as the only
    scan path. Raises 503 without OPENAI_API_KEY, 502 on any failure.
    Callers: the legacy image path (where it is still the scan) and the
    engine path (where it is optional enrichment — see _scan_via_engine).
    """
    import json
    key = _vision_key()
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
    return {"vendor": (data.get("vendor") or "").strip(),
            "date": (data.get("date") or "").strip(),
            "amount": float(data.get("amount") or 0),
            "tax": float(data.get("tax") or 0),
            "category_code": code if code in ACCOUNT_TYPE_OF else "",
            "category_name": NAME_OF.get(code, ""),
            "description": (data.get("description") or "").strip(),
            "trade": trade}


# ---------------- Intake engine scan (ADAPTER.md) ----------------
def _engine_url() -> str:
    return os.environ.get("INTAKE_ENGINE_URL", "").strip().rstrip("/")


async def _engine_extract(user: dict, doc: dict, data: bytes) -> dict:
    """POST the stored file to the Empire Intake Engine and return its
    record JSON. Any failure raises 502 — the caller marks the scan
    failed; there is never a silent empty extraction (ADAPTER.md,
    failure honesty). Scanned/photo pages are CPU-heavy: 150s budget.
    """
    url = f"{_engine_url()}/intake"

    def _do():
        return requests.post(
            url,
            headers={"X-Tenant-Id": user["tenant_id"]},
            files={"file": (doc["filename"], data, doc["content_type"])},
            data={"doc_class": "receipt"},
            timeout=150,
        )

    try:
        r = await asyncio.to_thread(_do)
    except Exception as exc:
        logger.error("intake engine request failed: %s", exc)
        raise HTTPException(502, "The intake engine could not be reached — enter it manually.")
    if r.status_code != 200:
        logger.error("intake engine error %s: %s", r.status_code,
                     scrub_secrets(r.text[:300]))
        raise HTTPException(502, f"Intake engine error ({r.status_code}) — enter it manually.")
    try:
        record = r.json()
    except Exception:
        raise HTTPException(502, "Intake engine replied unreadably — enter it manually.")
    if not isinstance(record, dict) or "line_items" not in record:
        raise HTTPException(502, "Intake engine replied unreadably — enter it manually.")
    return record


def _map_engine_record(record: dict, trade: str) -> dict:
    """Engine record -> Books `extracted` (ADAPTER.md field map).

    The map's NEW fields (line_items[], *_cents, scan_flag, the full
    reconciliation block) land alongside the legacy amount/tax floats
    the review UI and the bank-feed matcher already read — those are
    derived from the engine's cents, never a second source of truth.
    """
    recon = record.get("reconciliation") or {}
    needs_review = bool(recon.get("needs_review"))
    total_c = record.get("total_cents") or 0
    tax_c = record.get("tax_cents") or 0
    return {
        "vendor": (record.get("vendor") or "").strip(),
        "invoice_no": record.get("invoice_no"),
        "date": (record.get("date") or "").strip(),
        "due_date": record.get("due_date"),
        "currency": record.get("currency"),
        "line_items": record.get("line_items") or [],
        "subtotal_cents": record.get("subtotal_cents"),
        "discount_cents": record.get("discount_cents"),
        "tax_cents": tax_c,
        "total_cents": total_c,
        "tax_label": record.get("tax_label"),
        "reconciliation": recon,
        "scan_flag": "needs_review" if needs_review else None,
        "payment_terms": record.get("payment_terms"),
        "vendor_tax_id": record.get("tax_id"),
        "amount": total_c / 100.0,
        "tax": tax_c / 100.0,
        "category_code": "",
        "category_name": "",
        "description": "",
        "trade": trade,
        "extraction_source": "intake_engine",
        "needs_review": needs_review,
    }


async def _scan_via_engine(user: dict, doc: dict) -> dict:
    """The engine scan: every accepted upload type, no API key needed.
    Bytes come through the existing receipt_storage layer — no second
    file path. The extraction persists as a draft; nothing auto-posts.
    """
    rid = doc["id"]
    data = _store().read(_key(user, doc["stored"]))
    try:
        record = await _engine_extract(user, doc, data)
    except HTTPException as exc:
        # Failure honesty: mark the scan failed on the receipt, keep
        # the manual-entry path, and surface the error — never persist
        # a silent empty extraction.
        await receipts(user).update_one(
            {"id": rid},
            {"$set": {"scan_status": "failed", "scan_error": str(exc.detail)}})
        await audit(user, "receipt.scan_failed", "receipt", rid,
                    {"source": "intake_engine", "error": str(exc.detail)})
        raise
    pack = await _pack_for_user(user)
    trade = pack.get("name", "your trade")
    extracted = _map_engine_record(record, trade)
    # Optional enrichment, demoted per ADAPTER.md: with OPENAI_API_KEY
    # present, the vision prompt may add ONLY the fields the engine
    # does not produce (trade category, description) over the engine's
    # structured output, on image inputs. Engine money/vendor/date
    # fields are authoritative; enrichment failure never fails a scan.
    enriched = False
    if doc["content_type"] != "application/pdf" and \
            os.environ.get("OPENAI_API_KEY", "").strip():
        try:
            vision = await _vision_extract(user, doc, pack)
        except HTTPException:
            vision = None
        if vision:
            if vision.get("category_code"):
                extracted["category_code"] = vision["category_code"]
                extracted["category_name"] = vision.get("category_name", "")
            if vision.get("description"):
                extracted["description"] = vision["description"]
            enriched = bool(extracted["category_code"] or extracted["description"])
    await receipts(user).update_one(
        {"id": rid},
        {"$set": {"extracted": extracted, "status": "scanned",
                  "scan_status": "ok", "scan_error": None}})
    await audit(user, "receipt.scan", "receipt", rid,
                {"vendor": extracted["vendor"], "amount": extracted["amount"],
                 "source": "intake_engine", "enriched": enriched})
    return extracted


async def _scan_legacy(user: dict, doc: dict) -> dict:
    """The pre-engine scan paths, byte-for-byte in behavior. They run
    only when INTAKE_ENGINE_URL is not configured."""
    rid = doc["id"]
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
    pack = await _pack_for_user(user)
    extracted = await _vision_extract(user, doc, pack)
    await receipts(user).update_one({"id": rid},
                                    {"$set": {"extracted": extracted, "status": "scanned"}})
    await audit(user, "receipt.scan", "receipt", rid, {"vendor": extracted["vendor"], "amount": extracted["amount"]})
    return extracted


@router.post("/{rid}/scan")
async def scan_receipt(rid: str, user: dict = Depends(require("ops:manage"))):
    """Extract the receipt's details into a draft for review.

    With INTAKE_ENGINE_URL set: the intake engine (PDFs and images,
    line items + reconciliation). Without it: the legacy paths. Either
    way the result is a draft — posting stays a separate human action,
    and Pip's tray Scan action calls this same endpoint unchanged.
    """
    doc = await receipts(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(404, "Receipt not found")
    if _engine_url():
        return await _scan_via_engine(user, doc)
    return await _scan_legacy(user, doc)


# ---------------- Bank-feed matching ----------------
@router.get("/{rid}/matches")
async def receipt_matches(rid: str, user: dict = Depends(require("ledger:read"))):
    """Suggest bank transactions that look like this receipt (amount + date proximity)."""
    doc = await receipts(user).find_one({"id": rid})
    if not doc or not doc.get("extracted"):
        raise HTTPException(404, "Scan the receipt first")
    amt = to_cents(doc["extracted"].get("amount") or 0)
    rdate = (doc["extracted"].get("date") or "")[:10]
    if not amt or not rdate:
        return []
    # bank outflows are stored negative; receipt amounts are positive
    cands = await Scoped(db.bank_transactions, user["tenant_id"]).find_list(
        {"amount_cents": {"$in": [amt, -amt]},
         "txn_date": {"$gte": _shift(rdate, -4), "$lte": _shift(rdate, 4)}},
        sort=[("txn_date", -1)], limit=20)
    return [{"id": c["id"], "txn_date": c["txn_date"], "description": c.get("description", ""),
             "amount_cents": c["amount_cents"], "account": c.get("bank_account_id", "")}
            for c in cands]


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


@router.post("/{rid}/post")
async def post_receipt(rid: str, body: PostBody, user: dict = Depends(require("ops:manage"))):
    """Turn a scanned receipt into a posted expense (one balanced journal entry)."""
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
               "receipt_id": rid, "bank_transaction_id": body.bank_transaction_id,
               "created_at": now_iso()}
    await Scoped(db.expenses, user["tenant_id"]).insert_one(exp_doc)
    await receipts(user).update_one(
        {"id": rid},
        {"$set": {"status": "posted", "expense_id": exp_id,
                  "matched_bank_txn": body.bank_transaction_id}})
    await audit(user, "receipt.post", "receipt", rid,
                {"expense_id": exp_id, "amount": amount, "vendor": body.vendor})
    exp_doc.pop("_id", None)
    return exp_doc
