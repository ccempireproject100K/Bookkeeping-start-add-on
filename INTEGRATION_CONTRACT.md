# Momo ⇄ Books Integration Contract (v1 — defined 2026-10-17, Track A)

**Status: FROZEN at definition time.** Any change after this file is written
must be flagged in the Track A final report under "contract changes".

This contract is implemented by the **books** side
(`ccempireproject100K/Bookkeeping-start-add-on`, branch
`audit/idempotency-integration-auth`) and consumed by the **Momo** side
(sibling crew). It replaces the pasted 7-day owner JWT with dedicated
integration credentials and makes the books receiving side enforce the
`Idempotency-Key` header Momo already sends.

All paths are relative to the books API base. The API router is mounted
under `/api`, so canonical paths are `/api/customers`, `/api/invoices`,
etc. (Momo-side: verify the configured base URL includes the `/api`
segment — flagged as a Momo-side check, not changed by books.)

---

## 1. Integration credentials (books side)

### 1.1 Credential format

A credential is a single opaque string, presented exactly like the old
JWT (same `Authorization` header), so Momo's provider changes are
minimal:

```
Authorization: Bearer mae1.<client_id>.<secret>
```

- `mae1` — format/version prefix. Books auth detects this prefix and
  routes to integration authentication instead of JWT decoding.
- `client_id` — public identifier, `ci_` + 24 hex chars. Safe to log.
- `secret` — 43-char URL-safe random secret (256-bit). **Shown once** at
  issuance/rotation; books stores only `SHA-256(secret)` and compares
  with a constant-time digest compare. The secret is never returned by
  any other endpoint, never logged, and never accepted anywhere except
  the `Authorization` header.

### 1.2 Storage & lifecycle (books `integration_credentials` collection)

Fields: `id`, `tenant_id`, `client_id` (unique index), `secret_hash`,
`label`, `scopes` (always `["books:sync"]` in v1), `created_at`,
`created_by`, `expires_at`, `revoked_at`, `rotated_at`, `last_used_at`.

- **Issuance** — workspace owner only (JWT auth, `ledger:admin` role):
  `POST /api/integrations/credentials`
  body `{ "label": "Momo sync", "expires_in_days": 365 }`
  (`expires_in_days` optional, default 365, allowed 1–730).
  Response `200`:
  ```json
  { "id": "...", "client_id": "ci_…", "credential": "mae1.ci_….<secret>",
    "label": "Momo sync", "scopes": ["books:sync"],
    "created_at": "…", "expires_at": "…" }
  ```
  The `credential` field appears **only** in this response (and in the
  rotate response). Owners copy it into Momo's bookkeeping-link settings
  in place of the old JWT.
- **List** — `GET /api/integrations/credentials` (owner JWT): metadata
  only (`id`, `client_id`, `label`, `scopes`, `created_at`,
  `expires_at`, `revoked_at`, `last_used_at`). Never any secret/hash.
- **Revoke** — `POST /api/integrations/credentials/{id}/revoke` (owner
  JWT). Sets `revoked_at`; the credential is rejected from that moment.
- **Rotate** — `POST /api/integrations/credentials/{id}/rotate` (owner
  JWT). Same `client_id`, **new secret, old secret dies immediately**.
  Response shape = issuance response (new `credential` shown once).
  Rotation flow for the operator: rotate in books → paste the new
  credential into Momo → done. There is no grace window in v1.
- **Expiry** — credentials expire at `expires_at`; expired credentials
  are rejected with `401`. Owners rotate/replace before expiry; Momo
  surfaces the 401 as "bookkeeping connection needs attention", the
  same way it already surfaces JWT expiry.

### 1.3 Authentication semantics

- A valid integration credential authenticates an **integration actor**
  bound to exactly one tenant (`tenant_id` on the credential). It is
  **not** a user: it has role `integration`, which appears in **no**
  entry of the user permission map.
- The credential is accepted **only** on the sync surface (§2). On any
  other endpoint it is not a valid JWT and is rejected `401`
  (existing behavior of the JWT decoder — unchanged).
- Cross-tenant use is impossible by construction: the tenant comes
  from the credential record, never from the request. A credential for
  tenant A can only ever read/write tenant A's data.
- Rejection reasons (all `401`, `detail` string): malformed token,
  unknown client, bad secret (generic `"Invalid integration
  credential"` — does not reveal which part failed), `"Integration
  credential revoked"`, `"Integration credential expired"`.
- Successful authentications stamp `last_used_at` on the credential.

### 1.4 Health / identity check

`GET /api/integrations/me` — **integration credential required**
(a user JWT is rejected here with `401`).

