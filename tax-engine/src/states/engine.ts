import { applyRate, maxCents, toCents } from '../money.js';
import { computeBracketTax } from '../brackets.js';
import { computeSafeHarbor } from '../safeharbor.js';
import { buildSchedule } from '../schedule.js';
import { estimatedTaxDueDates } from '../holidays.js';
import { configHash } from '../hash.js';
import type { StateConfig } from '../config/types.js';
import type {
  JurisdictionResult,
  SafeHarborComparison,
  TaxComponent,
  TaxProfile,
  Workpaper,
  WorkpaperStep,
} from '../types.js';

/** Everything a state module needs from the federal computation. */
export interface StateContext {
  netProfitCents: number;
  /** Federal AGI — state modules start from it (documented simplification:
   *  state-specific additions/subtractions are not modeled). */
  federalAgiCents: number;
  profile: TaxProfile;
}

/** The interface every state module exports alongside its config. */
export type StateCalculator = (ctx: StateContext, locality?: string) => JurisdictionResult;

function zeroSafeHarbor(note: string): SafeHarborComparison {
  return {
    totalTaxCents: 0,
    currentYear90Cents: 0,
    priorYearBasisCents: null,
    priorYearMultiplier: null,
    requiredAnnualPaymentCents: 0,
    basis: 'current_only',
    notes: [note],
  };
}

function workpaperFor(cfg: StateConfig, steps: WorkpaperStep[]): Workpaper {
  return {
    jurisdiction: cfg.jurisdiction,
    configVersion: cfg.version,
    configHash: configHash(cfg),
    provisional: cfg.provisional,
    steps,
  };
}

/**
 * Effective profile for a state: prior-year figures and payments can be
 * overridden per state via profile.state[CODE]; payments default to 0 for
 * states (NOT the federal figure) so combined schedules never double-count
 * the same dollars.
 */
function effectiveStateProfile(cfg: StateConfig, profile: TaxProfile): TaxProfile {
  const ns = profile.state?.[cfg.stateCode] ?? {};
  const num = (v: unknown): number | undefined => (typeof v === 'number' ? v : undefined);
  const eff: TaxProfile = {
    ...profile,
    paymentsAlreadyMade: num(ns['paymentsAlreadyMade']) ?? 0,
  };
  const priorTax = num(ns['priorYearTax']);
  if (priorTax !== undefined) eff.priorYearTax = priorTax;
  const priorAgi = num(ns['priorYearAGI']);
  if (priorAgi !== undefined) eff.priorYearAGI = priorAgi;
  return eff;
}

/** Generic progressive-bracket state engine (CA, NY). All state-specific
 *  behavior lives in the config; this function holds no state constants. */
