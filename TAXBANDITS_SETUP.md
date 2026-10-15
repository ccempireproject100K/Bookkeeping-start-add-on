# TaxBandits setup — 1099-NEC e-filing

The app tracks contractors, W-9 status, and payments itself. **E-filing with the
IRS happens through TaxBandits, never in-house.** Without the keys below, the
e-file endpoints return a clear "not configured" message and everything else
(contractor tracking, year summaries, the 1099 preview) works normally.

## 1. Create a TaxBandits account

1. Sign up at https://www.taxbandits.com (developer docs: https://developer.taxbandits.com/docs/).
2. In the dashboard, create an API app to get:
   - **Client ID** and **Client Secret**
   - **User Token** (identifies your TaxBandits user/console)
3. Create (or select) the **Business** (payer) that will file the 1099s and note its **Business ID**.
   The payer's legal name, EIN, and address live in TaxBandits — the app sends only the recipient + amounts.

## 2. Add keys to the server environment

```bash
TAXBANDITS_CLIENT_ID=...
TAXBANDITS_CLIENT_SECRET=...
TAXBANDITS_USER_TOKEN=...
TAXBANDITS_BUSINESS_ID=...
TAXBANDITS_SANDBOX=true     # true = testapi.taxbandits.com (no real filings); false = api.taxbandits.com (live)
```

Restart the backend after adding them.

## 3. How the integration works

- **Auth:** the backend builds a short-lived JWT signed with your Client Secret and exchanges it at
  `POST {base}/OAuth/Token` (client-assertion flow) for a Bearer token.
- **Create:** `POST /v2/Form1099NEC/Create` with the payer Business ID, tax year, recipient
  (name, TIN, address), and Box 1 nonemployee compensation = the contractor's tracked total for the year.
- **Transmit is manual:** the app creates the return in TaxBandits as a draft. You review it and click
  **Transmit** inside TaxBandits to actually file with the IRS. This is deliberate — filing is
  irreversible and should always have human eyes on it.
- **Status:** the app can poll `GET /v2/Form1099NEC/Status?SubmissionId=...` to show IRS acceptance.

## 4. Sandbox first

Keep `TAXBANDITS_SANDBOX=true` and run a full cycle (create → draft PDF → transmit in sandbox)
before going live. Switch to `false` only when you're ready to file for real.

## 5. Costs

TaxBandits charges per form transmitted (see their pricing). Creating drafts is free.
