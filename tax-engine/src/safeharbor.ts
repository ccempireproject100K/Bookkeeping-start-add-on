import { applyRate, toCents } from './money.js';
import type { SafeHarborComparison, TaxProfile } from './types.js';
import type { SafeHarborParams } from './config/types.js';

/**
 * Safe-harbor required annual payment:
 *   required = min( 90% of current-year tax,
 *                   prior-year tax × (110% if prior AGI over the high-income
 *                   threshold, else 100%) )
 * The prior-year basis is unavailable when there is no prior-year tax on
 * record, when it was zero, or when the prior return covered < 12 months.
 * (Taxpayers who filed no return / owed no tax last year cannot use it.)
 */
export function computeSafeHarbor(
  totalTaxCents: number,
  profile: TaxProfile,
  params: SafeHarborParams,
): SafeHarborComparison {
  const notes: string[] = [];
  const currentYear90Cents = applyRate(Math.max(0, totalTaxCents), params.currentYearPct);

  let priorYearBasisCents: number | null = null;
  let priorYearMultiplier: number | null = null;

  const priorTax = profile.priorYearTax;
  if (priorTax === undefined) {
    notes.push('No prior-year tax provided; prior-year safe harbor not evaluated.');
  } else if (profile.priorYearFiledFullYear === false) {
    notes.push('Prior-year return covered less than 12 months; prior-year safe harbor unavailable.');
  } else if (priorTax <= 0) {
    notes.push('Prior-year tax was zero; prior-year safe harbor unavailable.');
  } else {
    const thresholdCents = toCents(params.highIncomeAgiThreshold);
    const priorAgiCents = profile.priorYearAGI === undefined ? null : toCents(profile.priorYearAGI);
    const comparison = params.highIncomeComparison ?? 'gt';
    const highIncome =
      priorAgiCents !== null &&
      (comparison === 'gte' ? priorAgiCents >= thresholdCents : priorAgiCents > thresholdCents);
    priorYearMultiplier = highIncome ? params.priorYearHighIncomePct : params.priorYearPct;
    priorYearBasisCents = applyRate(toCents(priorTax), priorYearMultiplier);
    if (priorAgiCents === null) {
      notes.push('Prior-year AGI not provided; the 110% high-income override was not applied.');
    }
  }

  let requiredAnnualPaymentCents: number;
  let basis: SafeHarborComparison['basis'];
  if (priorYearBasisCents === null || priorYearMultiplier === null) {
    requiredAnnualPaymentCents = currentYear90Cents;
    basis = 'current_only';
  } else if (priorYearBasisCents <= currentYear90Cents) {
    requiredAnnualPaymentCents = priorYearBasisCents;
    basis =
      priorYearMultiplier === params.priorYearHighIncomePct &&
      params.priorYearHighIncomePct !== params.priorYearPct
        ? 'prior_110'
        : 'prior_100';
  } else {
    requiredAnnualPaymentCents = currentYear90Cents;
    basis = 'current_90';
  }

  return {
    totalTaxCents,
    currentYear90Cents,
    priorYearBasisCents,
    priorYearMultiplier,
    requiredAnnualPaymentCents,
    basis,
    notes,
  };
}
