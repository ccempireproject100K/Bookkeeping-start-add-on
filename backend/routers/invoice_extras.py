"""Invoice extras — owner-approved features, 2026-11-14, all on OUR stack
(owner condition: these must cost nothing to run — no paid third-party
services or APIs anywhere in this module):

  AGREEMENT   PUT  /invoices/{id}/agreement — sender-provided agreement
              text on an invoice (rides to the first split part = deposit).
  SIGN        POST /c/{token}/sign/{invoice_id} — SELF-BUILT simple e-sign:
              typed or drawn signature + explicit consent checkbox + an
              audit trail (signer name, timestamp, IP, user-agent) stored
              on the invoice record. Honest label: a simple e-sign with an
              audit trail — NOT a third-party certified e-signature.
              Payment is gated until signed (see invoice_delivery.client_pay).
  PHOTOS      POST /invoices/{id}/photos (upload, receipts.py pattern:
              filesystem storage, tenant-scoped), GET/DELETE, served to the
              client page through the same token gate as everything else.
"""
import re
from pathlib import Path
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from db import db
from lib.audit import audit
from lib.auth import get_current_user, require
from lib.base import Scoped, new_id, now_iso
from lib import invoice_pay as IP

router = APIRouter(tags=["invoice-extras"])

UPLOAD_ROOT = Path(__file__).parent / "uploads" / "invoice_photos"
ALLOWED_PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png",
                       "image/webp": "webp"}
MAX_PHOTO_BYTES = 8 * 1024 * 1024       # per photo
MAX_PHOTOS_PER_INVOICE = 12
PHOTO_KINDS = ("before", "after", "job")


def _inv_coll(tenant_id):
    return Scoped(db.invoices, tenant_id)


async def _get_invoice(tenant_id, invoice_id):
    inv = await _inv_coll(tenant_id).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return inv


def _photo_dir(tenant_id, invoice_id) -> Path:
    d = UPLOAD_ROOT / tenant_id / invoice_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


# ---------------- AGREEMENT ----------------
class AgreementBody(BaseModel):
    agreement_text: str = Field(max_length=20000)


@router.put("/invoices/{invoice_id}/agreement")
async def set_agreement(invoice_id: str, body: AgreementBody,
                        user: dict = Depends(require("ops:manage"))):
    """Set (or clear, with empty text) the agreement the client reviews
    and signs on the client page before paying. If the invoice is later
    split, the text rides to the first part (the deposit)."""
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    if inv.get("signature"):
        raise HTTPException(409, "This agreement is already signed — "
                                 "the signed text cannot be changed.")
    text = body.agreement_text.strip()
    await _inv_coll(user["tenant_id"]).update_one(
        {"id": invoice_id}, {"$set": {"agreement_text": text}})
    await audit(user, "invoice.agreement_set", "invoice", invoice_id,
                {"chars": len(text)})
    return {"ok": True, "agreement_text": text,
            "signature_required": bool(text)}


# ---------------- SIGN (public, token-gated) ----------------
class SignBody(BaseModel):
    signer_name: str = Field(min_length=1, max_length=120)
    signature_type: str = Field(pattern=r"^(typed|drawn)$")
    # typed: the name as typed; drawn: a PNG data URL from the canvas
    signature_data: str = Field(min_length=1, max_length=700_000)
    consent: bool = False


@router.post("/c/{token}/sign/{invoice_id}")
async def client_sign(token: str, invoice_id: str, body: SignBody,
                      request: Request):
    """Record a simple e-signature with its audit trail on the invoice.

    The deposit/payment step cannot complete before this succeeds (the
    gate is enforced in invoice_delivery.client_pay).
    """
    link = await IP.resolve_client_token(token)
    if not link:
        raise HTTPException(404, "Link not found")
    inv = await _inv_coll(link["tenant_id"]).find_one({"id": invoice_id})
    if not inv or (link.get("customer_id")
                   and inv.get("customer_id") != link["customer_id"]):
        raise HTTPException(404, "Invoice not found")
    if not (inv.get("agreement_text") or "").strip():
        raise HTTPException(409, "This invoice has no agreement to sign.")
    if inv.get("signature"):
        raise HTTPException(409, "This agreement is already signed.")
    if not body.consent:
        raise HTTPException(
            422, "Please tick the consent box to sign electronically.")
    if body.signature_type == "drawn" and \
            not body.signature_data.startswith("data:image/"):
        raise HTTPException(422, "Drawn signature must be an image.")
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = (forwarded.split(",")[0].strip()
          or (request.client.host if request.client else ""))
    sig = {
        "signer_name": body.signer_name.strip(),
        "signature_type": body.signature_type,
        "signature_data": body.signature_data,
        "consent": True,
        "signed_at": now_iso(),
        "ip": ip,
        "user_agent": request.headers.get("user-agent", ""),
        "method_note": ("Simple electronic signature with audit trail "
                        "(Bookkeeping InvoiceEase) — not a third-party "
                        "certified e-signature service."),
    }
    await _inv_coll(link["tenant_id"]).update_one(
        {"id": invoice_id}, {"$set": {"signature": sig}})
    await IP.add_timeline(link["tenant_id"], invoice_id, "signed",
                          {"signer_name": sig["signer_name"],
                           "signature_type": sig["signature_type"]})
    return {"ok": True, "signed_at": sig["signed_at"],
            "signer_name": sig["signer_name"]}