export function progressiveCalculator(
  cfg: StateConfig,
  opts?: { localityNote?: (locality: string) => string },
): StateCalculator {
  return (ctx, locality) => {
    if (!cfg.standardDeduction || !cfg.brackets || !cfg.safeHarbor || !cfg.installmentPcts) {
      throw new Error(`State config ${cfg.jurisdiction} is marked full but is missing tables`);
    }
    const profile = effectiveStateProfile(cfg, ctx.profile);
    const status = profile.filingStatus;
    const steps: WorkpaperStep[] = [];
    const notes: string[] = [];
    const components: TaxComponent[] = [];
    const step = (label: string, detail: string, amountCents?: number) =>
      steps.push({ label, detail, ...(amountCents !== undefined ? { amountCents } : {}) });

    step('State starting point', 'Federal AGI (state additions/subtractions not modeled).', ctx.federalAgiCents);
    const stdDedCents = toCents(cfg.standardDeduction[status]);
    step('State standard deduction', `2026 ${cfg.stateCode} standard deduction for ${status}.`, stdDedCents);
    const taxableCents = maxCents(0, ctx.federalAgiCents - stdDedCents);
    step('State taxable income', 'Federal AGI − state standard deduction (floored at $0).', taxableCents);

    const bracketResult = computeBracketTax(taxableCents, cfg.brackets[status]);
    for (const line of bracketResult.lines) {
      if (line.taxableCents > 0) {
        step(
          `State tax — ${(line.rate * 100).toFixed(2).replace(/\.?0+$/, '')}% bracket`,
          `$${(line.fromCents / 100).toLocaleString()}–${line.toCents === null ? '∞' : `$${(line.toCents / 100).toLocaleString()}`}: $${(line.taxableCents / 100).toLocaleString()} taxed.`,
          line.taxCents,
        );
      }
    }
    components.push({
      jurisdiction: cfg.jurisdiction,
      type: 'state_income_tax',
      label: `${cfg.stateCode} income tax`,
      amountCents: bracketResult.totalCents,
    });
    let totalCents = bracketResult.totalCents;

    if (cfg.surtax) {
      const thresholdCents = toCents(cfg.surtax.threshold);
      if (taxableCents > thresholdCents) {
        const surtaxCents = applyRate(taxableCents - thresholdCents, cfg.surtax.rate);
        components.push({
          jurisdiction: cfg.jurisdiction,
          type: 'state_surtax',
          label: `${cfg.stateCode} ${cfg.surtax.label}`,
          amountCents: surtaxCents,
        });
        totalCents += surtaxCents;
        step(cfg.surtax.label, `${cfg.surtax.rate * 100}% of taxable income over $${cfg.surtax.threshold.toLocaleString()}.`, surtaxCents);
      }
    }
    step(`Total ${cfg.stateCode} tax`, 'Bracket tax plus any surtax.', totalCents);

    for (const omission of cfg.knownOmissions ?? []) {
      step(omission.label, omission.detail);
      notes.push(omission.detail);
    }

    const safeHarbor = computeSafeHarbor(totalCents, profile, cfg.safeHarbor);
    const remainingCents = maxCents(
      0,
      safeHarbor.requiredAnnualPaymentCents - toCents(profile.paymentsAlreadyMade ?? 0),
    );
    const schedule = buildSchedule(
      remainingCents,
      cfg.installmentPcts,
      estimatedTaxDueDates(cfg.taxYear),
    );
    notes.push(
      'State estimated-tax due dates use the federal statutory dates with federal holiday rollover (state-specific holiday calendars are not modeled).',
    );
    if (cfg.provisional) {
      notes.push(
        `${cfg.stateCode} 2026 figures are PROVISIONAL — see the workpaper source note and re-verify before relying on them.`,
      );
    }

    const result: JurisdictionResult = {
      jurisdiction: cfg.jurisdiction,
      status: 'OK',
      components,
      totalTaxCents: totalCents,
      safeHarbor,
      schedule,
      notes,
      workpaper: workpaperFor(cfg, steps),
    };
    if (locality !== undefined) {
      result.locality = locality;
      result.localityStatus = 'NOT_IMPLEMENTED';
      notes.push(
        opts?.localityNote?.(locality) ??
          `Locality tax for ${locality} is not implemented and is NOT included in these figures.`,
      );
    }
    return result;
  };
}

/** No-tax states: a TRUE zero — the legally correct answer, with the reason. */
export function noTaxCalculator(cfg: StateConfig): StateCalculator {
  return () => {
    const note = cfg.noTaxNote ?? `${cfg.stateCode} imposes no personal income tax.`;
    return {
      jurisdiction: cfg.jurisdiction,
      status: 'OK',
      components: [],
      totalTaxCents: 0,
      safeHarbor: zeroSafeHarbor('No tax in this jurisdiction; no estimated payments are required.'),
      schedule: buildSchedule(0, [0.25, 0.25, 0.25, 0.25], estimatedTaxDueDates(cfg.taxYear)),
      notes: [note],
      workpaper: workpaperFor(cfg, [
        { label: 'No-tax jurisdiction', detail: note, amountCents: 0 },
      ]),
    };
  };
}

/**
 * Stub states: an explicit NOT_IMPLEMENTED result. The zero amounts here are
 * placeholders guarded by the status — callers must surface the status and
 * must never present this as a computed $0 liability.
 */
export function stubCalculator(cfg: StateConfig): StateCalculator {
  return () => {
    const reason = cfg.stubReason ?? 'Module not implemented.';
    return {
      jurisdiction: cfg.jurisdiction,
      status: 'NOT_IMPLEMENTED',
      components: [],
      totalTaxCents: 0,
      safeHarbor: zeroSafeHarbor('Module not implemented; no safe-harbor computation was performed.'),
      schedule: [],
      notes: [`NOT_IMPLEMENTED: ${reason}`],
      workpaper: workpaperFor(cfg, [
        { label: 'Stub module', detail: reason },
      ]),
    };
  };
}
