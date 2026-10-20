import { describe, expect, it } from 'vitest';
import { calculateTax } from '../src/index.js';
import { computeFederal } from '../src/federal.js';

const comp = (r: ReturnType<typeof calculateTax>, type: string) =>
  r.breakdown.find((c) => c.type === type)?.amountCents;

describe('Social Security wage-base cap', () => {
  it('caps SS at exactly 12.4% × $184,500 when SE earnings exceed the base', () => {
    // $300,000 profit → SE earnings $277,050 > $184,500 base.
    const r = calculateTax(300_000, 'US', { filingStatus: 'single' });
    expect(comp(r, 'se_social_security')).toBe(2_287_800); // $22,878.00 exactly
    expect(comp(r, 'se_medicare')).toBe(803_445); // 2.9% × $277,050, uncapped
  });

  it('reduces the cap by W-2 Social Security wages', () => {
    const r = calculateTax(300_000, 'US', {
      filingStatus: 'single',
      w2Wages: 100_000,
      w2SocialSecurityWages: 100_000,
    });
    // Remaining base $84,500 × 12.4% = $10,478.00
    expect(comp(r, 'se_social_security')).toBe(1_047_800);
  });

  it('assumes W-2 SS wages = min(w2Wages, base) when only w2Wages is given', () => {
    const r = calculateTax(300_000, 'US', { filingStatus: 'single', w2Wages: 100_000 });
    expect(comp(r, 'se_social_security')).toBe(1_047_800);
    const step = r.workpapers[0]?.steps.find((s) => s.label.includes('assumed'));
    expect(step).toBeDefined();
  });

  it('does not cap SS below the base', () => {
    const r = calculateTax(80_000, 'US', { filingStatus: 'single' });
    expect(comp(r, 'se_social_security')).toBe(916_112); // 12.4% × $73,880
  });
});

describe('Additional Medicare Tax', () => {
  it('applies 0.9% above $200,000 (single)', () => {
    const r = calculateTax(300_000, 'US', { filingStatus: 'single' });
    // ($277,050 − $200,000) × 0.9% = $693.45
    expect(comp(r, 'se_additional_medicare')).toBe(69_345);
  });

  it('uses the $250,000 MFJ threshold', () => {
    const r = calculateTax(300_000, 'US', { filingStatus: 'married_filing_jointly' });
    // ($277,050 − $250,000) × 0.9% = $243.45
    expect(comp(r, 'se_additional_medicare')).toBe(24_345);
  });

  it('does not apply at the golden income level', () => {
    const r = calculateTax(80_000, 'US', { filingStatus: 'single' });
    expect(comp(r, 'se_additional_medicare')).toBeUndefined();
  });
});

describe('filing statuses', () => {
  it('MFJ: wider brackets + $32,200 standard deduction lower the income tax', () => {
    const r = calculateTax(80_000, 'US', { filingStatus: 'married_filing_jointly' });
    expect(comp(r, 'se_social_security')).toBe(916_112); // SE tax is status-independent
    // Taxable before QBI $42,148.18; QBI cap 20% × 42,148.18 = $8,429.64;
    // taxable $33,718.54 → 10% × 24,800 + 12% × 8,918.54 = $3,550.22
    expect(comp(r, 'income_tax')).toBe(355_022);
  });
});

