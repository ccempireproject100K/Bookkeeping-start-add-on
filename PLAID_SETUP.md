# Plaid bank feeds — setup

Live bank feeds connect a bank once (via Plaid Link) and sync transactions
automatically into the same `bank_transactions` staging collection the CSV
import uses, so the Banking page (categorize / match / reconcile) needs no
changes.

## 1. Get Plaid credentials

1. Create an account at https://dashboard.plaid.com and make a new app.
2. Start in **Sandbox** (free, fake banks) — switch to Production later after
   Plaid approves your use case (business verification required).
3. Copy the **Client ID** and **Secret** (Sandbox secret to start).

## 2. Set environment variables

On the server (or `backend/.env`):

```bash
PLAID_CLIENT_ID=your_client_id
PLAID_SECRET=your_sandbox_secret
PLAID_ENV=sandbox            # sandbox | development | production
TOKEN_ENCRYPTION_KEY=<key>   # see below
```

Generate the encryption key (used to encrypt Plaid access tokens at rest —
**never commit it**):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Without these keys, `GET /bankfeed/status` reports `configured: false` and the
Banking page shows setup instructions instead of the Connect button. Nothing
breaks — CSV import keeps working.

## 3. How it works

- `POST /bankfeed/link-token` → token for Plaid Link (frontend loads
  `https://cdn.plaid.com/link/v2/stable/link-initialize.js` on demand).
- `POST /bankfeed/exchange` swaps the Link `public_token` for an access
  token (Fernet-encrypted into `bank_connections`), and maps each Plaid
  account onto a local `bank_accounts` register (a free GL code from the
  1020/1021/... pool is picked automatically).
- `POST /bankfeed/sync` pulls added/modified transactions via
  `/transactions/sync`, negates Plaid's sign convention (Plaid: positive =
  money out; this app: positive = money in — do NOT "fix" this without
  checking both flows), applies the existing bank rules for category
  suggestions, and inserts into `bank_transactions` with `import_batch:
  "plaid"`. Plaid transaction ids dedupe re-syncs.
- `POST /bankfeed/webhook` (unauthenticated, logs only) records Plaid
  transaction webhooks; the next manual or cron `/sync` pulls the changes.
- `DELETE /bankfeed/connection/{id}` revokes the token at Plaid (best effort)
  and drops the connection. Already-imported transactions stay in the books.

## 4. Going to production

1. In the Plaid dashboard, request Production access and complete verification.
2. Set `PLAID_ENV=production` and swap in the production secret.
3. Rotate `TOKEN_ENCRYPTION_KEY` only when no connections exist (old tokens
   can't be decrypted with a new key — users would reconnect).

## Costs

- Plaid Sandbox: free.
- Plaid Production: usage-based pricing per connected account — check
  https://plaid.com/pricing and factor it into your plan pricing before
  promising "everything included."

## Security notes

- Access tokens are Fernet-encrypted in the `bank_connections` collection and
  never returned by the API (the status endpoint strips them).
- Importing and syncing require the `ops:manage` permission; status is
  `ledger:read`. The webhook endpoint intentionally takes no auth — it only
  logs; it cannot read or change anything.
