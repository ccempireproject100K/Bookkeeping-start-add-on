# TOKEN_ENCRYPTION_KEY rotation procedure

The app encrypts PII with a single Fernet key held in the `TOKEN_ENCRYPTION_KEY`
environment variable (`backend/lib/pii.py`). Rotating it re-encrypts every
stored value under a new key. **Do not rotate casually** — a botched rotation
locks users out of 2FA and breaks bank syncs. Read the whole procedure first.

## What is encrypted with this key

| Data | Where | Consequence if unreadable |
|---|---|---|
| Plaid bank access tokens | `bank_connections.access_token_enc` | Bank feeds stop syncing |
| TOTP 2FA secrets | `users.totp_secret_enc`, `users.totp_pending_enc` | Users cannot complete 2FA login |
| Contractor tax IDs | `contractors.tax_id_enc` | 1099 workflow breaks |
| Team payout account + routing | `users.payout_account_enc`, `users.payout_routing_enc` | Payroll payouts break |
| Encrypted backup exports | Offsite backup blobs (`lib/offsite.py`, `routers/backup.py`) | Backups unrestorable |

Fernet tokens are recognizable: they start with `gAAAAA`.

## Preconditions

1. The app is running on the OLD key and healthy (health check `/api/health`
   returns `"db": "ok"`).
2. You have MongoDB write access (same connection string the app uses).
3. You have generated the NEW key but have NOT set it in the server env yet:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

## Procedure (backup-first)

### Step 0 — Announce & freeze
Pick a low-traffic window. Writes during rotation are fine (the script holds
both keys), but keep the window short.

### Step 1 — Full backup (mandatory)
Take a MongoDB dump AND trigger an encrypted app backup (`routers/backup.py`).
Verify the dump restores into a scratch database — an unverified backup is not
a backup. Keep this dump until Step 5 is complete; it is the rollback path.

### Step 2 — Dry run
Copy the script outline in Step 4 into `scripts/rotate_encryption_key.py`,
point it at a **copy** of production data (restore the Step-1 dump to a
scratch DB), run it with `--dry-run`, and confirm it reports every encrypted
field it would touch and zero failures.

### Step 3 — Live re-encryption (old key still in server env)
Run the script against production with `--live --old-key ... --new-key ...`.
It decrypts each field with the OLD key and re-encrypts with the NEW key,
updating documents in place. It processes collections in this order:
`users` (totp, payout), `contractors` (tax_id), `bank_connections`
(access tokens). It stops on the first decryption failure so you can
investigate rather than half-rotating.

### Step 4 — Script outline (dry-run-safe)
```python
# scripts/rotate_encryption_key.py  (outline — review before running)
import argparse
from cryptography.fernet import Fernet, InvalidToken

FIELDS = [
    ("users", ["totp_secret_enc", "totp_pending_enc",
               "payout_account_enc", "payout_routing_enc"]),
    ("contractors", ["tax_id_enc"]),
    ("bank_connections", ["access_token_enc"]),
]

def rotate(db, old: Fernet, new: Fernet, live: bool) -> dict:
    report = {"updated": 0, "skipped": 0, "failed": []}
    for coll_name, fields in FIELDS:
        for doc in db[coll_name].find():
            patch = {}
            for f in fields:
                tok = doc.get(f)
                if not tok or not str(tok).startswith("gAAAAA"):
                    continue  # empty or not a Fernet token; leave alone
                try:
                    plain = old.decrypt(tok.encode())
                except InvalidToken:
                    report["failed"].append(f"{coll_name}/{doc.get('id')}:{f}")
                    return report  # STOP on first failure; investigate
                patch[f] = new.encrypt(plain).decode()
            if not patch:
                report["skipped"] += 1
                continue
            if live:
                db[coll_name].update_one({"_id": doc["_id"]}, {"$set": patch})
            report["updated"] += 1
    return report
# --dry-run: run with live=False against a restored copy; assert failed == []
# --live:    run with live=True against production; assert failed == []
```
Notes:
- Values that do not start with `gAAAAA` are left untouched (they are either
  empty or plaintext leftovers the in-app migration endpoint handles).
- Offsite backup blobs encrypted with the old key are NOT re-encrypted —
  keep the old key archived (see Step 5) so old backups stay restorable.
- The script never prints keys or plaintext. Run it from a machine with the
  same network access as the app, never from a shared shell history with the
  keys inline — pass keys via env vars or a prompt.

### Step 5 — Switch the server key
Only after the live run reports zero failures:
1. Set `TOKEN_ENCRYPTION_KEY` to the NEW key in the server env (Render:
   dashboard → service → Environment; the service redeploys).
2. Smoke-test: log in with a TOTP account, view a contractor tax ID (pii:read),
   trigger a bank-feed sync on one connection.
3. **Archive the OLD key** in the secrets vault labeled with the rotation
   date — old encrypted backup blobs still need it for restore. Retire it
   only when all backups encrypted under it have aged out.

### Step 6 — Verify & close
Watch error logs for 24h for `InvalidToken` / "Could not decrypt" messages.
If clean, delete the scratch DB from Step 2 and close the maintenance window.

## Rollback plan

- **Rotation script failed mid-run:** nothing was switched; investigate the
  reported document, fix data (usually a plaintext value where a token was
  expected), re-run from scratch. The server still runs on the OLD key.
- **New key deployed but decryption errors appear:** set
  `TOKEN_ENCRYPTION_KEY` back to the OLD key and redeploy immediately —
  all values are still old-key-encrypted unless the Step-3 run completed.
- **Step-3 completed but data is corrupt:** restore the Step-1 dump to
  production, keep the OLD key in the server env, and start over. This is why
  the dump is kept until Step 6 passes.
- **Old key lost:** there is no recovery — Fernet has no backdoor. The vault
  archive in Step 5 is the only copy. Treat the old key like production data.

## Rotation cadence

Rotate on a schedule (yearly is reasonable) and immediately if the key may
have been exposed (logged, committed, shared). After any emergency rotation,
also rotate the Plaid secrets and any API keys that shared an env file with it.
