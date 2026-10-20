import { describe, expect, it } from 'vitest';
import { computeSafeHarbor } from '../src/safeharbor.js';
import { FEDERAL_2026 } from '../src/config/federal-2026.js';
import { CALIFORNIA_2026 } from '../src/config/california-2026.js';
import { calculateTax } from '../src/index.js';

const FED = FEDERAL_2026.safeHarbor;
const TOTAL = 2_000_000; // $20,000 current-year tax
const PRIOR_TAX = 1_000_000; // $10,000 prior-year tax

describe('safe harbor — the $150,000 AGI line (federal)', () => {
  it('prior AGI of exactly $150,000 does NOT trigger the 110% rule (federal: strictly greater)', () => {
    const sh = computeSafeHarbor(
      TOTAL,
      { filingStatus: 'single', priorYearTax: 10_000, priorYearAGI: 150_000 },
      FED,
    );
    expect(sh.priorYearMultiplier).toBe(1);
    expect(sh.priorYearBasisCents).toBe(PRIOR_TAX);
    expect(sh.requiredAnnualPaymentCents).toBe(PRIOR_TAX); // min(90% × 20k, 10k)
    expect(sh.basis).toBe('prior_100');
  });

  it('prior AGI of $151,000 triggers the 110% rule', () => {
    const sh = computeSafeHarbor(
      TOTAL,
      { filingStatus: 'single', priorYearTax: 10_000, priorYearAGI: 151_000 },
      FED,
    );
    expect(sh.priorYearMultiplier).toBe(1.1);
    expect(sh.priorYearBasisCents).toBe(1_100_000);
    expect(sh.requiredAnnualPaymentCents).toBe(1_100_000);
  });

  it('90% of current wins when it is the smaller figure', () => {
    const sh = computeSafeHarbor(
      TOTAL,
      { filingStatus: 'single', priorYearTax: 19_000, priorYearAGI: 100_000 },
      FED,
    );
    expect(sh.requiredAnnualPaymentCents).toBe(1_800_000); // 90% × $20,000
    expect(sh.basis).toBe('current_90');
  });

  it('no prior-year data → 90% of current only', () => {
    const sh = computeSafeHarbor(TOTAL, { filingStatus: 'single' }, FED);
    expect(sh.basis).toBe('current_only');
    expect(sh.priorYearBasisCents).toBeNull();
    expect(sh.requiredAnnualPaymentCents).toBe(1_800_000);
  });

  it('short prior year (not filed for a full 12 months) → prior basis unavailable', () => {
    const sh = computeSafeHarbor(
      TOTAL,
      {
        filingStatus: 'single',
        priorYearTax: 10_000,
        priorYearAGI: 200_000,
        priorYearFiledFullYear: false,
      },
      FED,
    );
    expect(sh.basis).toBe('current_only');
    expect(sh.notes.some((n) => n.includes('less than 12 months'))).toBe(true);
  });

  it('prior-year tax without prior-year AGI: 100% basis, 110% override not applied', () => {
    const sh = computeSafeHarbor(
      TOTAL,
      { filingStatus: 'single', priorYearTax: 10_000 },
      FED,
    );
    expect(sh.priorYearMultiplier).toBe(1);
    expect(sh.notes.some((n) => n.includes('AGI not provided'))).toBe(true);
  });

  it('prior-year tax of $0 → prior basis unavailable (cannot divide a safe harbor out of zero)', () => {
    const sh = computeSafeHarbor(
      TOTAL,
      { filingStatus: 'single', priorYearTax: 0, priorYearAGI: 200_000 },
      FED,
    );
    expect(sh.basis).toBe('current_only');
  });
});

describe('safe harbor — state variations are config-driven', () => {
  it('California applies 110% at prior AGI ≥ $150,000 (inclusive, unlike federal)', () => {
    if (!CALIFORNIA_2026.safeHarbor) throw new Error('CA safe harbor config missing');
    const sh = computeSafeHarbor(
      TOTAL,
      { filingStatus: 'single', priorYearTax: 10_000, priorYearAGI: 150_000 },
      CALIFORNIA_2026.safeHarbor,
    );
    expect(sh.priorYearMultiplier).toBe(1.1);
  });
});

describe('safe harbor — end to end through calculateTax', () => {
  it('surfaces the 110% multiplier in the result', () => {
    const r = calculateTax(80_000, 'US', {
      filingStatus: 'single',
      priorYearTax: 12_000,
      priorYearAGI: 151_000,
    });
    expect(r.safeHarbor.federal.priorYearMultiplier).toBe(1.1);
    expect(r.safeHarbor.federal.requiredAnnualPaymentCents).toBe(1_320_000); // 110% × $12,000
    expect(r.schedule.reduce((a, p) => a + p.amountDueCents, 0)).toBe(1_320_000);
  });
});
