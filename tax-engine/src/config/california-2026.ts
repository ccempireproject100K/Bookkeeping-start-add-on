import type { StateConfig } from './types.js';

/**
 * California individual income tax — tax year 2026. PROVISIONAL.
 *
 * The bracket thresholds and standard deduction below follow the FTB 2026
 * figures as summarized by two cross-checked secondary sources
 * (onlinepaystub.com and countrytaxcalc.com, retrieved 2026-10-20) that
 * agree with each other, including the $5,900 single standard deduction.
 * Other secondary sources circulate at least two different "2026" tables
 * (which appear to be the 2024 and 2025 tables mislabeled), so this table
 * MUST be re-verified against the official FTB 2026 California Tax Rate
 * Schedules before production use. Structural facts that are not in
 * dispute: nine brackets 1%–12.3%, the 1% Mental Health Services Tax on
 * taxable income over $1M, no conformity to §199A QBI, estimated-tax
 * installments of 30/40/0/30 percent, and the 110% safe-harbor override at
 * prior-year AGI of $150,000 or more (no safe harbor at AGI ≥ $1M — noted
 * in results, not enforced).
 *
 * Simplification (documented): head-of-household uses the joint table.
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
};

export const CALIFORNIA_2026: StateConfig = {
  jurisdiction: 'US-CA',
  kind: 'state',
  stateCode: 'CA',
  implementation: 'full',
  taxYear: 2026,
  version: '2026.1.0',
  source:
    'PROVISIONAL — FTB 2026 rate schedules via cross-checked secondary summaries (onlinepaystub.com, countrytaxcalc.com, retrieved 2026-10-20); secondary sources conflict, re-verify against official FTB 2026 California Tax Rate Schedules before production use.',
  lastVerified: '2026-10-20',
  provisional: true,
  standardDeduction: {
    single: 5900,
    married_filing_jointly: 11800,
    married_filing_separately: 5900,
    head_of_household: 11800,
  },
  brackets: SINGLE,
  surtax: { label: 'Mental Health Services Tax', threshold: 1000000, rate: 0.01 },
  safeHarbor: {
    currentYearPct: 0.9,
    priorYearPct: 1.0,
    priorYearHighIncomePct: 1.1,
    highIncomeAgiThreshold: 150000,
    highIncomeComparison: 'gte',
  },
  installmentPcts: [0.3, 0.4, 0, 0.3],
};
