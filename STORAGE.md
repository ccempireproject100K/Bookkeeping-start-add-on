# Receipt file storage — local disk or Cloudflare R2

Receipt files (the photos/PDFs behind the Receipt Inbox) are stored
through `backend/lib/receipt_storage.py`. Two backends share one
interface; the app always serves the bytes itself
(`GET /api/receipts/{id}/image`), so the bucket is never public and no
access rule changes with the backend.

| | Local (default) | Cloudflare R2 |
|---|---|---|
| Where files live | `backend/uploads/receipts/<tenant_id>/` | Private R2 bucket, keys `<tenant_id>/<file>` |
| Durable across deploys? | **No** — the host's disk is ephemeral | Yes |
| Config | nothing to set | env sheet below |

## Env sheet (backend service)

| Variable | Required | Meaning |
|---|---|---|
| `BOOKS_STORAGE_BACKEND` | no | `local` (default) or `r2` |
| `BOOKS_R2_BUCKET` | when `r2` | Bucket name, e.g. `books-receipts` |
| `BOOKS_R2_ENDPOINT` | when `r2` | `https://<account-id>.r2.cloudflarestorage.com` |
| `BOOKS_R2_ACCESS_KEY_ID` | when `r2` | R2 API token access key id |
| `BOOKS_R2_SECRET_ACCESS_KEY` | when `r2` | R2 API token secret |

Fail-loud rule: `BOOKS_STORAGE_BACKEND=r2` with any `BOOKS_R2_*` value
missing or empty **crashes the backend at startup**, naming the missing
variables. It never falls back to local disk silently — on an ephemeral
host that would mean receipts quietly vanishing on the next deploy.

## Flip procedure (local → R2)

Nothing in the code changes production behavior by itself: until the
env below is set, the backend keeps using local disk exactly as today.

1. **Owner, in the Cloudflare dashboard:** enable R2 on the account
   (until this is done the Cloudflare API answers error 10042 and no
   bucket can be created). Cloudflare may ask for a payment method on
   file even though the free tier covers expected use — R2's free
   tier (checked against Cloudflare's docs, 2026-11-05) includes
   10 GB-month of storage, 1M Class A and 10M Class B operations per
   month, and free egress; beyond that, storage is $0.015/GB-month.
2. Create the bucket (e.g. `books-receipts`) and an R2 API token with
   read/write access to that bucket only; keep the values for step 3.
3. Set the five env vars on the **bookkeeping-api** Render service
   with a merge-style env update (read the current vars, change only
   these, write back, read back to verify) — never a wholesale env
   replace. `BOOKS_STORAGE_BACKEND=r2` goes in last, in the same
   update. The deploy that follows restarts the backend; if any value
   were wrong, the startup check above fails the deploy loudly rather
   than serving receipts from the wrong place.
4. Copy the existing files once, from the backend directory with the
   same env present:
   `python scripts/migrate_receipts_to_r2.py`
   It verifies every copy by SHA-256 read-back, skips files already
   identical, and never deletes the local copies — rerunning it is
   safe and simply finishes an interrupted run.
5. Verify end to end: upload a receipt in the app, open its image,
   run a scan, delete one — then confirm the objects exist in the
   bucket under `<tenant_id>/` keys.

## Rollback

Set `BOOKS_STORAGE_BACKEND=local` on bookkeeping-api (same merge-style
update) and redeploy. Because the migration never deleted the local
files, everything uploaded before the flip is still on disk. Receipts
uploaded *while* R2 was live exist only in the bucket; to roll back
after live R2 use, first copy the bucket's objects back to
`backend/uploads/receipts/` under the same keys, then flip.

## Notes

- `boto3` was already a backend dependency (offsite backups use it);
  the R2 backend reuses it — no new packages.
- AI Office video-estimate files have the same local-disk exposure;
  that is a separate change in that codebase, not covered here.