describe('edge behavior', () => {
  it('$0 profit: zero tax, explicit loss/zero note, no NaN', () => {
    const r = calculateTax(0, 'US', { filingStatus: 'single' });
    expect(r.totalLiabilityCents).toBe(0);
    expect(Number.isFinite(r.totalLiabilityCents)).toBe(true);
    expect(r.notes.some((n) => n.includes('zero or negative'))).toBe(true);
    expect(r.schedule).toHaveLength(4);
    expect(r.schedule.every((p) => p.amountDueCents === 0)).toBe(true);
  });

  it('negative profit: never a negative tax, never NaN', () => {
    const r = calculateTax(-5_000, 'US', { filingStatus: 'single' });
    expect(r.totalLiabilityCents).toBe(0);
    expect(r.totalLiabilityCents).toBeGreaterThanOrEqual(0);
    expect(Number.isFinite(r.totalLiabilityCents)).toBe(true);
  });

  it('payments already made reduce the remaining schedule, floored at zero', () => {
    const r = calculateTax(80_000, 'US', {
      filingStatus: 'single',
      paymentsAlreadyMade: 10_000,
    });
    // Required $14,982.71 − $10,000 = $4,982.71 remaining, spread exactly.
    expect(r.schedule.reduce((a, p) => a + p.amountDueCents, 0)).toBe(498_271);
    const over = calculateTax(80_000, 'US', {
      filingStatus: 'single',
      paymentsAlreadyMade: 99_999,
    });
    expect(over.schedule.every((p) => p.amountDueCents === 0)).toBe(true);
  });

  it('rejects non-finite profit', () => {
    expect(() => calculateTax(Number.NaN, 'US', { filingStatus: 'single' })).toThrow();
  });
});

describe('SSTB treatment', () => {
  it('partially phases the deduction for an SSTB inside the phase-in range', () => {
    // $300k profit → taxable-before-QBI ≈ $268,444, inside the 2026 range
    // ($201,750–$276,750): the deduction survives only in part.
    const r = calculateTax(300_000, 'US', { filingStatus: 'single', isSSTB: true });
    const labels = r.workpapers[0]?.steps.map((s) => s.label) ?? [];
    expect(labels.some((l) => l.includes('SSTB phase-in'))).toBe(true);
    const qbi = r.workpapers[0]?.steps.find((s) => s.label === 'QBI deduction');
    expect(qbi?.amountCents ?? 0).toBeGreaterThan(0);
    expect(qbi?.amountCents ?? 0).toBeLessThan(5_690_875); // below the full tentative amount
  });

  it('phases the QBI deduction to zero for an SSTB above the range', () => {
    const r = calculateTax(400_000, 'US', { filingStatus: 'single', isSSTB: true });
    const qbi = r.workpapers[0]?.steps.find((s) => s.label === 'QBI deduction');
    expect(qbi?.amountCents).toBe(0);
  });

  it('keeps the full deduction for a non-SSTB at the same income', () => {
    const r = calculateTax(400_000, 'US', { filingStatus: 'single' });
    const qbi = r.workpapers[0]?.steps.find((s) => s.label === 'QBI deduction');
    expect(qbi?.amountCents ?? 0).toBeGreaterThan(0);
  });
});

describe('§199A(i) minimum deduction (2026 OBBBA floor)', () => {
  it('floors the deduction at taxable income when the 20% cap is smaller', () => {
    // $17,500 profit: taxable-before-QBI is only $163.66, so the lesser-of
    // cap yields $32.73 — but QBI ≥ $1,000, so the $400 minimum applies,
    // clamped at taxable income: deduction = $163.66, income tax = $0.
    const r = calculateTax(17_500, 'US', { filingStatus: 'single' });
    const labels = r.workpapers[0]?.steps.map((s) => s.label) ?? [];
    expect(labels.some((l) => l.includes('§199A(i) minimum'))).toBe(true);
    const qbi = r.workpapers[0]?.steps.find((s) => s.label === 'QBI deduction');
    expect(qbi?.amountCents).toBe(16_366);
    expect(comp(r, 'income_tax')).toBe(0);
  });
});

describe('computeFederal module wrapper', () => {
  it('matches the federal slice of calculateTax', () => {
    const direct = computeFederal(8_000_000, { filingStatus: 'single' });
    const viaApi = calculateTax(80_000, 'US', { filingStatus: 'single' });
    expect(direct.totalTaxCents).toBe(viaApi.federal.totalTaxCents);
    expect(direct.jurisdiction).toBe('US');
  });
});
