"""Where receipt files live: local disk today, Cloudflare R2 when configured.

Every receipt byte the app stores, serves, scans or deletes goes through
the store built by ``build_receipt_storage`` (cached by
``get_receipt_storage``). Two backends share one small interface:

    save(key, data, content_type=None) / read(key) / delete(key) /
    exists(key) / size(key)

Keys keep the naming the app has always used — ``<tenant_id>/<receipt
id>.<ext>`` — and the bytes are always served by this app (the
``/receipts/{id}/image`` route): the bucket itself is never public, so
no existing access rule changes.

The local backend is today's behavior, byte for byte: files under
``backend/uploads/receipts/<tenant_id>/``. The R2 backend is env-gated
(``BOOKS_STORAGE_BACKEND=r2`` plus the ``BOOKS_R2_*`` values) and
**fails loudly at startup when any R2 value is missing**. Falling back
to local disk silently would be the worst outcome on a host whose disk
is ephemeral (Render): uploads would appear to work and then vanish on
the next deploy, with nobody told. A loud crash at boot is found in a
minute; silent data loss is found by a customer. See STORAGE.md at the
repo root for the env sheet and the flip procedure.

Moving existing receipts: run
``python scripts/migrate_receipts_to_r2.py`` once after the R2 env
values are set — it copies every local file into the bucket under the
same key, verifies each copy's SHA-256, is safe to rerun, and never
deletes the local files.
"""
import hashlib
import mimetypes
import os
from pathlib import Path

CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp",
                 ".pdf": "application/pdf"}


def content_type_for(key: str) -> str:
    return CONTENT_TYPES.get(Path(key).suffix.lower()) \
        or mimetypes.guess_type(key)[0] or "application/octet-stream"


def _check_key(key: str) -> str:
    """Keys are relative paths inside the receipts root/bucket — nothing else."""
    if not key or key.startswith("/") or "\\" in key:
        raise ValueError("bad receipt key")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("bad receipt key")
    return key


def default_local_root() -> Path:
    """The historical receipt folder: backend/uploads/receipts."""
    return Path(__file__).resolve().parent.parent / "uploads" / "receipts"


class LocalStorage:
    """Local disk — the original behavior, kept exactly."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def save(self, key: str, data: bytes, content_type: str | None = None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except ValueError:
            return False

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def list_keys(self) -> list[str]:
        """Every stored key, sorted — the migration's source listing."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*") if p.is_file())


class R2Storage:
    """Cloudflare R2 via its S3-compatible API (boto3).

    ``client`` is injectable so the tests run against an in-memory fake
    and no test ever touches a real bucket.
    """

    def __init__(self, bucket: str, endpoint: str, access_key_id: str,
                 secret_access_key: str, client=None):
        self.bucket = bucket
        if client is None:
            import boto3

            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="auto",
            )
        self.client = client

    def save(self, key: str, data: bytes, content_type: str | None = None) -> None:
        _check_key(key)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType=content_type or content_type_for(key))

    def _get(self, key: str, **kwargs):
        from botocore.exceptions import ClientError

        _check_key(key)
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key, **kwargs)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("NoSuchKey", "404", "NoSuchBucket"):
                raise FileNotFoundError(key) from exc
            raise OSError(f"R2 read failed for {key}: {code or exc}") from exc

    def read(self, key: str) -> bytes:
        return self._get(key)["Body"].read()

    def delete(self, key: str) -> None:
        _check_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            _check_key(key)
        except ValueError:
            return False
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NoSuchBucket"):
                return False
            raise OSError(f"R2 check failed for {key}: {code or exc}") from exc

    def size(self, key: str) -> int:
        from botocore.exceptions import ClientError

        _check_key(key)
        try:
            return int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey"):
                raise FileNotFoundError(key) from exc
            raise OSError(f"R2 check failed for {key}: {code or exc}") from exc


_R2_ENV = ("BOOKS_R2_BUCKET", "BOOKS_R2_ENDPOINT",
           "BOOKS_R2_ACCESS_KEY_ID", "BOOKS_R2_SECRET_ACCESS_KEY")


def r2_missing(env) -> list[str]:
    """The env names still needed before the R2 backend can start."""
    return [name for name in _R2_ENV if not str(env.get(name) or "").strip()]


def build_receipt_storage(env=None, client=None, local_root=None):
    """The one factory every entry point uses (app, scripts, tests).

    Fail-loud by design: backend=r2 with a missing value raises instead
    of quietly serving from a disk that will be wiped — see the module
    note. ``env`` is any mapping (os.environ by default); ``client``
    injects a prebuilt S3 client (tests); ``local_root`` overrides the
    local receipts folder (tests).
    """
    env = os.environ if env is None else env
    backend = str(env.get("BOOKS_STORAGE_BACKEND") or "local").strip().lower()
    if backend == "local":
        return LocalStorage(local_root or default_local_root())
    if backend == "r2":
        missing = r2_missing(env)
        if missing:
            raise RuntimeError(
                "BOOKS_STORAGE_BACKEND=r2 but these are not set: "
                + ", ".join(missing)
                + ". Refusing to start on local disk instead: on this host the disk is temporary, "
                "so receipt files would silently vanish. Set the missing values, "
                "or set BOOKS_STORAGE_BACKEND=local."
            )
        return R2Storage(str(env["BOOKS_R2_BUCKET"]).strip(),
                         str(env["BOOKS_R2_ENDPOINT"]).strip(),
                         str(env["BOOKS_R2_ACCESS_KEY_ID"]).strip(),
                         str(env["BOOKS_R2_SECRET_ACCESS_KEY"]).strip(),
                         client=client)
    raise RuntimeError(f"Unknown BOOKS_STORAGE_BACKEND {backend!r}: use 'local' or 'r2'.")


_cached = None


def get_receipt_storage():
    """The process-wide store, built once from the environment."""
    global _cached
    if _cached is None:
        _cached = build_receipt_storage()
    return _cached


def set_receipt_storage(store) -> None:
    """Replace the cached store (tests); pass None to force a rebuild."""
    global _cached
    _cached = store


def migrate_receipts(source: LocalStorage, dest) -> dict:
    """Copy every local receipt file to ``dest`` under the same key.

    Each copy is verified by reading it back and comparing SHA-256.
    Idempotent: a destination object already holding identical bytes is
    skipped. The local files are NEVER deleted — rolling back is just
    flipping BOOKS_STORAGE_BACKEND back to local. Returns a report dict
    with copied/skipped/failed counts and per-key failure details.
    """
    copied = skipped = failed = 0
    failures = []
    keys = source.list_keys()
    for key in keys:
        data = source.read(key)
        digest = hashlib.sha256(data).hexdigest()
        try:
            if dest.exists(key) and dest.size(key) == len(data) \
                    and hashlib.sha256(dest.read(key)).hexdigest() == digest:
                skipped += 1
                continue
            dest.save(key, data, content_type_for(key))
            if hashlib.sha256(dest.read(key)).hexdigest() != digest:
                raise OSError(f"verification mismatch after copy of {key}")
            copied += 1
        except Exception as exc:  # keep going: one bad file must not strand the rest
            failed += 1
            failures.append({"key": key, "error": str(exc)})
    return {"total": len(keys), "copied": copied, "skipped": skipped,
            "failed": failed, "failures": failures}
