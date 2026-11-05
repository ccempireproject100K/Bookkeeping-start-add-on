"""Receipt file storage: local disk today, Cloudflare R2 when configured.

Covers the storage layer (lib/receipt_storage.py) and proves the
receipt flow — upload (the shared ingest path the inbound-email
webhook also uses) -> serve -> scan -> delete — behaves identically on
both backends. The R2 backend is exercised against an in-memory fake
S3 client: no test ever touches a real bucket or the network.

Run from the backend directory with:
    python3 -m pytest tests/test_receipt_storage.py -n 0 -q
"""
import asyncio
import io
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_receipt_storage")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

import lib.audit as audit_mod
from lib import receipt_storage
from lib.receipt_storage import (LocalStorage, R2Storage,
                                 build_receipt_storage, migrate_receipts)
from routers import receipts as RCPT


# ------------------------------------------------------------------ fake S3

class _Body:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self):
        return self._buf.read()

    def iter_chunks(self, chunk_size=1024):
        while True:
            chunk = self._buf.read(chunk_size)
            if not chunk:
                return
            yield chunk


class FakeS3:
    """The slice of the boto3 S3 client the storage layer uses."""

    def __init__(self):
        self.objects = {}  # (bucket, key) -> (bytes, content_type)

    @staticmethod
    def _err(code, op):
        return ClientError({"Error": {"Code": code, "Message": code}}, op)

    def put_object(self, Bucket, Key, Body, ContentType=None, **kw):
        self.objects[(Bucket, Key)] = (bytes(Body), ContentType)
        return {}

    def get_object(self, Bucket, Key, **kw):
        if (Bucket, Key) not in self.objects:
            raise self._err("NoSuchKey", "GetObject")
        data, _ = self.objects[(Bucket, Key)]
        return {"Body": _Body(data), "ContentLength": len(data)}

    def head_object(self, Bucket, Key, **kw):
        if (Bucket, Key) not in self.objects:
            raise self._err("404", "HeadObject")
        data, _ = self.objects[(Bucket, Key)]
        return {"ContentLength": len(data)}

    def delete_object(self, Bucket, Key, **kw):
        self.objects.pop((Bucket, Key), None)
        return {}


FULL_R2_ENV = {"BOOKS_STORAGE_BACKEND": "r2",
               "BOOKS_R2_BUCKET": "books-receipts",
               "BOOKS_R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
               "BOOKS_R2_ACCESS_KEY_ID": "key-id",
               "BOOKS_R2_SECRET_ACCESS_KEY": "secret"}


# ------------------------------------------------------------------ fake db
# Minimal in-memory doubles in the style of test_tenant_isolation_probes.py —
# just the surface routers/receipts.py + lib.base.Scoped + lib.audit use.

def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict):
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
            if "$exists" in v and ((doc.get(k) is not None) != bool(v["$exists"])):
                return False
        elif doc.get(k) != v:
            return False
    return True


def _apply(doc, update):
    for op, changes in (update or {}).items():
        if op == "$set":
            doc.update(changes)
        elif op == "$inc":
            for k, v in changes.items():
                doc[k] = doc.get(k, 0) + v
        elif op == "$unset":
            for k in changes:
                doc.pop(k, None)


