import type { FilingStatus } from '../types.js';

/** One progressive bracket. `upTo` is the bracket ceiling in DOLLARS;
 *  null = open-ended top bracket. */
export interface Bracket {
  upTo: number | null;
  rate: number;
}

export type BracketsByStatus = Record<FilingStatus, Bracket[]>;

export interface SafeHarborParams {
  /** Fraction of current-year tax in the current-year safe harbor (0.90). */
  currentYearPct: number;
  /** Fraction of prior-year tax in the standard prior-year safe harbor. */
  priorYearPct: number;
  /** Fraction of prior-year tax when prior AGI clears the threshold (1.10). */
  priorYearHighIncomePct: number;
  /** AGI threshold (dollars) for the high-income override. */
  highIncomeAgiThreshold: number;
  /**
   * Federal rule (§6654): prior AGI must EXCEED $150,000 ('gt').
   * California: AGI of $150,000 OR MORE triggers 110% ('gte').
   */
  highIncomeComparison?: 'gt' | 'gte';
}

/** Metadata every versioned rate table carries. */
export interface ConfigMeta {
  jurisdiction: string;
  taxYear: number;
  /** Config version for this tax year, e.g. '2026.1.0'. */
  version: string;
  /** Where the figures came from (statute / revenue procedure / owner spec). */
  source: string;
  /** ISO date (yyyy-mm-dd) the figures were last verified against the source. */
  lastVerified: string;
  /** True when any figure could not be confirmed against a primary source
   *  and must not be presented as verified. */
  provisional: boolean;
}

export interface FederalConfig extends ConfigMeta {
  kind: 'federal';
  se: {
    /** Net-earnings factor: SE tax applies to 92.35% of net profit. */
    netEarningsFactor: number;
    socialSecurityRate: number;
    medicareRate: number;
    /** Social Security wage base (dollars). */
    socialSecurityWageBase: number;
    additionalMedicareRate: number;
    /** Additional Medicare Tax thresholds (dollars) by filing status. */
    additionalMedicareThreshold: Record<FilingStatus, number>;
  };
  standardDeduction: Record<FilingStatus, number>;
  brackets: BracketsByStatus;
  qbi: {
    rate: number;
    /** §199A threshold (dollars of taxable income BEFORE the QBI deduction). */
    threshold: Record<FilingStatus, number>;
    /** Width of the phase-in range above the threshold (dollars). */
    phaseInRange: Record<FilingStatus, number>;
    /** 2026 §199A(i) minimum deduction (dollars) when aggregate QBI ≥ minimumQbi. */
    minimumDeduction: number;
    minimumQbi: number;
  };
  safeHarbor: SafeHarborParams;
  /** Four installment percentages summing to 1. */
  installmentPcts: number[];
}

export type StateImplementation = 'full' | 'no-tax' | 'stub';

export interface StateConfig extends ConfigMeta {
  kind: 'state';
  /** Two-letter state code, e.g. 'CA'. */
  stateCode: string;
  implementation: StateImplementation;
  /** Present when implementation === 'full'. */
  standardDeduction?: Record<FilingStatus, number>;
  brackets?: BracketsByStatus;
  /** e.g. California's 1% Mental Health Services Tax over $1M. */
  surtax?: { label: string; threshold: number; rate: number };
  /**
   * Nonrefundable personal exemption credit (California). Applied in the
   * Form 540 order: the credit reduces the regular bracket tax (line 32)
   * BEFORE any surtax is added (the Mental/Behavioral Health Services Tax
   * is an "other tax" added later and is never offset by exemption
   * credits), and it can reduce the regular tax to $0 but not below.
   */
  exemptionCredit?: {
    /** Credit per personal exemption, in DOLLARS ($158 CA 2026). */
    perExemption: number;
    /** Personal exemptions for this credit, by filing status
     *  (married filing jointly = 2 — taxpayer + spouse; all others = 1),
     *  so the headline amounts are $158 single/MFS/HoH and $316 joint. */
    exemptions: Record<FilingStatus, number>;
    /** FTB high-income phaseout (Form 540 AGI Limitation Worksheet):
     *  for each whole `step` of federal AGI over `agiThreshold` (any
     *  fraction of a step rounds up to a full step), EACH exemption
     *  credit is reduced by `reductionPerStep`, never below $0. */
    phaseout: {
      agiThreshold: Record<FilingStatus, number>;
      /** Step size in DOLLARS of federal AGI ($2,500; $1,250 for MFS). */
      step: Record<FilingStatus, number>;
      /** Reduction per step PER EXEMPTION, in DOLLARS ($6 CA). */
      reductionPerStep: number;
    };
  };
  /**
   * Known omissions/simplifications for this state, disclosed in every
   * result's workpapers and notes — the same disclosure pattern as the
   * federal QBI W-2 wage / UBIA limitation warning.
   */
  knownOmissions?: { label: string; detail: string }[];
  safeHarbor?: SafeHarborParams;
  installmentPcts?: number[];
  /** Present when implementation === 'no-tax': why the liability is truly zero. */
  noTaxNote?: string;
  /** Present when implementation === 'stub': what is missing and why. */
  stubReason?: string;
}

export type JurisdictionConfig = FederalConfig | StateConfig;
