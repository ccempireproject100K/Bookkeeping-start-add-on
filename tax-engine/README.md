# @bookkeeping/tax-engine

A self-contained TypeScript package that estimates a freelancer's tax for
**tax year 2026**: federal self-employment + income tax, safe-harbor
estimated payments, quarterly schedules with real due dates, and state
modules behind a jurisdiction registry. Backend only — no UI.

Strict TypeScript, zero runtime dependencies, integer-cents money handling,
and an auditable **workpapers** trace on every result: any figure can be
reproduced and explained line by line.

## Quick start

```bash
npm install
npm run typecheck        # tsc --noEmit (strict)
npm test                 # vitest
npm run test:coverage    # coverage on the calculation path
npm run build            # emit dist/
```

```ts
import { calculateTax, validateJurisdictions } from '@bookkeeping/tax-engine';

const result = calculateTax(80_000, 'US-CA', { filingStatus: 'single' });
result.totalLiabilityCents;   // 1941843  ($19,418.43, federal + CA)
result.breakdown;             // per-type components, each jurisdiction labeled
result.schedule;              // quarterly payments, merged by due date
result.safeHarbor;            // federal + state comparisons + required annual payment
result.workpapers;            // full trace + config version + SHA-256 config hash per jurisdiction

validateJurisdictions();      // data-freshness (90-day) + schema health per jurisdiction
```

## The core interface

```ts
calculateTax(netProfit: number, jurisdiction: string, taxProfile: TaxProfile): TaxResult
```

- `netProfit` — Schedule C net profit in dollars (may be ≤ 0).
- `jurisdiction` — a registry key: `US`, `US-CA`, `US-NY`, `US-NY-NYC`, …
  (`listJurisdictions()` returns all of them). A state key returns
  **federal + state combined**, each broken out in `breakdown`,
  `safeHarbor.federal` / `safeHarbor.state`, and `workpapers`.
- `taxProfile` — `filingStatus` (`single`, `married_filing_jointly`,
  `married_filing_separately`, `head_of_household`), `w2Wages`,
  `w2SocialSecurityWages`, `netCapitalGains`, `priorYearTax`,
  `priorYearAGI`, `priorYearFiledFullYear`, `paymentsAlreadyMade`,
  `isSSTB`, and state-specific extras namespaced under
  `state: { CA: { ... } }`.

`TaxResult` carries: `totalLiabilityCents`, `breakdown` (by tax type),
`schedule` (quarterly, exact-sum), `dueDates`, `safeHarbor`
(90%-of-current vs prior-year comparison), `status`, `notes`, and
`workpapers`.

### Status discipline

`status` is `OK`, `PARTIAL`, or `NOT_IMPLEMENTED` — and a state module is
either a real computation, a **true zero** (no-tax states, with the legal
reason in the notes), or an explicit **stub** (`NOT_IMPLEMENTED`, excluded
from the total, result marked `PARTIAL`). A stub never masquerades as a $0
liability, and an unregistered jurisdiction key throws
`UnknownJurisdictionError`. Zero is a real answer; silence is a bug.

## Federal module (2026)

- **SE tax:** 15.3% split as Social Security 12.4% + Medicare 2.9% on
  92.35% of net profit. SS is capped at the **$184,500** wage base,
  **reduced by W-2 Social Security wages** from the profile;
  **Additional Medicare Tax** 0.9% applies above the filing-status
  threshold ($200k single / $250k MFJ / $125k MFS) on SE earnings + W-2
  wages.
- **Half-SE-tax deduction** reduces AGI.
- **Standard deduction** per filing status ($16,100 single, $32,200 MFJ,
  $16,100 MFS, $24,150 HoH).
- **QBI (§199A)** in the IRS's order: computed *after* the standard
  deduction as the **lesser of** 20% of qualified business income or 20% of
  (taxable income before QBI − net capital gains), with the SSTB
  applicable-percentage phase-in across the OBBBA-widened ranges and the
  2026 §199A(i) **$400 minimum deduction**. See the golden example below —
  the ordering matters by ~$2,200 on an $80k income.
