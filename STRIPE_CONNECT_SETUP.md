# Stripe Connect invoice payments — setup

Customers pay an invoice online and the money lands in **your own
Stripe account** — Empire is never merchant of record and never holds
the funds. Invoices get a **pay link** (card or bank/ACH); the
customer pays on Stripe's hosted checkout; a signature-verified
Connect webhook marks the invoice paid in your books exactly once
(Dr 1050 Stripe Clearing / Cr 1200 AR).

Design: Trade Circle `docs/invoice-payments.md`.

Everything is **key-gated**: until the platform keys below are set,
the pay-link and onboarding endpoints return a graceful `503` and
the webhook refuses every call. Nothing else is affected — manual
payment recording keeps working.

## 1. Platform Stripe account

Invoice payments run on Stripe Connect. The platform needs its own
Stripe account (this is the platform's API identity for operating
Connect — on a direct charge the funds never enter the platform's
balance):

1. In the Stripe dashboard, enable **Connect** (Settings → Connect).
2. Copy the **Secret key** (`sk_test_…` / `sk_live_…`) from
   Developers → API keys. Start in test mode.

## 2. Environment variables

On the server (Render → bookkeeping-api → Environment), never in
chat or in the repo:

```bash
STRIPE_SECRET_KEY=sk_test_...            # required — enables Connect endpoints
STRIPE_CONNECT_WEBHOOK_SECRET=whsec_...  # required — webhook signature secret

# Fee schedule (defaults shown; override only deliberately — see the
# design doc's fee table before touching these)
INVOICE_CARD_APP_FEE_BPS=20              # our take on card: 0.2%
INVOICE_ACH_TOTAL_BPS=100                # member's all-in ACH price: 1.0%
INVOICE_ACH_TOTAL_CAP_CENTS=1000         # ... capped at $10.00
```

## 3. Register the Connect webhook endpoint

In the Stripe dashboard → Developers → Webhooks → Add endpoint,
listening to **Connect** events:

- **URL:** `https://bookkeeping-api-ymh6.onrender.com/api/invoice-payments/stripe/webhook`
- **Events:** `checkout.session.completed`,
  `checkout.session.async_payment_succeeded`,
  `checkout.session.async_payment_failed`, `charge.refunded`,
  `charge.dispute.created`, `account.updated`,
  `account.application.deauthorized`

Copy the endpoint's **Signing secret** (`whsec_…`) into
`STRIPE_CONNECT_WEBHOOK_SECRET`. Without it the endpoint refuses
every call (503); a bad signature gets a 400. An unverifiable
webhook never moves money.

## 4. Each workspace connects its own Stripe

Pay links run on the **workspace's** Stripe, not the platform's.
The workspace owner onboards once from the books app
(`POST /api/invoice-payments/connect/start`, owner role): Stripe
hosts the onboarding (Express account), and the workspace's Connect
state (account, charges enabled) is stored on the workspace record.
Until a workspace has connected Stripe with charges enabled,
pay-link creation for its invoices answers honestly (409) with the
connect state — never a dead link.

## 5. Fees (what the member pays)

The customer always pays exactly the invoice amount; fees come out
of the member's proceeds, like Square/Wave/Stripe Invoicing:

- **Card:** Stripe 2.9% + 30¢ + our 0.2% → member's total
  **3.1% + 30¢** (Stripe Invoicing charges +0.4%; PayPal 3.49% + 49¢).
- **ACH:** **1.0% capped at $10 all-in** — Stripe's 0.8% (cap $5)
  plus our spread.

Refunds and disputes happen in the member's own Stripe and are
recorded on the invoice as states; reflect a refund in the ledger
with the existing invoice refund endpoint.
