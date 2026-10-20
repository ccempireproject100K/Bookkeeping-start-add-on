# Payroll Tracking — "everything but the money"

The payroll module calculates real gross-to-net pay, tracks every tax
obligation with its due date, and prepares filing workpapers. By owner
ruling it **never moves money**: it does not pay employees, does not deposit
taxes with any agency, and does not file returns. The user (or a future
payroll partner) remits — this module makes sure the amounts and dates are
right.

## What it computes (2026 tables, verified 2026-10-20)

All figures live in `backend/lib/payroll_config_2026.py` — versioned config
with sources and a `LAST_VERIFIED` date, same discipline as the tax engine.
The calculation engine is `backend/lib/payroll_calc.py` (pure functions,
integer cents, every result carries a workpaper).

- **Federal withholding** — IRS Pub 15-T (2026) Percentage Method for
  automated systems, Worksheet 1A: W-4 filing status, Step 2 multiple-jobs
  checkbox schedules, Step 3 dependents, Step 4(a)/(b)/(c) adjustments.
- **FICA** — Social Security 6.2% with the $184,500 wage base **actually
  applied per employee against year-to-date FICA wages**; Medicare 1.45%;
  Additional Medicare 0.9% withheld on wages above $200,000 YTD.
- **Employer taxes** — FICA match, FUTA 6.0% on the first $7,000 with the
  5.4% state credit accrued (net 0.6%), SUTA per state (CA 3.4% / $7,000,
  NY 4.100% / $17,600 new-employer defaults; tenant overrides via payroll
  settings), CA Employment Training Tax 0.1% / $7,000.
- **State withholding** — CA (EDD Method B exact calculation, including the
  two DE 4 allowance types, the low-income exemption, and the fact that CA
  does **not** exclude 401(k) deferrals from PIT wages) and NY
  (NYS-50-T-NYS Method II, with Method III at top incomes; Head of
  Household withholds on the Single schedule). Every other state returns an
  **explicit NOT_IMPLEMENTED status on the run** — never a silent zero.
  No-income-tax states (TX, FL, …) are a real zero, labeled as such.
- **Overtime** — 1.5× over 40 hours/week (FLSA workweek), split exactly
  from approved time entries or per-week hours; an even-spread estimate is
  labeled as such on the item.
- **Deductions** — pre-tax retirement (reduces income-tax wages, not FICA
  wages), Section 125 / cafeteria HSA (reduces both), post-tax, booth rent,
  and garnishments. Garnishments follow CCPA caps: child support first at
  its percentage of disposable earnings (default 50%); ordinary
  garnishments limited to 25% of disposable earnings and to what remains
  under the support cap. Capped amounts are shown on the stub as
  requested-vs-applied. **Disbursement of withheld deductions and
  garnishments is out of scope** — amounts are tracked as held liabilities
  (account 2120) for the user to remit.

## Runs, records, liabilities

- Pay runs keep the draft → approve → post flow. Posting writes the journal
  entry (Dr 6110 gross, Dr 6120 employer taxes, Cr 2120 withholdings and
  held deductions, Cr 2320 net Wages Payable), **immutable per-employee pay
  records** (`payroll_pay_records`), and **dated liability entries**
  (`payroll_liabilities`).
- Federal deposit due dates follow the IRS deposit schedule: lookback over
  the four quarters ending June 30 of the prior year (≤ $50,000 → monthly,
  otherwise semiweekly), the $100,000 next-business-day rule, and
  weekend roll-forward. FUTA, state withholding, SUTA and ETT get their own
  dated entries. Liabilities are tracked as `accrued` until the user marks
  them `remitted` with a reference — the app records the payment, it never
  makes it.
- A posted run can be **reversed** (offsetting journal entry, liabilities
  and pay records flagged reversed, nothing edited or deleted).
- Workpaper endpoints accumulate finalized pay records into the data a
  return needs — `GET /payroll/reports/form-941?year&quarter`,
  `form-940?year`, `w2-workpaper?year` (W-2 Boxes 1–7 plus per-state
  wages/withholding) — each labeled "Workpaper only — does not file".

## Where a money-movement executor attaches

The finalized outputs are the contract; no calculation code changes when a
partner (Gusto Embedded, Check, Zeal, …) is connected:

1. **Pay employees** — consume posted runs' `payroll_pay_records`
   (`net_cents` per employee, `pay_date`, immutable snapshot in `item`).
2. **Deposit taxes** — consume `payroll_liabilities` rows in `accrued`
   status, ordered by `due_date`, per agency/kind; write back `remitted`
   with the agency reference via `PATCH /payroll/liabilities/{id}`.
3. **File** — consume the 941/940/W-2 workpaper accumulations (out of scope
   for this product by owner ruling; the data contract exists regardless).

Known limitation: YTD wage-base caps are computed from finalized runs at
draft time, so runs should be created and posted in pay-date order;
drafting two runs before posting either one can double-count the first
$7,000 of FUTA/SUTA wage base. The posted record always shows the amounts
that were actually used.

## NOT_IMPLEMENTED (explicit, never faked)

- State withholding for every state except CA and NY.
- SUTA defaults for states other than CA/NY (tenant rate override exists,
  but a wage base is also required, so an override alone stays
  NOT_IMPLEMENTED until that state's base is configured in code).
- NYC / Yonkers local withholding (noted on every NY workpaper).
- Direct deposit, tax deposits, and all filing — by design (see above).
- Pre-2020 W-4 allowance-based withholding (the engine implements the
  2020+ W-4; employees without a W-4 on file use the IRS-prescribed
  single / no-adjustments default, flagged on the item).
