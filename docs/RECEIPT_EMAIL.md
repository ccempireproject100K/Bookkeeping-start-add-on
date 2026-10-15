# Receipts by email

Snap a receipt photo on your phone, email it to your workspace's inbound
address, and it lands in the receipt inbox — filed exactly like a manual
upload. Open the inbox, scan it with AI, review, and post as an expense.

## How it works

`POST /api/receipts/inbound` accepts inbound-email webhooks from Mailgun,
SendGrid, Cloudflare, or anything that POSTs a multipart form with the email
fields plus attachments (JPG, PNG, WEBP, PDF — 10 MB max each).

The endpoint:

1. **Verifies a shared secret** — `INBOUND_EMAIL_SECRET`, passed as
   `?secret=...` or the `X-Inbound-Secret` header. No secret, no entry.
   (Mailgun's HMAC signature is also verified when `MAILGUN_WEBHOOK_KEY`
   is set.)
2. **Matches the mail to a workspace** — first by the recipient address
   (`receipts+<token>@<domain>`, token from `GET /api/receipts/inbound-address`),
   then by the sender's email address against user accounts.
3. **Files each attachment** through the same ingestion path as manual
   uploads, tagged `source: "email"`.
4. **Dedupes** on the provider's `Message-Id` — re-deliveries return 200
   with `duplicate: true` instead of filing twice.

## Setup

1. Set the env vars on the backend:
   - `INBOUND_EMAIL_SECRET` — long random string, shared with the provider config.
   - `INBOUND_EMAIL_DOMAIN` — the domain you receive mail on (e.g. `in.mail.yourdomain.com`).
   - `MAILGUN_WEBHOOK_KEY` — optional; enables Mailgun signature checks.
2. As the workspace owner, call `GET /api/receipts/inbound-address` to get
   your `receipts+<token>@<domain>` address (rotate it anytime with
   `POST /api/receipts/inbound-address/rotate`).

### Mailgun

Routes → Create route → forward to webhook URL:

```
https://api.yourdomain.com/api/receipts/inbound?secret=YOUR_SECRET
```

Check "forward attachments". Mailgun signs every POST; set
`MAILGUN_WEBHOOK_KEY` to your HTTP webhook signing key for verification.

### SendGrid Inbound Parse

Settings → Inbound Parse → Add Host & URL:

- Hostname: your `INBOUND_EMAIL_DOMAIN`
- URL: `https://api.yourdomain.com/api/receipts/inbound?secret=YOUR_SECRET`
- Check "POST the raw, full MIME message" off; attachments arrive as
  parsed files either way.

Point the domain's MX records at `mx.sendgrid.net`.

### Cloudflare Email Routing / Workers

Email Routing alone can't POST webhooks — add a Worker on the route that
forwards the message as multipart form data to the same URL (fields
`sender`, `recipient`, `subject`, plus each attachment).

## Notes

- Unroutable mail (unknown sender/recipient) returns 200 with
  `"ok": false` so providers don't retry mail that can never be filed.
- Filing failures (bad file type/size) are reported per attachment in
  `skipped`; transient errors return 5xx so the provider retries.
- The webhook files receipts with status `uploaded` — AI scanning still
  runs on demand from the inbox (`POST /receipts/{id}/scan`).