class _Result:
    def __init__(self, n):
        self.matched_count = n
        self.modified_count = n
        self.deleted_count = n


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, spec, direction=-1):
        if isinstance(spec, list):
            for key, dirn in reversed(spec):
                self._docs.sort(key=lambda d: d.get(key) or "",
                                reverse=(dirn < 0))
        else:
            self._docs.sort(key=lambda d: d.get(spec) or "",
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return self._docs


class FakeColl:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                out = dict(d)
                if projection and projection.get("_id") == 0:
                    out.pop("_id", None)
                return out
        return None

    def find(self, q=None, projection=None):
        return _Cursor([dict(d) for d in self.docs if _matches(d, q)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if _matches(d, q):
                _apply(d, update)
                return _Result(1)
        return _Result(0)

    async def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if _matches(d, q):
                del self.docs[i]
                return _Result(1)
        return _Result(0)

    async def find_one_and_update(self, q, update, **kw):
        for d in self.docs:
            if _matches(d, q):
                before = dict(d)
                _apply(d, update)
                return before
        return None

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q))


class FakeDB:
    def __getattr__(self, name):
        coll = FakeColl()
        setattr(self, name, coll)
        return coll

    def __getitem__(self, name):
        return getattr(self, name)


USER = {"id": "u-1", "tenant_id": "tenant-1", "email": "owner@example.com",
        "name": "Owner", "role": "owner"}
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
PDF = b"%PDF-1.4 fake pdf bytes"


# ------------------------------------------------------- storage unit tests

@pytest.fixture(params=["local", "r2"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalStorage(tmp_path / "receipts")
    return R2Storage("books-receipts", FULL_R2_ENV["BOOKS_R2_ENDPOINT"],
                     "key-id", "secret", client=FakeS3())


def test_save_read_exists_size_delete(store):
    key = "tenant-1/abc123.jpg"
    assert not store.exists(key)
    store.save(key, JPEG, "image/jpeg")
    assert store.exists(key)
    assert store.read(key) == JPEG
    assert store.size(key) == len(JPEG)
    store.delete(key)
    assert not store.exists(key)
    with pytest.raises(FileNotFoundError):
        store.read(key)


def test_bad_keys_rejected(store):
    for bad in ("", "/abs.jpg", "../up.jpg", "a/../b.jpg", "a//b.jpg",
                "a\\b.jpg", "./x.jpg"):
        with pytest.raises(ValueError):
            store.save(bad, b"x")
        assert not store.exists(bad)


def test_content_type_for():
    assert receipt_storage.content_type_for("t/1.jpg") == "image/jpeg"
    assert receipt_storage.content_type_for("t/1.pdf") == "application/pdf"
    assert receipt_storage.content_type_for("t/1.webp") == "image/webp"


# ------------------------------------------------------------- config tests

def test_default_backend_is_local(tmp_path):
    s = build_receipt_storage({}, local_root=tmp_path)
    assert isinstance(s, LocalStorage)
    assert s.root == tmp_path


def test_r2_backend_builds_with_full_env():
    fake = FakeS3()
    s = build_receipt_storage(FULL_R2_ENV, client=fake)
    assert isinstance(s, R2Storage)
    assert s.bucket == "books-receipts"
    assert s.client is fake


@pytest.mark.parametrize("missing", list(FULL_R2_ENV)[1:])
def test_r2_missing_value_fails_loudly(missing):
    env = {k: v for k, v in FULL_R2_ENV.items() if k != missing}
    with pytest.raises(RuntimeError) as e:
        build_receipt_storage(env, client=FakeS3())
    assert missing in str(e.value)
    # the refusal must say why: never a silent local fallback
    assert "Refusing to start on local disk" in str(e.value)


def test_unknown_backend_fails_loudly():
    with pytest.raises(RuntimeError):
        build_receipt_storage({"BOOKS_STORAGE_BACKEND": "dropbox"})


# ---------------------------------------------------------- migration tests

def _seed_local(tmp_path):
    local = LocalStorage(tmp_path / "receipts")
    local.save("tenant-1/r1.jpg", JPEG, "image/jpeg")
    local.save("tenant-1/r2.pdf", PDF, "application/pdf")
    local.save("tenant-2/r3.png", b"\x89PNG\r\n\x1a\nfake", "image/png")
    return local


def test_migration_copies_verifies_and_never_deletes(tmp_path):
    local = _seed_local(tmp_path)
    dest = R2Storage("books-receipts", FULL_R2_ENV["BOOKS_R2_ENDPOINT"],
                     "key-id", "secret", client=FakeS3())
    report = migrate_receipts(local, dest)
    assert report == {"total": 3, "copied": 3, "skipped": 0,
                      "failed": 0, "failures": []}
    assert dest.read("tenant-1/r1.jpg") == JPEG
    assert dest.read("tenant-2/r3.png") == b"\x89PNG\r\n\x1a\nfake"
    # local copies untouched
    assert local.list_keys() == ["tenant-1/r1.jpg", "tenant-1/r2.pdf",
                                 "tenant-2/r3.png"]
    assert local.read("tenant-1/r2.pdf") == PDF


def test_migration_is_idempotent_and_repairs_tampering(tmp_path):
    local = _seed_local(tmp_path)
    fake = FakeS3()
    dest = R2Storage("books-receipts", FULL_R2_ENV["BOOKS_R2_ENDPOINT"],
                     "key-id", "secret", client=fake)
    migrate_receipts(local, dest)
    again = migrate_receipts(local, dest)
    assert again["copied"] == 0 and again["skipped"] == 3
    # tamper with one stored object: the next run must re-copy just it
    fake.objects[("books-receipts", "tenant-1/r2.pdf")] = (b"tampered",
                                                           "application/pdf")
    repaired = migrate_receipts(local, dest)
    assert repaired["copied"] == 1 and repaired["skipped"] == 2
    assert dest.read("tenant-1/r2.pdf") == PDF


# ------------------------------------------------- receipt flow, both stores

@pytest.fixture(params=["local", "r2"])
def flow(request, tmp_path, monkeypatch):
    """The receipts router wired to a fake db + one storage backend."""
    if request.param == "local":
        backend = LocalStorage(tmp_path / "receipts")
    else:
        backend = R2Storage("books-receipts", FULL_R2_ENV["BOOKS_R2_ENDPOINT"],
                            "key-id", "secret", client=FakeS3())
    db = FakeDB()
    db.tenants.docs.append({"id": "tenant-1", "tenant_id": "tenant-1",
                            "niche_id": None})
    monkeypatch.setattr(RCPT, "db", db)
    monkeypatch.setattr(audit_mod, "db", db)
    receipt_storage.set_receipt_storage(backend)
    yield backend, db
    receipt_storage.set_receipt_storage(None)


def test_flow_upload_serve_delete(flow):
    backend, db = flow
    doc = asyncio.run(RCPT.ingest_receipt(USER, "receipt.jpg",
                                          "image/jpeg", JPEG))
    assert doc["status"] == "uploaded"
    key = f"tenant-1/{doc['stored']}"
    assert backend.exists(key)
    resp = asyncio.run(RCPT.receipt_image(doc["id"], dict(USER)))
    assert resp.body == JPEG
    assert resp.media_type == "image/jpeg"
    out = asyncio.run(RCPT.delete_receipt(doc["id"], dict(USER)))
    assert out == {"ok": True}
    assert not backend.exists(key)
    with pytest.raises(HTTPException) as e:
        asyncio.run(RCPT.get_receipt(doc["id"], dict(USER)))
    assert e.value.status_code == 404


def test_flow_image_missing_file_is_404(flow):
    backend, db = flow
    doc = asyncio.run(RCPT.ingest_receipt(USER, "receipt.jpg",
                                          "image/jpeg", JPEG))
    backend.delete(f"tenant-1/{doc['stored']}")
    with pytest.raises(HTTPException) as e:
        asyncio.run(RCPT.receipt_image(doc["id"], dict(USER)))
    assert e.value.status_code == 404
    assert "Image file missing" in str(e.value.detail)


def test_flow_scan_image_via_vision(flow, monkeypatch):
    backend, db = flow
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            content = json.dumps({
                "vendor": "Supply House", "date": "2026-11-01",
                "amount": 42.50, "tax": 3.10,
                "category": "5700 Materials", "description": "pipe fittings"})
            return {"choices": [{"message": {"content": content}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["payload"] = json
        return _Resp()

    monkeypatch.setattr(RCPT.requests, "post", fake_post)
    doc = asyncio.run(RCPT.ingest_receipt(USER, "receipt.jpg",
                                          "image/jpeg", JPEG))
    out = asyncio.run(RCPT.scan_receipt(doc["id"], dict(USER)))
    assert out["vendor"] == "Supply House"
    assert out["amount"] == 42.50
    # the vision call carried THIS receipt's bytes (read through storage)
    import base64
    expected = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    parts = seen["payload"]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"] == expected
    saved = asyncio.run(RCPT.get_receipt(doc["id"], dict(USER)))
    assert saved["status"] == "scanned"


def test_flow_scan_pdf_reads_through_storage(flow):
    # a PDF whose text layer matches no invoice template takes the manual
    # path — reaching that 422 proves scan read the bytes from the store
    # (a missing file would raise instead, on either backend).
    backend, db = flow
    doc = asyncio.run(RCPT.ingest_receipt(USER, "invoice.pdf",
                                          "application/pdf", PDF))
    with pytest.raises(HTTPException) as e:
        asyncio.run(RCPT.scan_receipt(doc["id"], dict(USER)))
    assert e.value.status_code == 422
    assert "No invoice template matched" in str(e.value.detail)
