"""Receipt scan via the Empire Intake Engine (books_adapter/ADAPTER.md).

The wiring under test (routers/receipts.py): with INTAKE_ENGINE_URL
set, POST /receipts/{id}/scan reads the file through the existing
receipt_storage layer and calls the engine — every accepted upload
type, no API key — and the extracted draft gains line_items[] plus
the reconciliation/scan_flag block. The gpt-4o-mini prompt is demoted
to optional enrichment (category/description only). Engine failure
marks the scan failed; never a silent empty extraction.

The engine HTTP call is faked at requests.post — no network, no real
engine. The unconfigured legacy paths are covered unchanged by
tests/test_receipt_storage.py (vision image scan; invoice2data 422).

Run from the backend directory with:
    python3 -m pytest tests/test_receipt_scan_engine.py -n 0 -q
"""
import asyncio
import json
import json as json_module  # alias: fakes below shadow `json` as a kwarg
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_receipt_scan_engine")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException

import lib.audit as audit_mod
from lib import receipt_storage
from lib.receipt_storage import LocalStorage
from routers import receipts as RCPT
from test_receipt_storage import FakeDB, USER, JPEG, PDF

ENGINE_URL = "http://intake.test:8100"

# The engine's real F1 output shape (books_adapter/example_response.json).
ENGINE_RECORD = {
    "document_type": "invoice",
    "vendor": "Lone Star Plumbing Supply Co.",
    "invoice_no": "LPS-2026-1042",
    "date": "2026-10-28",
    "due_date": "2026-11-27",
    "currency": "USD",
    "language": "en",
    "line_items": [
        {"description": "Copper Pipe 3/4in 10ft", "quantity": 12.0,
         "unit_price_cents": 850, "amount_cents": 10200},
        {"description": "PVC Elbow 90deg 1in", "quantity": 40.0,
         "unit_price_cents": 125, "amount_cents": 5000},
        {"description": "Ball Valve 3/4in Brass", "quantity": 6.0,
         "unit_price_cents": 1475, "amount_cents": 8850},
        {"description": "Teflon Tape Roll", "quantity": 24.0,
         "unit_price_cents": 85, "amount_cents": 2040},
    ],
    "subtotal_cents": 26090,
    "discount_cents": None,
    "tax_cents": 2152,
    "tax_label": "Sales Tax",
    "total_cents": 28242,
    "payment_terms": "Net 30",
    "tax_id": None,
    "reconciliation": {"status": "clean", "needs_review": False,
                       "lines_sum_cents": 26090,
                       "computed_total_cents": 28242,
                       "stated_total_cents": 28242, "delta_cents": 0,
                       "detail": "within tolerance"},
    "source_kind": "borndigital_pdf",
    "pages": 1,
}


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _fake_post(seen, engine_record=ENGINE_RECORD, engine_status=200,
               vision=None):
    def fake_post(url, headers=None, json=None, files=None, data=None,
                  timeout=None):
        if url.endswith("/intake"):
            seen["engine"] = {"url": url, "headers": headers,
                              "files": files, "data": data,
                              "timeout": timeout}
            return _Resp(engine_status, engine_record,
                         text="engine boom" if engine_status != 200 else "")
        seen["vision"] = {"url": url, "json": json}
        content = json_module.dumps(vision or {})
        return _Resp(200, {"choices": [{"message": {"content": content}}]})
    return fake_post


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    backend = LocalStorage(tmp_path / "receipts")
    db = FakeDB()
    db.tenants.docs.append({"id": "tenant-1", "tenant_id": "tenant-1",
                            "niche_id": None})
    monkeypatch.setattr(RCPT, "db", db)
    monkeypatch.setattr(audit_mod, "db", db)
    monkeypatch.setenv("INTAKE_ENGINE_URL", ENGINE_URL)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    receipt_storage.set_receipt_storage(backend)
    yield backend, db
    receipt_storage.set_receipt_storage(None)