- **Safe harbor:** required annual payment = min(90% of current-year tax,
  100% of prior-year tax), with the **110% override when prior-year AGI >
  $150,000** (federal rule is strictly greater; CA's is ≥ — both are
  config). The prior-year basis is unavailable for short-year returns or a
  $0 prior-year tax.
- **Due dates:** Apr 15 / Jun 15 / Sep 15 / Jan 15(+1yr), rolled forward
  past weekends **and federal holidays** from a real holiday table —
  including DC Emancipation Day, which moves April deadlines (2028's Q1
  estimate is due **Tue Apr 18, 2028**), and MLK Day, which moves January
  ones (Q4 2027 estimates: **Tue Jan 18, 2028**).

### The golden example — and the QBI question for the owner

$80,000 net profit, single, no W-2 wages:

| Figure | Amount |
|---|---|
| SE tax (SS $9,161.12 + Medicare $2,142.52) | **$11,303.64** |
| Income tax, **correct §199A ordering** (QBI deduction $11,649.64 — the lesser-of cap binds) | **$5,343.82** |
| Income tax on the same income **without any QBI benefit** | $7,526.60 |
| Total liability (correct) | **$16,647.46** |
| Safe-harbor required (90% of current, no prior-year data) | $14,982.71 |
| Quarterly payments | $3,745.68 × 3 + $3,745.67 |

The owner's spec expected income tax "around $7,500" and quarterly
"≈ $4,700". Those figures match the **no-QBI** computation ($7,526.60).
With the QBI deduction applied the way the IRS orders it, the correct
income tax is **$5,343.82** and the total/quarterly follow from it. The
engine implements the correct ordering; `tests/golden.test.ts` asserts the
correct value and preserves the $7,526.60 comparison with a full
explanation, for the owner's ruling.

## State registry

Each state is a separate file in `src/states/` exporting the same
`{ config, calculate }` interface; `src/registry.ts` maps keys to modules.
Adding a state never touches the core.

**Top-10 freelancer states** (the design target — all ten have modules):

| State | Status |
|---|---|
| CA | **Implemented** (progressive brackets + 1% MHSA surtax over $1M; installments 30/40/0/30) |
| NY | **Implemented** (progressive brackets; NYC reserved as a locality segment — see below) |
| TX, FL, WA | **No-tax** — true zero with explanatory note |
| PA, IL, NC, GA, AZ | **Stubs** — explicit `NOT_IMPLEMENTED` |

Also included because a true zero costs one config line: NV, SD, WY, AK
(no income tax), TN (Hall tax repealed 2021), NH (interest & dividends tax
repealed 2025).

**The cut line:** every other income-tax state (NJ, OH, MA, VA, CO, …) has
**no module and its key throws**. Implementing a state requires a verified
rate table; shipping an unverified one — or worse, a silent zero — is the
liability this design exists to avoid. The flat-tax stubs (PA 3.07%,
IL 4.95%, NC, GA, AZ) are each a data-only addition away once their 2026
figures are verified.

**Localities:** keys compose as `US-NY-NYC`. The result computes NY State
in full and reports `localityStatus: 'NOT_IMPLEMENTED'` with the NYC rate
range (3.078%–3.876%) noted as excluded — never silently folded in, never
silently dropped.

## Config-driven rates

Every bracket, threshold, cap, deduction, and percentage lives in
`src/config/` as a versioned data file per jurisdiction per year
(`federal-2026.ts`, `california-2026.ts`, …), carrying `taxYear`,
`version`, a `source` note, a `lastVerified` ISO date, and a `provisional`
flag. Adding next year's numbers is a data change only. Every workpaper
records the config **version and SHA-256 hash** actually used.

**Verification status of the 2026 figures:**

- **Federal — verified** 2026-10-20 against IRS Rev. Proc. 2025-32
  (IR-2025-103) and the SSA wage-base announcement. `provisional: false`.
- **CA and NY — PROVISIONAL.** Their 2026 tables are the cross-checked
  choice among conflicting secondary summaries (sources named in each
  config file). Results and validation both surface the flag. Re-verify
  against the official FTB 2026 rate schedules and the NY DTF tables
  before production reliance.

