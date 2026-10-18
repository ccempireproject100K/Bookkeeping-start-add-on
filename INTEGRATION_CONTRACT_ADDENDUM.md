# Integration Contract — Addendum A: Invoice Refund Endpoint

**Status: POST-FREEZE ADDITION — proposed for owner ratification.**
`INTEGRATION_CONTRACT.md` is frozen (Track A, PR #144); this addendum
does not edit it. It specifies one endpoint added by the
financial-sync fix track (2026-10-17) because the frozen sync surface
(§3) had no way to reflect a refund: the existing customer-credit
refund (`POST /customer-credits/{credit_id}/refund`) is owner-JWT
only and settles a credit balance (2210), not a payment taken against
an invoice. Track E (PR #102) confirmed the gap live: a Stripe refund
processed in Momo left the books invoice showing paid.

## Endpoint

`POST /invoices/{invoice_id}/refund`

- **Auth:** the standard sync actor — a Track A integration
  credential (`mae1.<client_id>.<secret>`) scoped to its tenant, or
  the tenant owner's JWT (contract §1). Tenant is always derived from
  the credential record, never from the request. A refund against
  another tenant's invoice is a **404** (the invoice does not exist
  in the caller's scope).
- **Idempotency:** contract §4 applies unchanged. `Idempotency-Key`
  required for replay protection; the endpoint's registry name is
  `POST /invoices/refund`. Same key + semantically identical body →
  the recorded response is replayed, no second entry. Same key +
  different payload → **409** (§4.3). A payment record stamped with
  the key also completes crash-window retries exactly once (§4.2).
- **Body:** `{ "amount": <number, dollars, > 0>,
  "payment_method": "card" | "bank_transfer" | "check" | "cash" |
  "other" (default "card"), "refund_date": <optional ISO date> }`.
  Clients send the **delta** being refunded by this delivery (see
  client behavior below), not a cumulative total.
- **Effect (the only mutation):** one balanced journal entry —
  Dr `1200` Accounts Receivable / Cr cash account by payment method
  (card → `1050`, bank_transfer/check → `1020`, cash → `1010`,
  other → `1050`), source `"invoice_refund"` — plus one payment
  record on the invoice: negative `amount_cents`, `kind: "refund"`.
  Invoice status recomputes: fully refunded → back to `issued`
  (or `partial` if some payment remains); the invoice is never
  deleted or marked void by this endpoint.
- **Response 200:** `{ status, paid_cents, balance_cents,
  entry_number, refund_cents }` (amounts in cents; `entry_number` is
  what Momo records as the refund's provider id).
- **Errors:** 404 unknown/cross-tenant invoice; **422** when the
  amount is ≤ 0, exceeds the invoice's paid total, or the invoice
  has no payments to refund. Validation failures never post.

## Client behavior (Momo, for ratification alongside)

- Momo tracks `refunded_cents_synced` per invoice and pushes only the
  not-yet-synced delta of Stripe's cumulative `amount_refunded`, with
  key `idempotency_key(tenant, invoice, "refund",
  str(cumulative_cents))` — so duplicate webhook deliveries and
  redeliveries collapse at both layers, and sequential partial
  refunds each push their own delta under distinct keys.
- Known limitation, accepted for this addendum: a refund that
  arrives before the invoice's paid-sync has nothing to correct in
  books and is not replayed later (the shop's manual recovery is the
  books UI). Revisit if the owner wants a refund queue.

## Not changed by this addendum

- The frozen contract's dedup semantics (§4.2/§4.4): books' natural
  dedup 409 on `POST /customers` carrying `existing_id` is correct as
  specified. Fix F1 was entirely client-side (Momo now adopts
  `existing_id`); books' dedup and tenant isolation are untouched.
