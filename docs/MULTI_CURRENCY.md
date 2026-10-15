# Multi-Currency Support

The bookkeeping app supports 20 foreign currencies end to end. Built in four
phases (PRs #107–#110), each ending at a clean stopping point.

## Supported currencies

USD, EUR, JPY, GBP, CNY, AUD, CAD, CHF, HKD, SGD, SEK, NZD, MXN, INR, BRL,
ZAR, KRW, TWD, THB, MYR — all 20 support Stripe presentment (charging).
Metadata lives in `backend/lib/currencies.py`; adding a currency is one line.

## What each phase built

**Phase 1 — data model (PR #107).** Every money document (journal entries,
invoices, bills, bank accounts) carries an ISO 4217 `currency` code defaulting
to USD. Workspaces have a `base_currency` setting (default USD). Existing data
was backfilled to USD via an idempotent startup migration. Minor-unit helpers
(`to_minor_units` / `from_minor_units`) handle zero-decimal currencies
(JPY, KRW have no cents). USD-only behavior is unchanged.

**Phase 2 — exchange rates (PR #108).** Daily rates from the European Central
Bank's free euro-reference feed (no API key), stored in the `rates` collection
as `{base, quote, date, rate, source}`. A scheduler job refreshes rates daily
(`ENABLE_SCHEDULER=true`, `FX_RATES_ENABLED=true`); the first run backfills
30 days of history. `fx.get_rate` / `fx.convert` resolve any pair via EUR
cross-rates, using the latest rate on or before the transaction date.

**Phase 3 — FX gain/loss postings (PR #109).** When a foreign-currency
invoice/bill is paid at a different rate than its issue date, the difference
posts automatically to **4080 Foreign Exchange Gain** (income) or **5960
Foreign Exchange Loss** (expense). `POST /api/fx/revalue` runs month-end
unrealized revaluation of open foreign balances (idempotent per date).
Every FX posting records both currencies, both rates, both dates, and the
computed difference for audit.

**Phase 4 — reports, UI, Stripe (PR #110).** P&L, balance sheet, and trial
balance accept `?currency=` (default: workspace `base_currency`); each
foreign-currency line converts at its own transaction-date rate, and responses
carry `currency` + `translated` metadata. The invoice/bill forms and workspace
settings have currency pickers (`GET /api/currencies`); amounts render with
the right symbol and decimals (¥500, not ¥500.00). Stripe Checkout charges in
the **invoice's** currency using proper minor units.

## The FX accounts

| Code | Name | Type |
|------|------|------|
| 4080 | Foreign Exchange Gain | income |
| 5960 | Foreign Exchange Loss | expense |

Created automatically on first use, per workspace.

## What's still manual

- **Base currency changes are prospective.** Switching `base_currency` affects
  future translations; historical entries keep their original currencies
  (by design — translation happens at report time).
- **Rate source is ECB only.** If the ECB feed is unreachable, the scheduler
  logs and skips; reports raise a clear error when no rate exists for a date.
  A commercial FX provider (OANDA, XE) can replace the feed later.
- **Zero-decimal edge in cross-currency FX.** Internal amounts use a x100
  convention from Phase 1; same-currency flows are exact, and 2-decimal
  cross-currency FX is exact. Cross-currency payments involving zero-decimal
  currencies (JPY, KRW) in the Phase 3 realized-FX path use proper minor
  units at the Stripe boundary but the x100 convention inside the GL —
  review before relying on it for large JPY/KRW cross-currency settlements.
- **No automatic hedging or forward contracts.** Unrealized revaluation is
  owner-triggered, not scheduled.
