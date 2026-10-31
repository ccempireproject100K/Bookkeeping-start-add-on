"""PDF utilities API (bake-off #14): the twelve operations from the
in-house set in lib/pdfutil.py, exposed over JSON with base64 payloads
(this API is JSON-first; receipts keep their own multipart upload path).

Every response that produces a document returns {"file_b64": ...}.
All processing is in-process on permissive libraries — see lib/pdfutil.py
for the license record. Implemented independently in our stack.
"""
import base64

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from lib.auth import require
from lib import pdfutil as PU

router = APIRouter(prefix="/pdf", tags=["pdf"])


def _dec(b64: str) -> bytes:
    try:
        return base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid base64 payload")


def _enc(data: bytes) -> dict:
    return {"file_b64": base64.b64encode(data).decode(), "bytes": len(data),
            "pages": PU.page_count(data)}


def _guard(fn):
    try:
        return fn()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"PDF processing failed: {str(e)[:200]}")


class FilesIn(BaseModel):
    files: List[str]           # base64 PDFs


class FileIn(BaseModel):
    file: str                  # base64 PDF


class SplitIn(FileIn):
    pages: List[int]           # 1-based


class StampIn(FileIn):
    text: str
    footer: bool = False


class EncryptIn(FileIn):
    password: str


class SignIn(FileIn):
    signer: str
    signed_on: str             # YYYY-MM-DD


class FillIn(FileIn):
    values: dict


class ImagesIn(BaseModel):
    images: List[str]          # base64 PNG/JPEG


class CompareIn(BaseModel):
    a: str
    b: str


class RedactIn(FileIn):
    terms: List[str]


@router.post("/merge")
async def merge(body: FilesIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.merge_pdfs([_dec(f) for f in body.files])))


@router.post("/split")
async def split(body: SplitIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.extract_pages(_dec(body.file), body.pages)))


@router.post("/compress")
async def compress(body: FileIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.compress_pdf(_dec(body.file))))


@router.post("/watermark")
async def watermark(body: StampIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.stamp_pdf(_dec(body.file), body.text, footer=body.footer)))


@router.post("/flatten")
async def flatten(body: FileIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.flatten_pdf(_dec(body.file))))


@router.post("/encrypt")
async def encrypt(body: EncryptIn, user: dict = Depends(require("ops:manage"))):
    if len(body.password) < 4:
        raise HTTPException(status_code=422, detail="Password too short")
    return _guard(lambda: _enc(PU.encrypt_pdf(_dec(body.file), body.password)))


@router.post("/sign")
async def sign(body: SignIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.sign_pdf(_dec(body.file), body.signer, body.signed_on)))


@router.post("/form-fields")
async def form_fields(body: FileIn, user: dict = Depends(require("ledger:read"))):
    return _guard(lambda: {"fields": PU.form_fields(_dec(body.file))})


@router.post("/fill-form")
async def fill_form(body: FillIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.fill_form_pdf(_dec(body.file), body.values)))


@router.post("/to-text")
async def to_text(body: FileIn, user: dict = Depends(require("ledger:read"))):
    return _guard(lambda: {"text": PU.extract_text(_dec(body.file))})


@router.post("/to-csv")
async def to_csv(body: FileIn, user: dict = Depends(require("ledger:read"))):
    return _guard(lambda: {"csv": PU.pdf_to_csv(_dec(body.file))})


@router.post("/from-images")
async def from_images(body: ImagesIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.images_to_pdf([_dec(i) for i in body.images])))


@router.post("/redact")
async def redact(body: RedactIn, user: dict = Depends(require("ops:manage"))):
    return _guard(lambda: _enc(PU.redact_pdf(_dec(body.file), body.terms)))


@router.post("/compare")
async def compare(body: CompareIn, user: dict = Depends(require("ledger:read"))):
    return _guard(lambda: PU.compare_pdfs(_dec(body.a), _dec(body.b)))


@router.post("/ocr")
async def ocr(body: FileIn, user: dict = Depends(require("ledger:read"))):
    return _guard(lambda: PU.ocr_pdf(_dec(body.file)))
