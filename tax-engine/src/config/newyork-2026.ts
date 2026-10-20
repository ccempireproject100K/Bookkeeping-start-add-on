import type { StateConfig } from './types.js';

/**
 * New York State individual income tax — tax year 2026. PROVISIONAL.
 *
 * Rates reflect the lower-bracket reductions enacted for tax years
 * beginning in 2026 (3.9 / 4.4 / 5.15 / 5.4 / 5.9% in the bottom five
 * brackets), cross-checked across secondary summaries including a fully
 * worked example (incometaxpro.com) and realtakehomepay.com, retrieved
 * 2026-10-20. Bracket thresholds are unchanged from prior law. Standard
 * deductions are statutory amounts (not inflation-indexed): $8,000 single /
 * $16,050 joint / $11,200 head of household.
 *
 * NOT modeled (documented): the tax-benefit recapture that phases in above
 * $107,650 of NY AGI, supplemental high-income tables, credits, and the
 * MCTMT. New York City resident tax is a LOCALITY add-on (see the
 * 'US-NY-NYC' key) and is not included in state figures.
 *
 * Simplifications: head-of-household uses the single table; MFS uses the
 * single table (correct under NY law).
 */
const SINGLE_TABLE = [
  { upTo: 8500, rate: 0.039 },
  { upTo: 11700, rate: 0.044 },
  { upTo: 13900, rate: 0.0515 },
  { upTo: 80650, rate: 0.054 },
  { upTo: 215400, rate: 0.059 },
  { upTo: 1077550, rate: 0.0685 },
  { upTo: 5000000, rate: 0.0965 },
  { upTo: 25000000, rate: 0.103 },
  { upTo: null, rate: 0.109 },
];

export const NEW_YORK_2026: StateConfig = {
  jurisdiction: 'US-NY',
  kind: 'state',
  stateCode: 'NY',
  implementation: 'full',
  taxYear: 2026,
  version: '2026.1.0',
  source:
    'PROVISIONAL — NY Tax Law §601 as amended effective Jan 1 2026 (lower-bracket rate reductions), via cross-checked secondary summaries retrieved 2026-10-20; verify against tax.ny.gov (Form IT-201 / Pub. NYS-50) before production use.',
  lastVerified: '2026-10-20',
  provisional: true,
  standardDeduction: {
    single: 8000,
    married_filing_jointly: 16050,
    married_filing_separately: 8000,
    head_of_household: 11200,
  },
  brackets: {
    single: SINGLE_TABLE,
    married_filing_jointly: [
      { upTo: 17150, rate: 0.039 },
      { upTo: 23600, rate: 0.044 },
      { upTo: 27900, rate: 0.0515 },
      { upTo: 161550, rate: 0.054 },
      { upTo: 323200, rate: 0.059 },
      { upTo: 2155350, rate: 0.0685 },
      { upTo: 5000000, rate: 0.0965 },
      { upTo: 25000000, rate: 0.103 },
      { upTo: null, rate: 0.109 },
    ],
    married_filing_separately: SINGLE_TABLE,
    head_of_household: SINGLE_TABLE,
  },
  safeHarbor: {
    currentYearPct: 0.9,
    priorYearPct: 1.0,
    priorYearHighIncomePct: 1.1,
    highIncomeAgiThreshold: 150000,
    highIncomeComparison: 'gt',
  },
  installmentPcts: [0.25, 0.25, 0.25, 0.25],
};
