import { applyRate, half, maxCents, minCents, mulDivRound, toCents } from './money.js';
import { computeBracketTax } from './brackets.js';
import { computeSafeHarbor } from './safeharbor.js';
import { buildSchedule } from './schedule.js';
import { estimatedTaxDueDates } from './holidays.js';
import { configHash } from './hash.js';
import type { FederalConfig } from './config/types.js';
import { FEDERAL_2026 } from './config/federal-2026.js';
import type {
  JurisdictionResult,
  TaxComponent,
  TaxProfile,
  Workpaper,
  WorkpaperStep,
} from './types.js';

export interface FederalComputation {
  result: JurisdictionResult;
  /** Federal AGI in cents — the starting point for state modules. */
  agiCents: number;
}

/**
 * US federal computation for one estimate year: SE tax (Social Security
 * with the wage-base cap net of W-2 SS wages, Medicare, Additional Medicare
 * Tax), the half-SE-tax deduction, AGI, standard deduction, the §199A QBI
 * deduction with the IRS lesser-of ordering, bracket income tax, safe
 * harbor, and the quarterly schedule.
 */
export function computeFederalDetailed(
  netProfitCents: number,
  profile: TaxProfile,
  cfg: FederalConfig = FEDERAL_2026,
): FederalComputation {
  const status = profile.filingStatus;
  const steps: WorkpaperStep[] = [];
  const notes: string[] = [];
  const components: TaxComponent[] = [];
  const step = (label: string, detail: string, amountCents?: number) =>
    steps.push({ label, detail, ...(amountCents !== undefined ? { amountCents } : {}) });

  const w2WagesCents = toCents(profile.w2Wages ?? 0);
  const capGainsCents = toCents(profile.netCapitalGains ?? 0);

  step('Net profit (input)', 'Schedule C net profit as provided.', netProfitCents);
  if (netProfitCents <= 0) {
    notes.push(
      'Net profit is zero or negative: no self-employment tax is due. The loss still reduces AGI; a §199A loss carryforward is not modeled.',
    );
  }

  // ---- Self-employment tax -------------------------------------------------
  let seTaxCents = 0;
  let halfSeDeductionCents = 0;
  if (netProfitCents > 0) {
    const netSeEarningsCents = applyRate(netProfitCents, cfg.se.netEarningsFactor);
    step(
      'Net SE earnings',
      `Net profit × ${cfg.se.netEarningsFactor} (§1402(a)(12) employer-equivalent exclusion).`,
      netSeEarningsCents,
    );

    const wageBaseCents = toCents(cfg.se.socialSecurityWageBase);
    let w2SsWagesCents: number;
    if (profile.w2SocialSecurityWages !== undefined) {
      w2SsWagesCents = toCents(profile.w2SocialSecurityWages);
    } else if (profile.w2Wages !== undefined) {
      w2SsWagesCents = minCents(w2WagesCents, wageBaseCents);
      step(
        'W-2 Social Security wages (assumed)',
        'w2SocialSecurityWages not provided; assumed min(w2Wages, wage base). Provide Box 3 wages for an exact figure.',
        w2SsWagesCents,
      );
    } else {
      w2SsWagesCents = 0;
    }
    const remainingBaseCents = maxCents(0, wageBaseCents - w2SsWagesCents);
    const ssTaxableCents = minCents(netSeEarningsCents, remainingBaseCents);
    const ssTaxCents = applyRate(ssTaxableCents, cfg.se.socialSecurityRate);
    step(
      'Social Security tax',
      `min(net SE earnings, $${cfg.se.socialSecurityWageBase.toLocaleString()} wage base − W-2 SS wages $${(w2SsWagesCents / 100).toLocaleString()}) × ${cfg.se.socialSecurityRate * 100}%.`,
      ssTaxCents,
    );
    components.push({
      jurisdiction: cfg.jurisdiction,
      type: 'se_social_security',
      label: 'Self-employment tax — Social Security',
      amountCents: ssTaxCents,
    });

    const medicareTaxCents = applyRate(netSeEarningsCents, cfg.se.medicareRate);
    step(
      'Medicare tax',
      `Net SE earnings × ${cfg.se.medicareRate * 100}% (uncapped).`,
      medicareTaxCents,
    );
    components.push({
      jurisdiction: cfg.jurisdiction,
      type: 'se_medicare',
      label: 'Self-employment tax — Medicare',
      amountCents: medicareTaxCents,
    });

    // Additional Medicare Tax (§3101(b)(2)): 0.9% on SE earnings + W-2 wages
    // above the filing-status threshold. NOTE (documented assumption): the
    // deductible half of SE tax below is computed on the Schedule SE tax
    // (SS + regular Medicare) and excludes this Additional Medicare Tax.
    const addlThresholdCents = toCents(cfg.se.additionalMedicareThreshold[status]);
    const addlBaseCents = maxCents(0, netSeEarningsCents + w2WagesCents - addlThresholdCents);
    const addlTaxCents =
      addlBaseCents > 0 ? applyRate(addlBaseCents, cfg.se.additionalMedicareRate) : 0;
    if (addlTaxCents > 0) {
      step(
        'Additional Medicare Tax',
        `(Net SE earnings + W-2 wages − $${cfg.se.additionalMedicareThreshold[status].toLocaleString()} threshold) × ${cfg.se.additionalMedicareRate * 100}%.`,
        addlTaxCents,
      );
      components.push({
        jurisdiction: cfg.jurisdiction,
        type: 'se_additional_medicare',
        label: 'Additional Medicare Tax',
        amountCents: addlTaxCents,
      });
    }

    const scheduleSeTaxCents = ssTaxCents + medicareTaxCents;
    seTaxCents = scheduleSeTaxCents + addlTaxCents;
    halfSeDeductionCents = half(scheduleSeTaxCents);
    step(
      'Deductible part of SE tax',
      '50% of the Schedule SE tax (Social Security + regular Medicare). The Additional Medicare Tax is excluded — it has no employer-equivalent share (documented assumption).',
      halfSeDeductionCents,
    );
    step('Total SE tax', 'Social Security + Medicare + Additional Medicare Tax.', seTaxCents);
  }

  // ---- AGI, standard deduction, QBI --------------------------------------
  const agiCents = w2WagesCents + netProfitCents - halfSeDeductionCents;
  step('Adjusted gross income', 'W-2 wages + net profit − deductible part of SE tax.', agiCents);

  const stdDedCents = toCents(cfg.standardDeduction[status]);
  step(
    'Standard deduction',
    `2026 standard deduction for filing status ${status}.`,
    stdDedCents,
  );

  const taxableBeforeQbiCents = maxCents(0, agiCents - stdDedCents);
  step('Taxable income before QBI', 'AGI − standard deduction (floored at $0).', taxableBeforeQbiCents);

  let qbiDeductionCents = 0;
  if (netProfitCents > 0) {
    // QBI is reduced by the deductible part of SE tax attributable to the
    // business (Reg. §1.199A-3(b)); W-2 wages are not QBI.
    const qbiBaseCents = maxCents(0, netProfitCents - halfSeDeductionCents);
    const tentativeCents = applyRate(qbiBaseCents, cfg.qbi.rate);
    const capCents = applyRate(
      maxCents(0, taxableBeforeQbiCents - capGainsCents),
      cfg.qbi.rate,
    );
    step(
      'QBI — tentative amount',
      `20% of qualified business income (net profit − deductible SE tax = $${(qbiBaseCents / 100).toLocaleString()}).`,
      tentativeCents,
    );
    step(
      'QBI — overall cap',
      '20% of (taxable income before QBI − net capital gains) — the §199A(a)(1)(B) lesser-of cap.',
      capCents,
    );

    let applicableCents = tentativeCents;
    const thresholdCents = toCents(cfg.qbi.threshold[status]);
    const rangeCents = toCents(cfg.qbi.phaseInRange[status]);
    if (taxableBeforeQbiCents > thresholdCents) {
      const excessCents = taxableBeforeQbiCents - thresholdCents;
      if (profile.isSSTB) {
        if (excessCents >= rangeCents) {
          applicableCents = 0;
          step(
            'QBI — SSTB exclusion',
            'SSTB with taxable income above the phase-in range: no QBI deduction.',
            0,
          );
        } else {
          applicableCents = mulDivRound(tentativeCents, rangeCents - excessCents, rangeCents);
          step(
            'QBI — SSTB phase-in',
            `SSTB within the phase-in range: tentative amount scaled by the applicable percentage (${(((rangeCents - excessCents) / rangeCents) * 100).toFixed(1)}%).`,
            applicableCents,
          );
        }
      } else {
        step(
          'QBI — above threshold (non-SSTB)',
          'Above the §199A threshold. The W-2 wage / UBIA limitation is NOT modeled (documented simplification); for a business with employees or qualified property this figure can overstate the deduction — review before relying on it.',
          applicableCents,
        );
      }
    }

    qbiDeductionCents = minCents(applicableCents, capCents);

    // 2026 §199A(i) minimum deduction (OBBBA): at least $400 when aggregate
    // QBI ≥ $1,000, except for an SSTB fully phased out.
    const sstbFullyPhased =
      profile.isSSTB === true && taxableBeforeQbiCents > thresholdCents + rangeCents;
    if (!sstbFullyPhased && qbiBaseCents >= toCents(cfg.qbi.minimumQbi)) {
      const floorCents = minCents(toCents(cfg.qbi.minimumDeduction), taxableBeforeQbiCents);
      if (floorCents > qbiDeductionCents) {
        qbiDeductionCents = floorCents;
        step(
          'QBI — §199A(i) minimum deduction',
          `2026 minimum deduction of $${cfg.qbi.minimumDeduction} applies (aggregate QBI ≥ $${cfg.qbi.minimumQbi}).`,
          qbiDeductionCents,
        );
      }
    }
    qbiDeductionCents = minCents(qbiDeductionCents, taxableBeforeQbiCents);
    step(
      'QBI deduction',
      'Lesser of the (phase-adjusted) tentative amount and the overall cap, after the §199A(i) floor. Ordering per §199A(a): computed AFTER the standard deduction.',
      qbiDeductionCents,
    );
  }

  const taxableIncomeCents = taxableBeforeQbiCents - qbiDeductionCents;
  step('Taxable income', 'Taxable income before QBI − QBI deduction.', taxableIncomeCents);

  // ---- Income tax ---------------------------------------------------------
  // The before/after QBI pair is a first-class output (owner requirement):
  // the same bracket engine runs on taxable income before the deduction so
  // the QBI benefit is a visible, auditable number — not buried in the trace.
  const incomeTaxBeforeQbiCents = computeBracketTax(
    taxableBeforeQbiCents,
    cfg.brackets[status],
  ).totalCents;
  step(
    'Income tax before QBI deduction',
    'Bracket tax on taxable income BEFORE the §199A deduction.',
    incomeTaxBeforeQbiCents,
  );
  const bracketResult = computeBracketTax(taxableIncomeCents, cfg.brackets[status]);
  for (const line of bracketResult.lines) {
    if (line.taxableCents > 0) {
      step(
        `Income tax — ${(line.rate * 100).toFixed(0)}% bracket`,
        `$${(line.fromCents / 100).toLocaleString()}–${line.toCents === null ? '∞' : `$${(line.toCents / 100).toLocaleString()}`}: $${(line.taxableCents / 100).toLocaleString()} taxed.`,
        line.taxCents,
      );
    }
  }
  const incomeTaxCents = bracketResult.totalCents;
  step('Income tax', 'Sum of bracket lines (ordinary rates; preferential capital-gains rates not modeled).', incomeTaxCents);
  if (qbiDeductionCents > 0) {
    step(
      'QBI tax benefit',
      'Income tax before QBI − income tax after QBI: the cash value of the deduction at these brackets.',
      incomeTaxBeforeQbiCents - incomeTaxCents,
    );
  }
  components.push({
    jurisdiction: cfg.jurisdiction,
    type: 'income_tax',
    label: 'Federal income tax',
    amountCents: incomeTaxCents,
  });

  const totalTaxCents = seTaxCents + incomeTaxCents;
  step('Total federal tax', 'SE tax + income tax (before payments/credits other than those in the profile).', totalTaxCents);

  // ---- Safe harbor + schedule --------------------------------------------
  const safeHarbor = computeSafeHarbor(totalTaxCents, profile, cfg.safeHarbor);
  const paymentsCents = toCents(profile.paymentsAlreadyMade ?? 0);
  const remainingCents = maxCents(0, safeHarbor.requiredAnnualPaymentCents - paymentsCents);
  if (paymentsCents > 0) {
    step(
      'Payments already made',
      'Withholding/estimates from the profile, netted against the required annual payment; the remainder is re-spread across the four quarters (withholding convention).',
      paymentsCents,
    );
  }
  const schedule = buildSchedule(
    remainingCents,
    cfg.installmentPcts,
    estimatedTaxDueDates(cfg.taxYear),
  );

  const workpaper: Workpaper = {
    jurisdiction: cfg.jurisdiction,
    configVersion: cfg.version,
    configHash: configHash(cfg),
    provisional: cfg.provisional,
    steps,
  };

  const result: JurisdictionResult = {
    jurisdiction: cfg.jurisdiction,
    status: 'OK',
    components,
    totalTaxCents,
    incomeTaxBeforeQbiCents,
    qbiDeductionCents,
    incomeTaxCents,
    safeHarbor,
    schedule,
    notes,
    workpaper,
  };
  return { result, agiCents };
}

/** Federal-only convenience wrapper matching the state-module interface. */
export function computeFederal(
  netProfitCents: number,
  profile: TaxProfile,
  cfg: FederalConfig = FEDERAL_2026,
): JurisdictionResult {
  return computeFederalDetailed(netProfitCents, profile, cfg).result;
}
