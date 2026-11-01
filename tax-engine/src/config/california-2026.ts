import type { StateConfig } from './types.js';

/**
 * California individual income tax — tax year 2026. VERIFIED.
 *
 * Bracket thresholds (Schedules X, Y, and Z) and standard deductions were
 * verified directly against the official FTB Tax News, October 2026,
 * "2026 Indexing" (ftb.ca.gov; inflation basis CCPI 3.4%), checked
 * 2026-10-20: Schedule X and Y thresholds and the $5,900 / $11,800
 * standard deductions match FTB exactly. Head of household uses FTB
 * Schedule Z (corrected 2026-10-20 — an earlier revision of this config
 * reused the wider joint table for HoH, materially understating HoH tax
 * at higher incomes: on Schedule Z the 9.3% band starts at $102,356,
 * not $150,394).
 * Structural facts: nine brackets 1%–12.3%, the 1% Mental Health Services
 * Tax on taxable income over $1M, no conformity to §199A QBI,
 * estimated-tax installments of 30/40/0/30 percent, and the 110%
 * safe-harbor override at prior-year AGI of $150,000 or more (no safe
 * harbor at AGI ≥ $1M — noted in results, not enforced).
 *
 * Personal exemption credit — MODELED (added 2026-11-01, gap sweep S3;
 * previously a documented omission under the owner's 2026-10-20
 * "document, don't model yet" call, superseded by the owner's
 * 2026-11-01 order to build anything left off): $158 per personal
 * exemption (single / MFS / HoH = 1 exemption = $158; joint = 2
 * exemptions = $316), nonrefundable against the bracket tax only —
 * never against the Mental Health Services Tax (Form 540 order:
 * exemption credits line 32, MHST added later as an other tax) — with
 * the FTB high-income phaseout: each exemption credit is reduced by $6
 * for each whole $2,500 of federal AGI over the filing-status threshold
 * ($1,250 steps for MFS), per the Form 540 AGI Limitation Worksheet.
 * Credit amounts re-verified 2026-11-01 against FTB Tax News, October
 * 2026; the 2026 phaseout thresholds ($260,778 single/MFS, $391,173 HoH,
 * $521,561 joint) are the FTB October 2026 indexing figures as published
 * in Spidell's California Taxletter, October 2026 (FTB's own 2026 Form
 * 540 instructions were not yet published at check time; the thresholds
 * are exactly the FTB 2025 thresholds × the 3.4% CCPI factor), and the
 * phaseout mechanics were verified against FTB's Form 540 instructions
 * AGI Limitation Worksheet.
 *
 * Remaining known omission (documented, disclosed in every result via
 * `knownOmissions`): the dependent exemption credit ($491 per
 * dependent) and the blind / age-65 exemption credits ($158 each) are
 * NOT modeled — the tax profile carries no dependents / blind / senior
 * inputs.
 */
