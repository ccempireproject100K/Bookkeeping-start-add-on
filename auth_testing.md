# Auth Testing — The Barber's Ledger

Custom email/password auth with httpOnly JWT cookie (`access_token`), `SameSite=Lax`, `Secure`.
Frontend and backend are the SAME origin (served via ingress), so Lax cookies are sent on same-origin XHR.

## Quick API check
```
API=https://bookkeeping-api-ymh6.onrender.com/api
curl -s -c c.txt -X POST $API/auth/login -H "Content-Type: application/json" \
  -d '{"email":"onelovellc@outlook.com","password":"Owner123!"}'
curl -s -b c.txt $API/auth/me        # -> 200 with user + tenant
```

## Role matrix (enforced server-side via permission map)
- owner: everything (post/approve/reverse, periods, accounts, POS, invoices, cash close)
- accountant: READ ledger + reports only (create JE -> 403)
- bookkeeper: read + create DRAFT journal entries (cannot approve/post -> stays draft)
- barber: POS /pos/sell only; ledger read -> 403

## Key invariants to verify
- Trial balance `balanced=true`, `total_debit==total_credit`.
- Balance sheet `balanced=true` (Assets == Liabilities + Equity + Net Income).
- Posting into a LOCKED fiscal period -> HTTP 423.
- Quick Sell decrements stock atomically; out-of-stock -> HTTP 409.
- Reversing a posted entry creates a linked reversal and flips the original to `reversed`.
