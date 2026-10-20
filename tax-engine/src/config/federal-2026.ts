import type { FederalConfig } from './types.js';

/**
 * US federal individual + self-employment tax — tax year 2026.
 *
 * Figures verified 2026-10-20 against IRS Rev. Proc. 2025-32 (released with
 * IR-2025-103, Oct 9 2025) and the SSA 2026 wage-base announcement
 * (Oct 24 2025). The owner's spec constants — $16,100 single standard
 * deduction and the $184,500 Social Security wage base — match the
 * published figures exactly.
 */
export const FEDERAL_2026: FederalConfig = {
  jurisdiction: 'US',
  kind: 'federal',
  taxYear: 2026,
  version: '2026.1.0',
  source:
    'IRS Rev. Proc. 2025-32 (brackets, standard deduction, §199A thresholds incl. OBBBA-widened phase-in ranges and the §199A(i) $400 minimum); SSA 2026 wage base announcement ($184,500); SE rates and Additional Medicare thresholds are statutory (IRC §§1401, 3101(b)(2)) and not inflation-adjusted.',
  lastVerified: '2026-10-20',
  provisional: false,
  se: {
    netEarningsFactor: 0.9235,
    socialSecurityRate: 0.124,
    medicareRate: 0.029,
    socialSecurityWageBase: 184500,
    additionalMedicareRate: 0.009,
    additionalMedicareThreshold: {
      single: 200000,
      married_filing_jointly: 250000,
      married_filing_separately: 125000,
      head_of_household: 200000,
    },
  },
  standardDeduction: {
    single: 16100,
    married_filing_jointly: 32200,
    married_filing_separately: 16100,
    head_of_household: 24150,
  },
  brackets: {
    single: [
      { upTo: 12400, rate: 0.1 },
      { upTo: 50400, rate: 0.12 },
      { upTo: 105700, rate: 0.22 },
      { upTo: 201775, rate: 0.24 },
      { upTo: 256225, rate: 0.32 },
      { upTo: 640600, rate: 0.35 },
      { upTo: null, rate: 0.37 },
    ],
    married_filing_jointly: [
      { upTo: 24800, rate: 0.1 },
      { upTo: 100800, rate: 0.12 },
      { upTo: 211400, rate: 0.22 },
      { upTo: 403550, rate: 0.24 },
      { upTo: 512450, rate: 0.32 },
      { upTo: 768700, rate: 0.35 },
      { upTo: null, rate: 0.37 },
    ],
    married_filing_separately: [
      { upTo: 12400, rate: 0.1 },
      { upTo: 50400, rate: 0.12 },
      { upTo: 105700, rate: 0.22 },
      { upTo: 201775, rate: 0.24 },
      { upTo: 256225, rate: 0.32 },
      { upTo: 384350, rate: 0.35 },
      { upTo: null, rate: 0.37 },
    ],
    head_of_household: [
      { upTo: 17700, rate: 0.1 },
      { upTo: 67450, rate: 0.12 },
      { upTo: 105700, rate: 0.22 },
      { upTo: 201750, rate: 0.24 },
      { upTo: 256200, rate: 0.32 },
      { upTo: 640600, rate: 0.35 },
      { upTo: null, rate: 0.37 },
    ],
  },
  qbi: {
    rate: 0.2,
    threshold: {
      single: 201750,
      married_filing_jointly: 403500,
      married_filing_separately: 201775,
      head_of_household: 201750,
    },
    phaseInRange: {
      single: 75000,
      married_filing_jointly: 150000,
      married_filing_separately: 75000,
      head_of_household: 75000,
    },
    minimumDeduction: 400,
    minimumQbi: 1000,
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
