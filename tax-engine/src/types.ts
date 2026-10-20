/**
 * Public types for the tax engine.
 *
 * All money fields are integer CENTS (names end in `Cents`). Dollar inputs on
 * TaxProfile are the only floats allowed, and only at the boundary.
 */

export type FilingStatus =
  | 'single'
  | 'married_filing_jointly'
  | 'married_filing_separately'
  | 'head_of_household';

export interface TaxProfile {
  filingStatus: FilingStatus;
  /** W-2 wages (Box 1). Counted in AGI and in the Additional Medicare base. */
  w2Wages?: number;
  /**
   * W-2 Social Security wages (Box 3). Reduces the remaining SS wage base
   * for SE tax. If omitted while w2Wages is present, the engine assumes
   * min(w2Wages, wage base) and says so in the workpapers.
   */
  w2SocialSecurityWages?: number;
  /** Net capital gains — only feeds the QBI overall cap. Preferential
   *  capital-gains rates are NOT modeled (see README assumptions). */
  netCapitalGains?: number;
  /** Total tax shown on last year's return (safe-harbor prior-year basis). */
  priorYearTax?: number;
  /** Last year's AGI — drives the 110% high-income safe-harbor override. */
  priorYearAGI?: number;
  /** False if last year's return covered fewer than 12 months; the
   *  prior-year safe harbor is then unavailable. Defaults to true. */
  priorYearFiledFullYear?: boolean;
  /** Withholding + estimated payments already made for the estimate year.
   *  The schedule nets this against the required annual payment and shows
   *  the REMAINING due per quarter. */
  paymentsAlreadyMade?: number;
  /** Specified Service Trade or Business flag for the §199A phase-out. */
  isSSTB?: boolean;
  /** State-specific inputs, namespaced by state code, e.g. { CA: {...} }. */
  state?: Record<string, Record<string, unknown>>;
}

export type ResultStatus = 'OK' | 'PARTIAL' | 'NOT_IMPLEMENTED';
export type ModuleStatus = 'OK' | 'NOT_IMPLEMENTED';

export interface TaxComponent {
  jurisdiction: string;
  /** Machine-readable tax type, e.g. 'se_social_security', 'income_tax'. */
  type: string;
  label: string;
  amountCents: number;
}

export interface WorkpaperStep {
  label: string;
  detail: string;
  amountCents?: number;
}

/** Auditable trace for one jurisdiction: every intermediate figure, plus the
 *  exact config version and content hash that produced the result. */
export interface Workpaper {
  jurisdiction: string;
  configVersion: string;
  configHash: string;
  /** True when any figure in the config is marked PROVISIONAL. */
  provisional: boolean;
  steps: WorkpaperStep[];
}

export interface SafeHarborComparison {
  /** Total tax for the jurisdiction before safe-harbor rules. */
  totalTaxCents: number;
  /** 90% of current-year tax. */
  currentYear90Cents: number;
  /** Prior-year basis actually used (prior tax × multiplier), or null when
   *  the prior-year safe harbor is unavailable. */
  priorYearBasisCents: number | null;
  /** 1.0, 1.1, or null when no prior-year basis exists. */
  priorYearMultiplier: number | null;
  /** The required annual payment = the lesser applicable basis. */
  requiredAnnualPaymentCents: number;
  basis: 'current_90' | 'prior_100' | 'prior_110' | 'current_only';
  notes: string[];
}

export interface QuarterlyPayment {
  quarter: number; // 1..4
  /** The statutory date before weekend/holiday rollover (ISO yyyy-mm-dd). */
  statutoryDate: string;
  /** The payable date after rollover (ISO yyyy-mm-dd). */
  dueDate: string;
  /** Remaining amount due this quarter (after paymentsAlreadyMade). */
  amountDueCents: number;
  /** Present on combined schedules: the per-jurisdiction parts of this date. */
  parts?: { jurisdiction: string; amountDueCents: number }[];
}

export interface JurisdictionResult {
  jurisdiction: string;
  status: ModuleStatus;
  /** Locality segment of a composed key (e.g. 'NYC'), when present. */
  locality?: string;
  localityStatus?: 'NOT_IMPLEMENTED';
  components: TaxComponent[];
  totalTaxCents: number;
  safeHarbor: SafeHarborComparison;
  schedule: QuarterlyPayment[];
  notes: string[];
  workpaper: Workpaper;
}

export interface TaxResult {
  /** The jurisdiction key as requested, e.g. 'US', 'US-CA', 'US-NY-NYC'. */
  jurisdiction: string;
  taxYear: number;
  /**
   * OK — every part computed. PARTIAL — some part is NOT_IMPLEMENTED (the
   * total then excludes it and says so). NOT_IMPLEMENTED — nothing computed.
   */
  status: ResultStatus;
  /** Combined liability of all IMPLEMENTED parts, in cents. */
  totalLiabilityCents: number;
  /** Every tax component across jurisdictions, in calculation order. */
  breakdown: TaxComponent[];
  federal: JurisdictionResult;
  state?: JurisdictionResult;
  safeHarbor: {
    federal: SafeHarborComparison;
    state?: SafeHarborComparison;
    /** Sum of the per-jurisdiction required annual payments. */
    requiredAnnualPaymentCents: number;
  };
  /** Combined payment schedule, merged by due date, chronological. */
  schedule: QuarterlyPayment[];
  /** Unique payable due dates across the combined schedule. */
  dueDates: string[];
  notes: string[];
  workpapers: Workpaper[];
}