const SINGLE: StateConfig['brackets'] = {
  single: [
    { upTo: 11456, rate: 0.01 },
    { upTo: 27157, rate: 0.02 },
    { upTo: 42861, rate: 0.04 },
    { upTo: 59498, rate: 0.06 },
    { upTo: 75197, rate: 0.08 },
    { upTo: 384109, rate: 0.093 },
    { upTo: 460927, rate: 0.103 },
    { upTo: 768213, rate: 0.113 },
    { upTo: null, rate: 0.123 },
  ],
  married_filing_jointly: [
    { upTo: 22912, rate: 0.01 },
    { upTo: 54314, rate: 0.02 },
    { upTo: 85722, rate: 0.04 },
    { upTo: 118996, rate: 0.06 },
    { upTo: 150394, rate: 0.08 },
    { upTo: 768218, rate: 0.093 },
    { upTo: 921854, rate: 0.103 },
    { upTo: 1536426, rate: 0.113 },
    { upTo: null, rate: 0.123 },
  ],
  married_filing_separately: [
    { upTo: 11456, rate: 0.01 },
    { upTo: 27157, rate: 0.02 },
    { upTo: 42861, rate: 0.04 },
    { upTo: 59498, rate: 0.06 },
    { upTo: 75197, rate: 0.08 },
    { upTo: 384109, rate: 0.093 },
    { upTo: 460927, rate: 0.103 },
    { upTo: 768213, rate: 0.113 },
    { upTo: null, rate: 0.123 },
  ],
  head_of_household: [
    // FTB Schedule Z (2026) — head of household has its own table.
    { upTo: 22927, rate: 0.01 },
    { upTo: 54316, rate: 0.02 },
    { upTo: 70018, rate: 0.04 },
    { upTo: 86654, rate: 0.06 },
    { upTo: 102356, rate: 0.08 },
    { upTo: 522385, rate: 0.093 },
    { upTo: 626864, rate: 0.103 },
    { upTo: 1044771, rate: 0.113 },
    { upTo: null, rate: 0.123 },
  ],
};

export const CALIFORNIA_2026: StateConfig = {
  jurisdiction: 'US-CA',
  kind: 'state',
  stateCode: 'CA',
  implementation: 'full',
  taxYear: 2026,
  version: '2026.3.0',
  source:
    'FTB 2026 California Tax Rate Schedules (X, Y, Z) and standard deductions, verified directly against FTB Tax News, October 2026, "2026 Indexing" (ftb.ca.gov), checked 2026-10-20. Personal exemption credit ($158 per exemption; $316 joint) modeled since config 2026.3.0 (2026-11-01): amounts per FTB Tax News, October 2026; 2026 phaseout thresholds per FTB October 2026 indexing figures as published in Spidell California Taxletter, October 2026; phaseout mechanics per FTB Form 540 instructions, AGI Limitation Worksheet. Dependent exemption credit not modeled — see knownOmissions.',
  lastVerified: '2026-11-01',
  provisional: false,
  standardDeduction: {
    single: 5900,
    married_filing_jointly: 11800,
    married_filing_separately: 5900,
    head_of_household: 11800,
  },
  brackets: SINGLE,
  surtax: { label: 'Mental Health Services Tax', threshold: 1000000, rate: 0.01 },
  exemptionCredit: {
    // FTB 2026: $158 per personal exemption. Joint filers claim two
    // personal exemptions (taxpayer + spouse) = $316.
    perExemption: 158,
    exemptions: {
      single: 1,
      married_filing_jointly: 2,
      married_filing_separately: 1,
      head_of_household: 1,
    },
    phaseout: {
      // FTB October 2026 indexing (see header note for sourcing).
      agiThreshold: {
        single: 260778,
        married_filing_jointly: 521561,
        married_filing_separately: 260778,
        head_of_household: 391173,
      },
      step: {
        single: 2500,
        married_filing_jointly: 2500,
        married_filing_separately: 1250,
        head_of_household: 2500,
      },
      reductionPerStep: 6,
    },
  },
  knownOmissions: [
    {
      label: 'Dependent exemption credit — NOT modeled',
      detail:
        "California's dependent exemption credit is NOT modeled (documented simplification): $491 per dependent (2026, per FTB), and neither are the blind / age-65 exemption credits ($158 each) — the tax profile carries no dependents, blindness, or age inputs. The PERSONAL exemption credit IS modeled, including its high-income phaseout (see the 'Personal exemption credit' workpaper step). CA estimates for filers with dependents therefore overstate tax by up to $491 per dependent — review before relying on them.",
    },
  ],
  safeHarbor: {
    currentYearPct: 0.9,
    priorYearPct: 1.0,
    priorYearHighIncomePct: 1.1,
    highIncomeAgiThreshold: 150000,
    highIncomeComparison: 'gte',
  },
  installmentPcts: [0.3, 0.4, 0, 0.3],
};
