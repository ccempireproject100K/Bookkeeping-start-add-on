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
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require, get_current_user
from lib.base import Scoped, new_id, now_iso, today_str, to_cents
from lib import ledger as L
from lib import posting as P
from lib.coa import ACCOUNT_TYPE_OF, NAME_OF
from lib.niches import get_pack

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/receipts", tags=["receipts"])

UPLOAD_ROOT = Path(__file__).parent / "uploads" / "receipts"
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_BYTES = 10 * 1024 * 1024


def receipts(user):
    return Scoped(db.receipts, user["tenant_id"])


def _upload_dir(user) -> Path:
    d = UPLOAD_ROOT / user["tenant_id"]
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    (_upload_dir(user) / stored).write_bytes(data)
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
    path = _upload_dir(user) / doc["stored"]
    if not path.exists():
        raise HTTPException(404, "Image file missing")
    return FileResponse(path, media_type=doc["content_type"], filename=doc["filename"])


@router.delete("/{rid}")
async def delete_receipt(rid: str, user: dict = Depends(require("ops:manage"))):
    doc = await receipts(user).find_one({"id": rid})
    if not doc:
        raise HTTPException(404, "Receipt not found")
    if doc.get("status") == "posted":
        raise HTTPException(409, "Already posted as an expense — reverse the expense instead.")
    path = _upload_dir(user) / doc["stored"]
    if path.exists():
        path.unlink()
    await receipts(user).delete({"id": rid})
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
        raise HTTPException(422, "AI scan needs an image (JPG/PNG/WEBP) — PDFs can be entered manually.")
    key = _vision_key()
    pack = await _pack_for_user(user)
    cats = _trade_categories(pack)
    trade = pack.get("name", "your trade")
    path = _upload_dir(user) / doc["stored"]
    b64 = base64.b64encode(path.read_bytes()).decode()
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
        logger.error("receipt vision error %s: %s", r.status_code, r.text[:300])
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
