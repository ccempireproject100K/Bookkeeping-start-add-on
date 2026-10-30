import { describe, expect, it } from 'vitest';
import { calculateTax } from '../src/index.js';

describe('property: quarterly schedules always sum exactly to the required annual payment', () => {
  const profits = [0, 1, 100, 5_000, 12_345, 80_000, 123_456, 300_000, 999_999];
  const keys = ['US', 'US-CA', 'US-NY', 'US-TX'];

  for (const key of keys) {
    for (const profit of profits) {
      it(`${key} @ $${profit}`, () => {
        const r = calculateTax(profit, key, { filingStatus: 'single' });
        const sum = r.schedule.reduce((a, p) => a + p.amountDueCents, 0);
        expect(sum).toBe(r.safeHarbor.requiredAnnualPaymentCents);
        for (const p of r.schedule) {
          expect(Number.isInteger(p.amountDueCents)).toBe(true);
          expect(p.amountDueCents).toBeGreaterThanOrEqual(0);
          // per-jurisdiction parts sum to the merged amount
          const parts = (p.parts ?? []).reduce((a, part) => a + part.amountDueCents, 0);
          expect(parts).toBe(p.amountDueCents);
        }
      });
    }
  }
});

describe('property: liability is monotonic non-decreasing in profit', () => {
  it('holds across a profit ladder for federal and state jurisdictions', () => {
    const ladder = [0, 1_000, 5_000, 20_000, 50_000, 80_000, 120_000, 200_000, 400_000, 1_000_000];
    for (const key of ['US', 'US-CA', 'US-NY']) {
      let prev = -1;
      for (const profit of ladder) {
        const r = calculateTax(profit, key, { filingStatus: 'single' });
        expect(r.totalLiabilityCents, `${key} @ ${profit}`).toBeGreaterThanOrEqual(prev);
        prev = r.totalLiabilityCents;
      }
    }
  });
});

describe('property: results are always finite and never negative', () => {
  it('holds for awkward inputs across statuses', () => {
    const statuses = [
      'single',
      'married_filing_jointly',
      'married_filing_separately',
      'head_of_household',
    ] as const;
    for (const filingStatus of statuses) {
      for (const profit of [-100_000, -1, 0, 0.01, 1, 999.99, 80_000, 5_000_000]) {
        const r = calculateTax(profit, 'US-CA', { filingStatus });
        expect(Number.isFinite(r.totalLiabilityCents)).toBe(true);
        expect(r.totalLiabilityCents).toBeGreaterThanOrEqual(0);
        expect(r.schedule.every((p) => Number.isFinite(p.amountDueCents))).toBe(true);
      }
    }
  });
});