```json
{ "ok": true, "client_id": "ci_…", "label": "Momo sync",
  "scopes": ["books:sync"], "tenant_id": "…", "tenant_name": "…",
  "expires_at": "…" }
```

Momo's `test_connection` should call this endpoint when the configured
token starts with `mae1.` (and keep calling `GET /api/auth/me` for
legacy JWT tokens during migration).

### 1.5 Sync monitoring (owner)

`GET /api/integrations/sync-records?limit=100` (owner JWT) — recent
idempotency/sync records for the owner's tenant: `key`, `endpoint`,
`status` (`processing|succeeded|failed`), `response_status`,
`resource_id`, `error`, `actor`, `attempts`, timestamps. This is the
connection-health/failure surface for §3 records.

---

## 2. Sync surface (the only endpoints an integration credential may call)

| Endpoint | JWT path (unchanged) | Integration path |
|---|---|---|
| `POST /api/customers` | user with `ops:manage` | credential, scope `books:sync` |
| `POST /api/invoices` | user with `ops:manage` | credential, scope `books:sync` |
| `POST /api/invoices/{invoice_id}/pay` | user with `ops:manage` | credential, scope `books:sync` |
| `GET /api/integrations/me` | rejected (401) | credential |

Request/response bodies are **unchanged** from the current REST
contract (the same payloads Momo's `MindAtEaseProvider` already sends:
customer `{name, email?, phone?}`; invoice `{client_name, lines[],
tax, terms, customer_id?, due_date?}`; pay `{payment_method, amount}`).
Journal entries created via an integration actor record
`created_by = integration+<client_id>@system.local` for auditability.

## 3. Idempotency enforcement (books receiving side)

Applies to the three sync endpoints above, for **both** auth paths,
whenever the request carries an `Idempotency-Key` header. Requests
without the header behave exactly as before (UI flows unaffected).

- **Record** — one document per `(tenant_id, key)` in
  `idempotency_records`, protected by a **unique index** on
  `(tenant_id, key)`. Fields: `key`, `tenant_id`, `endpoint`
  (`"POST /customers"` etc.), `request_hash`, `status`,
  `response_status`, `response_body`, `resource_id`, `error`,
  `actor`, `attempts`, `created_at`, `updated_at`, `completed_at`.
- **Request hash** — `SHA-256` of the canonical JSON of
  `{method, path, body}` (path includes the invoice id for payments).
- **First delivery** — the record is claimed atomically (unique-index
  insert, `status=processing`) before the handler runs; on success the
  record stores the response (`status=succeeded`); on a handled error
  the record is marked `failed` with the error detail so a later retry
  may re-execute (see below).
- **Duplicate delivery (same key, same payload)** — the stored response
  is returned verbatim with header `Idempotency-Replayed: true`. No
  second customer/invoice/payment/journal entry is created.
- **Key reuse with a different payload** — `409` with
  `detail = "Idempotency-Key was already used with a different request payload"`.
- **Concurrent duplicate** — the unique index admits exactly one
  claim; the other request waits (bounded poll, ~15 s) for the outcome
  and then replays the stored response (or the mismatch 409). If the
  first request is still processing after the bound, the second gets
  `409` `"A request with this Idempotency-Key is still in progress"`.
- **Interrupted request / safe retry** — resources created by a keyed
  request are stamped with the key (`sync_key` on the customer/invoice
  document; `sync_key` + the full result on the payment record; the
  journal entry receives the key via the ledger's existing
  `idempotency_key` field). If a first attempt died after creating the
  resource but before the response was recorded, a retry with the same
  key finds the stamped resource and returns it instead of creating a
  duplicate. A `failed` record may be re-claimed by a retry; a
  `processing` record older than 120 s is treated as abandoned and
  may be taken over.
- **Tenant scoping** — keys are unique per tenant; the same key string
  under two tenants is two independent records.

## 4. What Momo must do to consume this (client contract)

1. Settings: keep storing one opaque token string per tenant (the
   existing bookkeeping-link token field works unchanged) — the value
   is now the `mae1.…` credential instead of a JWT. No 7-day expiry
   interruption; default credential life is 365 days.
2. `test_connection`: if the token starts with `mae1.`, call
   `GET /api/integrations/me`; otherwise keep `GET /api/auth/me`.
3. Keep sending `Idempotency-Key` exactly as today (SHA-256 of
   tenant + local record + step). Books now honors it per §3:
   handle `Idempotency-Replayed: true` as success, and treat the
   payload-mismatch `409` as a hard error (it means the same local
   record produced a different payload — a bug, not a retry case).
4. Rotation runbook for support: owner rotates in books (Integrations
   settings) → pastes the new credential into Momo → old secret is
   already dead, so paste promptly; sync resumes on the next push
   (idempotency makes the catch-up safe).