def test_scan_pdf_via_engine_accepted(wired, monkeypatch):
    """The Books-side equivalent of the engine's F2 regression: a PDF
    scan is ACCEPTED through the engine — the old 422 is gone from
    this path — and the draft gains line items + reconciliation."""
    backend, db = wired
    seen = {}
    monkeypatch.setattr(RCPT.requests, "post", _fake_post(seen))
    doc = asyncio.run(RCPT.ingest_receipt(USER, "invoice.pdf",
                                          "application/pdf", PDF))
    out = asyncio.run(RCPT.scan_receipt(doc["id"], dict(USER)))
    assert out["extraction_source"] == "intake_engine"
    assert out["vendor"] == "Lone Star Plumbing Supply Co."
    assert out["invoice_no"] == "LPS-2026-1042"
    assert len(out["line_items"]) == 4
    assert [li["amount_cents"] for li in out["line_items"]] == \
        [10200, 5000, 8850, 2040]
    assert out["subtotal_cents"] == 26090
    assert out["total_cents"] == 28242
    assert out["amount"] == 282.42  # legacy float, derived from cents
    assert out["scan_flag"] is None
    assert out["reconciliation"]["status"] == "clean"
    # the engine call carried THIS receipt's bytes (read through the
    # storage layer), the Books tenant header, and doc_class=receipt
    call = seen["engine"]
    assert call["headers"]["X-Tenant-Id"] == "tenant-1"
    assert call["data"] == {"doc_class": "receipt"}
    fname, fbytes, ftype = call["files"]["file"]
    assert fbytes == PDF and ftype == "application/pdf"
    assert call["timeout"] >= 120
    saved = asyncio.run(RCPT.get_receipt(doc["id"], dict(USER)))
    assert saved["status"] == "scanned"
    assert saved["scan_status"] == "ok"
    assert saved["extracted"]["line_items"] == out["line_items"]


def test_scan_image_via_engine_needs_no_openai_key(wired, monkeypatch):
    """With no OPENAI_API_KEY anywhere, an image scan still works —
    the engine path is not gated on the vision key."""
    backend, db = wired
    assert not os.environ.get("OPENAI_API_KEY")
    seen = {}
    monkeypatch.setattr(RCPT.requests, "post", _fake_post(seen))
    doc = asyncio.run(RCPT.ingest_receipt(USER, "receipt.jpg",
                                          "image/jpeg", JPEG))
    out = asyncio.run(RCPT.scan_receipt(doc["id"], dict(USER)))
    assert out["total_cents"] == 28242
    assert "vision" not in seen  # no key -> no enrichment attempt


def test_scan_engine_mismatch_flags_needs_review(wired, monkeypatch):
    backend, db = wired
    record = dict(ENGINE_RECORD)
    record["reconciliation"] = {"status": "mismatch", "needs_review": True,
                                "lines_sum_cents": 24590,
                                "computed_total_cents": 26742,
                                "stated_total_cents": 28242,
                                "delta_cents": 1500,
                                "detail": "lines + tax != stated total"}
    seen = {}
    monkeypatch.setattr(RCPT.requests, "post",
                        _fake_post(seen, engine_record=record))
    doc = asyncio.run(RCPT.ingest_receipt(USER, "invoice.pdf",
                                          "application/pdf", PDF))
    out = asyncio.run(RCPT.scan_receipt(doc["id"], dict(USER)))
    assert out["scan_flag"] == "needs_review"
    assert out["needs_review"] is True
    assert out["reconciliation"]["delta_cents"] == 1500
    saved = asyncio.run(RCPT.get_receipt(doc["id"], dict(USER)))
    assert saved["extracted"]["scan_flag"] == "needs_review"


def test_scan_engine_failure_marks_failed_never_empty(wired, monkeypatch):
    backend, db = wired
    seen = {}
    monkeypatch.setattr(RCPT.requests, "post",
                        _fake_post(seen, engine_status=500))
    doc = asyncio.run(RCPT.ingest_receipt(USER, "invoice.pdf",
                                          "application/pdf", PDF))
    with pytest.raises(HTTPException) as e:
        asyncio.run(RCPT.scan_receipt(doc["id"], dict(USER)))
    assert e.value.status_code == 502
    saved = asyncio.run(RCPT.get_receipt(doc["id"], dict(USER)))
    assert saved["scan_status"] == "failed"
    assert saved["scan_error"]
    assert saved["extracted"] is None  # no silent empty extraction
    assert saved["status"] == "uploaded"


def test_scan_enrichment_adds_category_never_overwrites(wired, monkeypatch):
    """OPENAI_API_KEY present: vision enriches ONLY category +
    description over the engine output. Engine money/vendor win."""
    backend, db = wired
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    vision = {"vendor": "WRONG VENDOR", "date": "1999-01-01",
              "amount": 999.99, "tax": 0,
              "category": "5700 Materials", "description": "pipe and fittings"}
    seen = {}
    monkeypatch.setattr(RCPT.requests, "post",
                        _fake_post(seen, vision=vision))
    doc = asyncio.run(RCPT.ingest_receipt(USER, "receipt.jpg",
                                          "image/jpeg", JPEG))
    out = asyncio.run(RCPT.scan_receipt(doc["id"], dict(USER)))
    assert "vision" in seen  # enrichment ran
    assert out["vendor"] == "Lone Star Plumbing Supply Co."
    assert out["amount"] == 282.42
    assert out["total_cents"] == 28242
    assert out["category_code"] == "5700"
    assert out["category_name"]
    assert out["description"] == "pipe and fittings"
