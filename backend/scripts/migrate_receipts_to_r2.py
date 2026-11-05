"""Copy the existing local receipt files into Cloudflare R2.

Run this ONCE when flipping receipt storage to R2 (see STORAGE.md at
the repo root for the full procedure), after the BOOKS_R2_* values
exist in the environment. Every file under backend/uploads/receipts/
is copied to the bucket under the same key (<tenant_id>/<file>), and
each copy is verified by reading it back and comparing SHA-256.

Idempotent by construction: files whose identical bytes already sit in
the bucket are skipped, so re-running after a partial failure simply
finishes the job. Local files are NEVER deleted — rolling back is just
BOOKS_STORAGE_BACKEND=local plus a redeploy.

Usage (from the backend directory, with the deployment's env):
    BOOKS_R2_BUCKET=… BOOKS_R2_ENDPOINT=… BOOKS_R2_ACCESS_KEY_ID=… \
    BOOKS_R2_SECRET_ACCESS_KEY=… python scripts/migrate_receipts_to_r2.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import receipt_storage  # noqa: E402


def main() -> int:
    missing = receipt_storage.r2_missing(os.environ)
    if missing:
        print("Missing R2 configuration: " + ", ".join(missing)
              + " — nothing copied.")
        return 2
    dest = receipt_storage.build_receipt_storage(
        {**os.environ, "BOOKS_STORAGE_BACKEND": "r2"})
    source = receipt_storage.LocalStorage(receipt_storage.default_local_root())
    report = receipt_storage.migrate_receipts(source, dest)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["failed"]:
        print(f"\n{report['failed']} file(s) FAILED to copy — fix the "
              "cause and rerun; already-copied files will be skipped.")
        return 1
    print(f"\nDone: {report['copied']} copied, {report['skipped']} "
          "already identical, 0 failed. Local files were left in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
