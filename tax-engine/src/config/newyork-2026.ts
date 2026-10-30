import type { StateConfig } from './types.js';

/**
 * New York State individual income tax — tax year 2026. VERIFIED.
 *
 * Rates reflect the lower-bracket reductions enacted for tax years
 * beginning in 2026 (3.9 / 4.4 / 5.15 / 5.4 / 5.9% in the bottom five
 * brackets). Rates, bracket thresholds (single/MFS, joint, and head of
 * household), and standard deductions were verified 2026-10-20 against
 * the NYS DTF "2026 Estimated Tax Instructions" table as reproduced by
 * incometaxpro.com (updated 2026-10-19, citing tax.ny.gov) and
 * corroborated by multiple secondary sources — one notch below a direct
 * primary-source check, since the DTF PDF itself was not opened. Standard
 * deductions are statutory amounts (not inflation-indexed): $8,000 single /
 * $16,050 joint / $11,200 head of household. Head of household has its OWN
 * threshold table (corrected 2026-10-20 — an earlier revision of this
 * config reused the single table for HoH, overstating HoH tax: on the HoH
 * table the 5.9% band runs to $269,300, not $215,400).
 *
 * NOT modeled (documented): the tax-benefit recapture that phases in above
 * $107,650 of NY AGI, supplemental high-income tables, credits, and the
 * MCTMT. New York City resident tax is a LOCALITY add-on (see the
 * 'US-NY-NYC' key) and is not included in state figures.
 *
 * MFS uses the single table (correct under NY law).
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
  version: '2026.2.0',
  source:
    'NY Tax Law §601 as amended effective Jan 1 2026 (lower-bracket rate reductions). 2026 rates, thresholds, and standard deductions verified 2026-10-20 against the NYS DTF 2026 Estimated Tax Instructions table as reproduced by incometaxpro.com (updated 2026-10-19, citing tax.ny.gov), corroborated by multiple secondary sources.',
  lastVerified: '2026-10-20',
  provisional: false,
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
    head_of_household: [
      // HoH thresholds per Tax Law §601 structure + DTF 2026 instructions
      // table; same rate sequence as the other statuses.
      { upTo: 12800, rate: 0.039 },
      { upTo: 17650, rate: 0.044 },
      { upTo: 20900, rate: 0.0515 },
      { upTo: 107650, rate: 0.054 },
      { upTo: 269300, rate: 0.059 },
      { upTo: 1616450, rate: 0.0685 },
      { upTo: 5000000, rate: 0.0965 },
      { upTo: 25000000, rate: 0.103 },
      { upTo: null, rate: 0.109 },
    ],
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