## 5. Explicitly out of scope / unchanged

- All user authentication (JWT, cookies, roles, 2FA) is untouched.
- No QuickBooks anything. The CSV bridge is untouched.
- Sync payload shapes, money handling (dollars at the edge, cents
  inside), and the set of synced record types (customers, invoices,
  payments — no expenses) are unchanged.

---

## Addendum 2026-11-05 — procurement sync (v1.1)

**Status: POST-FREEZE EXTENSION, flagged explicitly per this contract's
own rule** (owner ruling 2026-11-05: orders and supplier bills from the
AI Office parts system flow into Books as bills owed / AP, and the
materials spend lands on the job's cost record). This addendum adds ONE
endpoint to the sync surface; §§1–5 above are unchanged. Implemented in
`backend/routers/procurement_sync.py`, tested by
`backend/tests/test_procurement_sync.py`.

### Endpoint

`POST /api/integrations/procurement/bills` — sits alongside the existing
sync surface, next to `GET /api/integrations/me` (the integrations
router family).

- **Auth:** the standard sync actor (`get_sync_actor`) — a dedicated
  integration credential with the `books:sync` scope (§1), or a user
  session with `ops:manage`. The tenant always comes from the
  credential/session, never from the request. Missing/invalid
  credentials are `401`; a valid credential without `books:sync`
  (e.g. a `referrals:sync` platform credential) is `403`.
- **Body** (dollars at the edge, integer cents in storage, as §5):

  ```json
  { "source": "aioffice",
    "source_bill_id": "<AI Office supplier-bill id>",   // required
    "source_po_id": "<AI Office PO id or null>",
    "supplier_invoice_no": "<string or null>",
    "vendor_name": "<string>",
    "vendor_external_id": "<AI Office supplier id or null>",
    "vendor_is_1099": false,
    "bill_date": "YYYY-MM-DD or null", "due_date": "YYYY-MM-DD or null",
    "lines": [{ "description": "...", "amount": 123.45,
                "expense_code": "5700 or null",
                "job_ref": "<AI Office job/appointment id or null>",
                "job_number": "<or null>",
                "customer_name": "<or null>" }] }
  ```

- **Effect — one entry per spend (hard rule):** the bill posts exactly
  ONE journal entry, the bill's own, under the `create_bill`
  discipline (Dr per-line expense / Cr `2010` Accounts payable,
  source `"bill"`) and lands as an open AP bill (`status: "open"`,
  `kind: "procurement"`). Per line, the expense code is the explicit
  `expense_code` when given, else `5000` (job materials) when the line
  carries a `job_ref`, else `5700`. Job-linked lines additionally
  create `job_costs` rows (category `material`, grouped per `job_ref`)
  that reference the bill (`bill_id`) and the bill's single entry
  number — **no second journal entry is ever posted for the same
  spend**, preserving the jobs module's one-entry rule, so the job's
  profit/cost reads include the materials amount exactly once.
- **Vendor resolution:** an existing non-deleted vendor matched by
  `external_ref` (the stored `vendor_external_id`) first, else by
  normalized name (trim / collapse whitespace / lowercase), else a new
  vendor is created `{name, kind: "supplier", external_ref,
  is_1099 + form_1099: vendor_is_1099, source:
  "aioffice_procurement"}`. When a push asserts `vendor_is_1099`, the
  flag is set on the matched/created vendor; it is never unset by a
  later push that omits it.
- **Job resolution:** a Books job matched by `jobs.external_ref ==
  job_ref`, else a minimal shell is created `{job_number: payload
  job_number or "EXT-" + short hash of job_ref, name/customer_name:
  payload customer_name or "AI Office job", external_ref: job_ref,
  source: "aioffice_procurement", status: "active",
  contract_amount_cents: 0}`.
- **Idempotency:** §3 applies unchanged — `Idempotency-Key` claims a
  record under endpoint label `POST /integrations/procurement/bills`;
  a duplicate delivery replays the stored response verbatim
  (`Idempotency-Replayed: true`), and the created bill is stamped
  `sync_key` so a crash-window retry returns the original bill.
  Additionally, `(tenant, source, source_bill_id)` is a natural dedupe
  anchor: a push with no idempotency key whose `source_bill_id` was
  already received returns the original bill's response with
  `"deduplicated": true` instead of creating a second bill.
- **Response 200:** `{bill_id, entry_number, total_cents, vendor_id,
  job_costs: [{job_id, job_cost_id, amount_cents}], deduplicated}`.
- **Errors:** `422` for a missing `source_bill_id`, no lines, a
  non-positive line amount, or a malformed date; ledger validation
  (unknown/archived expense account) surfaces as its usual `422`.
  Validation failures never post.