## Validation layer

`validateJurisdictions({ asOf?, configs?, maxAgeDays? })` returns a
per-jurisdiction health list:

- **Freshness:** flags any config whose `lastVerified` is older than
  90 days as `stale`.
- **Schema:** bracket ceilings strictly ascending with an open-ended top,
  rates within (0, 1) and non-decreasing, caps positive, thresholds
  ordered, installment percentages summing to 1, deductions ≥ 0, stubs
  carrying no rate table, no-tax modules carrying their note. Violations
  are `invalid` with itemized issues.

## Money and rounding rules

- Integer **cents** everywhere inside the engine; floats exist only at the
  input boundary and are converted once (`toCents`).
- Rates are applied with exact BigInt rational arithmetic — never float
  multiplication.
- Rounding is **half-up**, applied at the points a tax form produces a
  number: each component, each bracket line, each deduction.
- Quarterly amounts are rounded per quarter and the **last
  non-zero-percentage quarter absorbs the remainder**, so the four
  quarters sum **exactly** to the annual required payment — including
  uneven patterns like CA's 30/40/0/30 (Q3 is exactly $0.00).

## Assumptions and simplifications (read before relying on output)

1. **States start from federal AGI.** State-specific additions and
   subtractions are not modeled.
2. The deductible half of SE tax is computed on the Schedule SE tax
   (Social Security + regular Medicare) and **excludes the Additional
   Medicare Tax**, which has no employer-equivalent share.
3. If `w2SocialSecurityWages` is omitted, it is assumed to be
   min(`w2Wages`, wage base) — and the workpapers say so.
4. QBI base = net profit − deductible half of SE tax
   (Reg. §1.199A-3(b)); a single business is assumed.
5. The §199A(i) $400 floor is applied **after** the lesser-of cap and
   clamped at taxable income before QBI; it does not apply to a fully
   phased-out SSTB.
6. The §199A **W-2 wage / UBIA limitation is not modeled**; above the
   threshold, non-SSTB results carry a workpaper warning instead.
7. **Preferential capital-gains rates are not modeled** — all income is
   taxed at ordinary rates; capital gains only reduce the QBI cap base.
8. CA simplifications: MFJ thresholds = 2× single; HoH uses the joint
   table. NY simplifications: HoH uses the single table; the tax-benefit
   recapture above $107,650 of NY AGI is not modeled; MCTMT is not
   modeled.
9. State due dates reuse the federal statutory dates and federal holiday
   rollover; state holiday calendars are not modeled.
10. `paymentsAlreadyMade` nets against the **federal** requirement. State
    payments are separate: `profile.state[CODE].paymentsAlreadyMade`
    (default 0), so a combined schedule never double-counts the same
    dollars. State prior-year safe-harbor figures likewise accept
    namespaced overrides and otherwise fall back to the top-level
    prior-year values.
11. `netProfit ≤ 0`: SE tax is $0; the loss still reduces AGI (it can
    offset W-2 wages). A §199A loss carryforward is not modeled. No result
    is ever negative or NaN.
12. Credits, AMT, NIIT, estimated-tax penalties, and the Form 8959
    withholding reconciliation are out of scope.

## Layout

```
src/
  index.ts            public barrel
  registry.ts         jurisdiction keys, calculateTax composition
  federal.ts          federal computation (SE, AGI, QBI, brackets)
  money.ts            integer-cents primitives, exact rate application
  brackets.ts         progressive bracket engine (per-line rounding)
  safeharbor.ts       safe-harbor comparison
  schedule.ts         exact-sum quarterly schedules + merging
  holidays.ts         federal holiday table + due-date rollover
  validation.ts       validateJurisdictions()
  hash.ts             stable config hashing (SHA-256)
  types.ts            public types (all money fields are *Cents)
  config/             versioned rate tables per jurisdiction-year
  states/             one module file per state (+ engine.ts factories)
tests/                golden, federal, safe harbor, holidays, states,
                      properties (exact-sum, monotonicity), validation,
                      money, hash — 140 tests
```
