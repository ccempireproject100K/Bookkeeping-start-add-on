# Personal Finance Layer — Behavioral Spec + Feature Map

**Job:** owner ruling, voice call 2026-11-17 ~18:09 CST — build a personal-finance
layer INSIDE the bookkeeping app, clean-room, modeled on Sure's feature set, for
customers and for the owner himself.

**Clean-room fence (absolute):** Sure (we-promise/sure) is AGPL-3.0. This spec is
built ONLY from Sure's public docs/site behavior (as captured in
`~/workspace/buildout/sure-screen-2026-11-17/SCREEN-SURE.md`) and from our own
codebase. No Sure source code was read for this spec and none may be copied.
Analysis in, original code out.

**Repo:** ccempireproject100K/Bookkeeping-start-add-on, local lineage
`~/workspace/bookkeeping-private`, base = origin/main `5ea8024`.
**Branch:** `feat/personal-finance-layer` (own branch; DO NOT merge; do not touch
the pending merge train #229–#238 / #239).

Labels: **[seen]** = behavior documented in Sure's public docs/site per the
screen. **[inferred]** = reasonable reading of the feature's public description,
not directly documented. **[gap]** = we don't have it and this slice doesn't build it.

---

## 1. Behavioral spec — the Sure-modeled feature set

### F1. Net-worth dashboard across accounts — [seen]
One number: everything you own minus everything you owe, across bank accounts,
cards, loans, investments, crypto, property and debts, with the per-account
breakdown underneath it.
- Ours: assets = balances of asset-type GL accounts + manually-tracked assets
  (property, vehicle, other). Liabilities = liability-type GL balances + manual
  debts. Net worth = assets − liabilities, integer cents, as of today.

### F2. Day-by-day balance history — [seen]
Balances are rebuilt day by day (not just "current balance"), so net worth and
per-account balances can be charted over time.
- Ours: per bank account, replay the account's `bank_transactions` stream
  (signed cents) on top of a stored opening balance/date; emit one point per
  day from the first transaction (or opening date) to today. Net-worth history
  = sum of account series + manual assets/liabilities held flat at their
  current values (honest limit, labeled in the payload).

### F3. Cash-flow view — [seen]
Money in vs money out over a period, from the same transaction stream.
- Ours: per period (YYYY-MM or explicit range): total in (amount > 0), total
  out (amount < 0), net; transfers between own accounts excluded (see F7).

### F4. Budgets per category, with rollover — [seen]
A monthly amount per spending category; unspent (or overspent) remainder can
roll into next month instead of resetting to zero.
- Ours: `personal_budgets` lines {category, period YYYY-MM, amount_cents,
  rollover}. Effective budget for month M = amount set for M + (if rollover)
  the leftover carried from M−1 (recursively, capped at the earliest line).
  Spent = money-out in that category in M from `bank_transactions`.
  Variance and remaining returned per category; categories with spend but no
  budget line still appear (budgeted = 0) — same "nothing hides" rule as the
  business `monthly_budgets`.

### F5. Recurring-bill tracking — [seen]
A dedicated area listing recurring bills/subscriptions with amounts and due
dates.
- Ours (slice): manual registry — {name, category, amount_cents, cadence
  (weekly/biweekly/monthly/quarterly/yearly), next_due_date, active} — list
  endpoint flags due-within-7-days and overdue.
- **[gap]** Auto-detection of recurring charges from transaction history is a
  later slice (Sure's docs imply detection + a recurring area; detection
  algorithm is not publicly specified).

### F6. Savings goals — [seen] (goals/planning area)
Named goals with a target; progress visible.
- Ours: {name, target_cents, saved_cents, target_date?}; progress % computed;
  saved amount is user-maintained in this slice.
- **[gap]** Auto-funding goals from a linked account balance is a later slice.

### F7. Transfer pairing — [seen]
Moving your own money between your own accounts is not income or spending;
the two legs pair off and drop out of cash-flow/spending math.
- Ours: a transaction whose counterparty account is another account in the
  same household tenant is excluded from F3/F4 math. Slice implementation:
  transactions posted via the existing banking `POST /banking/transfer`
  already post a journal entry with source="transfer" and never enter
  category math; for staged `bank_transactions`, exclusion = rows flagged
  `is_transfer` (set by the pairing helper when an equal-and-opposite row
  exists in another household account within 3 days). Pairing helper is
  **[inferred]** mechanics — the *behavior* (transfers don't count) is [seen].

### F8. Rule-based categorization for personal transactions — [seen]
Transactions auto-categorize by user rules (plus AI in Sure; AI is out of
scope here).
- Ours: REUSE the existing banking machinery as-is — `bank_rules`
  (contains-match) + `assess_categorization`/`_apply_rules` in
  `routers/banking.py` already run over the same `bank_transactions`
  collection the personal layer reads. No new categorization code in this
  slice; the personal budgets read the category the existing pipeline set.

### F9. CSV import as the no-credential door — [seen]
Bank sync can be skipped entirely: import a CSV, no credentials anywhere.
- Ours: REUSE `POST /banking/import` (`routers/banking.py`) unchanged — it
  already writes into `bank_transactions` with dedupe hashing, which is
  exactly the stream F2/F3/F4 read. Plaid (`routers/bankfeed.py`, sandbox)
  feeds the same collection when configured. No new import code.

### F10. Multi-member household — [seen] in Sure; **[gap]** for us this slice
Sure lets family members each connect accounts under one household.
- Ours: a household is one tenant; memberships already support many users
  per tenant, but member-level account ownership is not modeled this slice.

### Out of scope / not built (named gaps)
- AI assistant over personal data (Sure: optional, own key) — not built.
- Investment/crypto price tracking, property valuation feeds — manual values only.
- Plaid production anything — sandbox seams only; production keys are the owner's.
- Frontend UI — this slice is API + tests; the web surface is the next slice.

---

## 2. Feature map — Sure feature → what the app already has → gap

| # | Feature | Already in app @ 5ea8024 | This slice builds | Still gap after slice |
|---|---------|--------------------------|-------------------|-----------------------|
| F1 | Net-worth dashboard | GL ledger balances (`lib/ledger._balances`), entity/tenant model, base COA asset/liability codes | `/personal/net-worth` endpoint + manual assets/debts registry | Investment/property auto-valuation |
| F2 | Day-by-day balances | `bank_transactions` stream (Plaid + CSV + manual), bank account registry | `/personal/balances/history` replay + per-account opening-balance meta | History before first imported txn is only as good as the opening balance the user sets |
| F3 | Cash-flow view | Same stream; business cash-flow forecast exists (`routers/budget.py`) but is business-shaped | `/personal/cash-flow` (in/out/net per period, transfers excluded) | — |
| F4 | Budgets + rollover | Business monthly category budgets (`monthly_budgets`, no rollover, business categories) | `personal_budgets` collection + CRUD + rollover math + variance vs stream | Quarterly personal periods (monthly only this slice) |
| F5 | Recurring bills | Business AP bills + forecast recurring items (business-shaped) | `personal_recurring_bills` registry + due flags | Auto-detection from history |
| F6 | Savings goals | Nothing | `personal_goals` CRUD + progress | Auto-funding from account balances |
| F7 | Transfer pairing | `POST /banking/transfer` posts balanced entries; staged-txn pairing absent | Exclusion rule in personal math + pairing flag helper | Perfect pairing (partial matches, fees) |
| F8 | Rule categorization | `bank_rules` + confidence engine (`lib/catconfidence.py`), review queue | Nothing — reused | AI categorization (not planned) |
| F9 | CSV import (no credentials) | `POST /banking/import` with dedupe + profiles | Nothing — reused | — |
| — | Household entity type | Tenants are businesses (`niche_id`, trade packs); `memberships` on user doc; `/entities` owner list | `tenant.kind = "household"` + `POST /personal/households` creation path (tenant + base COA seed + owner membership) | Household plan/entitlement shaping (rides `budget:*` perms this slice) |
| — | Isolation | `Scoped` tenant filtering on every collection | Personal endpoints hard-gated: 403 unless the active tenant is `kind="household"`; all personal collections Scoped | — |

## 3. Slice 1 definition (what gets built now)

Backend only, in the app's existing stack/patterns (FastAPI router + `Scoped`
+ integer cents + pydantic bodies + fake-collection pytest style):

1. `backend/routers/personal.py` — prefix `/personal`:
   - `POST /personal/households` — create a household tenant for the current
     user (kind="household", base COA seed, owner membership appended).
     Existing tenants default to kind="business" (absent field = business).
   - Gate helper: every other endpoint 403s unless active tenant is household.
   - `GET /personal/net-worth` — F1.
   - `GET /personal/balances/history` — F2 (per-account daily series + total).
   - `GET /personal/cash-flow?period=YYYY-MM` — F3.
   - Budgets: `GET/POST /personal/budgets`, `DELETE /personal/budgets/{id}` — F4
     with rollover carry math.
   - Recurring bills: `GET/POST /personal/recurring-bills`,
     `DELETE /personal/recurring-bills/{id}` — F5.
   - Goals: `GET/POST /personal/goals`, `DELETE /personal/goals/{id}` — F6.
   - Manual assets/debts: `GET/POST /personal/assets`,
     `DELETE /personal/assets/{id}` — feeds F1.
   - Account meta: `POST /personal/accounts/{bank_account_id}/opening` —
     sets opening balance/date used by F2.
   - Permissions: reuse `budget:read` / `budget:write` (already in
     `lib/auth.py` PERMISSIONS on main).
2. `server.py` — register the router (import + include, following the
   existing lists).
3. `backend/tests/test_personal_finance.py` — repo test style (fake
   collections, direct endpoint calls): household gate (business tenant →
   403; other tenant's data invisible), net-worth math, balance-history
   replay, cash-flow transfer exclusion, budget rollover carry (incl.
   overspend carrying negative), recurring due flags, goal progress.
4. This spec lands in the repo as `docs/PERSONAL_FINANCE_SPEC.md`.

## 4. Next slice (after this one)
Frontend household view (net-worth card, balance chart, budget bars) +
recurring-bill auto-detection from transaction history + goal auto-funding
from a chosen account's balance + household entitlement in `lib/plans.py`.
