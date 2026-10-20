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
 * Known omission (documented, disclosed in every result via
 * `knownOmissions`): the personal exemption credit — $158 single / MFS /
 * HoH, $316 joint (2026, per FTB), plus the $491 dependent exemption
 * credit — is NOT modeled, and neither is the credit's high-income
 * phaseout. CA estimates overstate tax by up to the credit amount until
 * the credit is modeled (owner decision, 2026-10-20: document, don't
 * model yet).
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
  version: '2026.2.0',
  source:
    'FTB 2026 California Tax Rate Schedules (X, Y, Z) and standard deductions, verified directly against FTB Tax News, October 2026, "2026 Indexing" (ftb.ca.gov), checked 2026-10-20. Personal exemption credit not modeled — see knownOmissions.',
  lastVerified: '2026-10-20',
  provisional: false,
  standardDeduction: {
    single: 5900,
    married_filing_jointly: 11800,
    married_filing_separately: 5900,
    head_of_household: 11800,
  },
  brackets: SINGLE,
  surtax: { label: 'Mental Health Services Tax', threshold: 1000000, rate: 0.01 },
  knownOmissions: [
    {
      label: 'Personal exemption credit — NOT modeled',
      detail:
        "California's personal exemption credit is NOT modeled (documented simplification): $158 for single / married filing separately / head of household and $316 for married filing jointly (2026, per FTB). The dependent exemption credit ($491 per dependent) and the credit's high-income phaseout are also not modeled. CA estimates therefore overstate tax by up to the credit amount until the credit is modeled — review before relying on them.",
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
