# Personal Finance Layer — Slice 3: Budget-App Fold-In

**Job:** owner ruling, voice call 2026-11-18 ~14:38 CST — the standalone
budget app STAYS a standalone product, and its strengths are folded into
this household (personal-finance) layer, which was missing exactly those
pieces next to the reference apps.

**Source of behavior:** our own budget app (`~/workspace/buildout/budget-app`,
BRIEF.md + README.md + `backend/services.py`, read firsthand by the fold-in
crew). The behaviors below are the budget app's proven shapes, re-implemented
here from scratch in this codebase's patterns — `Scoped` tenant isolation,
integer cents, the `bank_transactions` stream slices 1–2 already read, the
household gate (`_require_household`) on every endpoint. No business
collection is read or written by any of it; the two sides never mix.

## What was folded in

### 1. Envelope budgeting (budget app F1/F7/F8/F15) — `personal_envelopes`
An envelope is a persistent pot for one spending category — not a monthly
budget line. It carries:
- a per-cycle allocation and **its own cycle start day** (1–28), so an
  envelope's month can start on payday instead of the 1st;
- a **health state**: green while >20% of the effective allocation
  remains, yellow at ≤20%, red when empty or overspent;
- **rollover**: a finished cycle's leftover (overspend included) carries
  into the next cycle's effective allocation;
- members + visibility (shared/private) for family envelopes.

Spending is measured from the household's categorized bank transactions
in the envelope's category during the current cycle — nothing is entered
twice. Endpoints: `POST/GET /personal/envelopes`,
`PUT/DELETE /personal/envelopes/{id}`.

**Payday pulse (F6):** `GET /personal/envelopes/suggestions?paycheck_cents=`
suggests allocations from the last three months of spending (each
category's share of the total applied to the paycheck). Suggestions
only — nothing is created until the customer saves an envelope.

### 2. Family safe-to-spend view (budget app F9 + Wave 10) — `GET /personal/safe-to-spend`
One number the whole family can trust, with every part shown:

    safe = account balances
           − bills due before the next payday (each occurrence counts;
             an overdue bill counts once, immediately)
           − envelope obligations (unspent envelope money)
           − vault locks (vault balances + locked goals' savings)

The next payday derives from the household's payday rhythm
(`last_payday_date` + `payday_cadence` — weekly / biweekly /
semimonthly (15th + month-end) / monthly — stored via `PUT /personal/settings`).
Without a rhythm, the horizon is today + 14 days and the payload says
`payday_estimated: true`. The raw figure can go negative (shown as
`raw_cents`); the family number floors at 0. The payload also names
the envelope with the most room left, mirroring the budget app's
spoken answer ("…and it comes from your Fun envelope").

### 3. Savings methods library (budget app F14 + Wave 2) — `personal_savings_methods` / `personal_savings_events`
Seven methods, each a real program with enrollment, actions, a running
total, and a yearly pace labeled **EXACT / ESTIMATE / PROJECTION with
its basis printed**:
- **Day-of-Month Stack** ($1 on day 1 … $31 on day 31 = $496/mo, $5,952/yr)
- **52-Week Challenge** (forward and reverse variants, $1,378)
- **100-Envelope Challenge** ($5,050)
- **Round-Ups** (sweep spare change from actual purchases; the sweep is
  idempotent — a purchase is never swept twice)
- **The $5 Rule**
- **No-Spend Days** (each kept day sweeps the household's average daily
  discretionary spend, computed from its own history)
- **Payday Escalator** (percent of each payday; every rise is a
  suggestion the user approves or declines — never automatic)

Endpoints: `GET /personal/savings-methods`,
`POST /personal/savings-methods/{key}/enroll`,
`DELETE /personal/savings-methods/{key}` (unenroll — history is kept),
`POST /personal/savings-methods/{key}/actions`.
Tracked savings are a rhythm, not a transfer: method money stays in the
household's accounts, exactly like goal `saved_cents` in slice 1.

### 4. Timed savings vaults (budget app P11) — `personal_vaults`
Money set aside on purpose behind a **withdrawal timer**: requesting a
withdrawal starts the countdown; the money cannot leave until it
elapses. Vault balances count as locks in safe-to-spend, as do the
savings of goals flagged `locked` (new optional field on goals).
Endpoints: `POST/GET /personal/vaults`, `POST /personal/vaults/{id}/deposit`,
`POST /personal/vaults/{id}/request-withdrawal`,
`POST /personal/vaults/{id}/withdraw`, `DELETE /personal/vaults/{id}`.

### 5. Bills + envelopes forecast (budget app Wave 6) — `GET /personal/forecast`
Per coming month: every bill occurrence walked forward from the
recurring-bill registry, plus each envelope's projected health
(projected spend = the category's average over the last three months).
The whole payload is labeled PROJECTION with its basis.

### 6. Age of money (budget app F13) — `GET /personal/age-of-money`
FIFO over the transaction stream: how many days ago the household
earned the oldest dollar it has not spent yet. Transfers between the
household's own accounts are neither income nor spending.

## Frontend
`frontend/src/pages/Household.jsx` gains: a safe-to-spend card (number,
parts breakdown, age of money, fullest envelope), an envelopes card
(state chips, cycle dates, rollover carry, create form, payday
suggestion splits with one-click apply), a savings-methods card
(enroll, progress, labeled yearly pace, next-action button), and a
vaults card (balances, timers, deposit / start-withdrawal / withdraw).
The fold-in surfaces load from their own endpoints — the slice-2
`/personal/summary` contract is untouched.

## Deliberately NOT folded in (and why)
- **Bill the wallet mascot, chatbot, voice Q&A** — the budget app's
  character surface; the bookkeeping app has its own assistant shape.
  The safe-to-spend *answer* (number + envelope it comes from) is in;
  the mascot is not.
- **Kid accounts / kid vaults / kids' game (F16/F17)** — a family-mode
  build of its own (sub-accounts, parent approvals). Envelopes already
  carry members here; kid sub-envelopes are a later slice, not a fold-in.
- **Money Circles, Empire Credit Tank, financial-literacy university,
  geofence alerts, receipt OCR, Bill Reads the Bill** — engagement /
  content / platform features of the standalone app, not money-math
  strengths the personal layer was missing. Bookkeeping also already
  has its own receipt pipeline and bill machinery (registry, detection,
  overdue flags from slices 1–2).
- **Daily check-in + streaks (F4/F5), Win Log (F12)** — habit mechanics;
  the insights surface (slice 2) already covers the reflective side.
- **Irregular-income budgeting mode (F21)** — needs an income-settings
  design pass of its own; payday suggestions currently take the
  paycheck amount per request.
- **Payday auto-transfer (P4/P12)** — the budget app itself ships these
  as suggestions behind a bank-sync seam; goal self-funding (slice 2)
  is this layer's real-money version of the same idea.