# ---------------- JOB PHOTOS ----------------
@router.post("/invoices/{invoice_id}/photos")
async def upload_photo(invoice_id: str,
                       file: UploadFile = File(...),
                       caption: str = Form(""),
                       kind: str = Form("job"),
                       user: dict = Depends(require("ops:manage"))):
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    if file.content_type not in ALLOWED_PHOTO_TYPES:
        raise HTTPException(
            422, f"Unsupported file type: {file.content_type}. "
                 "Use JPG, PNG or WEBP.")
    if kind not in PHOTO_KINDS:
        raise HTTPException(422, "kind must be before, after or job")
    if len(inv.get("photos", [])) >= MAX_PHOTOS_PER_INVOICE:
        raise HTTPException(
            422, f"An invoice can carry at most {MAX_PHOTOS_PER_INVOICE} photos.")
    data = await file.read()
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(422, "Photo is larger than 8 MB.")
    if not data:
        raise HTTPException(422, "Empty file.")
    pid = new_id()
    stored = f"{pid}.{ALLOWED_PHOTO_TYPES[file.content_type]}"
    (_photo_dir(user["tenant_id"], invoice_id) / stored).write_bytes(data)
    photo = {"id": pid, "filename": file.filename or stored,
             "stored": stored, "content_type": file.content_type,
             "size_bytes": len(data), "caption": caption.strip()[:200],
             "kind": kind, "created_at": now_iso()}
    await _inv_coll(user["tenant_id"]).update_one(
        {"id": invoice_id}, {"$push": {"photos": photo}})
    await audit(user, "invoice.photo_add", "invoice", invoice_id,
                {"photo_id": pid, "kind": kind})
    return {k: photo[k] for k in ("id", "filename", "content_type",
                                  "size_bytes", "caption", "kind",
                                  "created_at")}


@router.delete("/invoices/{invoice_id}/photos/{photo_id}")
async def delete_photo(invoice_id: str, photo_id: str,
                       user: dict = Depends(require("ops:manage"))):
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    photo = next((p for p in inv.get("photos", []) if p["id"] == photo_id),
                 None)
    if not photo:
        raise HTTPException(404, "Photo not found")
    path = _photo_dir(user["tenant_id"], invoice_id) / _safe(photo["stored"])
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    await _inv_coll(user["tenant_id"]).update_one(
        {"id": invoice_id}, {"$pull": {"photos": {"id": photo_id}}})
    return {"ok": True}


def _serve_photo(tenant_id, inv, photo_id):
    photo = next((p for p in inv.get("photos", []) if p["id"] == photo_id),
                 None)
    if not photo:
        raise HTTPException(404, "Photo not found")
    path = UPLOAD_ROOT / tenant_id / inv["id"] / _safe(photo["stored"])
    if not path.exists():
        raise HTTPException(404, "Photo file missing")
    return FileResponse(path, media_type=photo["content_type"],
                        filename=photo["filename"])


@router.get("/invoices/{invoice_id}/photos/{photo_id}")
async def get_photo(invoice_id: str, photo_id: str,
                    user: dict = Depends(get_current_user)):
    inv = await _get_invoice(user["tenant_id"], invoice_id)
    return _serve_photo(user["tenant_id"], inv, photo_id)


@router.get("/c/{token}/photos/{invoice_id}/{photo_id}")
async def client_get_photo(token: str, invoice_id: str, photo_id: str):
    """Public serve — behind the same unguessable, revocable client token
    as the rest of the client page."""
    link = await IP.resolve_client_token(token)
    if not link:
        raise HTTPException(404, "Link not found")
    inv = await _inv_coll(link["tenant_id"]).find_one({"id": invoice_id})
    if not inv or (link.get("customer_id")
                   and inv.get("customer_id") != link["customer_id"]):
        raise HTTPException(404, "Invoice not found")
    return _serve_photo(link["tenant_id"], inv, photo_id)
