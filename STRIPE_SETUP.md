# Stripe card payments (client portal) — setup

Customers can pay an invoice by card from their magic-link portal page
(**Pay … with card** → Stripe Checkout). When the payment completes, Stripe
calls our webhook, which records the payment and posts the journal entry
(Dr 1050 Stripe Clearing / Cr 1200 AR) exactly once.

Everything is **key-gated**: until the keys below are set, the checkout
endpoint returns a graceful `503` and the portal page shows "Card payments
aren't set up yet" instead of a dead button. Nothing else in the portal is
affected — manual payment recording keeps working.

## 1. Get Stripe credentials

1. Create or open your account at https://dashboard.stripe.com.
2. Start in **test mode** — switch to live keys after a sandbox payment
   cycle passes.
3. Copy the **Publishable key** (`pk_test_…`) and **Secret key**
   (`sk_test_…`) from Developers → API keys.

## 2. Set environment variables

On the server (Render → bookkeeping-api → Environment), never in chat or
in the repo:

```bash
STRIPE_SECRET_KEY=sk_test_...        # required — enables checkout
STRIPE_PUBLISHABLE_KEY=pk_test_...   # shown to the portal page (public by design)
STRIPE_WEBHOOK_SECRET=<redacted>     # required — see step 3
```

## 3. Register the webhook endpoint

In the Stripe dashboard → Developers → Webhooks → Add endpoint:

- **URL:** `https://bookkeeping-api-ymh6.onrender.com/api/portal/stripe/webhook`
- **Events:** `checkout.session.completed` and
  `checkout.session.async_payment_succeeded`

Copy the endpoint's **Signing secret** (`whsec_…`) into
`STRIPE_WEBHOOK_SECRET` above. The webhook is the only unauthenticated
write surface in this integration, so signature verification is
mandatory — without the secret the endpoint refuses every call (503),
and a bad signature gets a 400. An unverifiable webhook never moves money.

## 4. How it works

- `POST /portal/checkout/{token}` creates a Checkout Session for the
  invoice's outstanding balance, charged in the **invoice's currency**
  (multi-currency presentment — JPY/KRW convert with no minor units).
  Success/cancel return the customer to the portal page.
- The webhook marks the session paid and calls fulfillment, which claims
  the session atomically (a session can only be claimed once), posts the
  journal entry, appends the payment to the invoice with the Stripe
  session id as its reference, and marks the portal link used when the
  invoice is paid in full. Duplicate webhook deliveries are safe.
- If an invoice was already paid in full through another channel before
  the card payment lands, fulfillment records an error on the transaction
  instead of double-applying it — refund that card payment from the
  Stripe dashboard.

## 5. Go-live checklist

- [ ] Test-mode keys set on the API service; webhook registered (test mode).
- [ ] Sandbox cycle: portal link → Pay with card → test card
      `4242 4242 4242 4242` → invoice shows paid, journal entry posted.
- [ ] Repeat the webhook delivery from the Stripe dashboard → no
      duplicate payment/entry (idempotency check).
- [ ] Swap all three values to live keys + a live-mode webhook endpoint.
