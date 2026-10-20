import { describe, expect, it } from 'vitest';
import { calculateTax } from '../src/index.js';

/**
 * GOLDEN TEST — the owner's worked example:
 *   $80,000 net profit, single, no W-2 wages.
 *
 * SE tax: the owner said "around $11,300"; the formula gives exactly
 * $11,303.64 (92.35% × $80,000 = $73,880; SS $9,161.12 + Medicare
 * $2,142.52). Asserted tightly.
 *
 * INCOME TAX — READ THIS: the owner expected "around $7,500". That figure
 * is what this income costs WITHOUT any QBI benefit:
 *   taxable-before-QBI $58,248.18 → tax $7,526.60.
 * With §199A applied the way the IRS orders it — standard deduction first,
 * then the QBI deduction as the LESSER of 20% of QBI ($14,869.64) or 20% of
 * (taxable-before-QBI − capital gains) ($11,649.64) — the cap binds, the
 * deduction is $11,649.64, taxable income is $46,598.54, and the correct
 * income tax is $5,343.82. The engine implements the correct ordering and
 * this test asserts the CORRECT value. The owner's $7,500 is preserved in
 * this comment (and verified below as the no-QBI comparison) for his
 * ruling — the math was not bent to hit it.
 *
 * Quarterly: total $16,647.46; with no prior-year data the safe harbor is
 * 90% of current = $14,982.71 → $3,745.68/quarter (Q4 absorbs the rounding
 * remainder so the sum is exact). The owner's "≈$4,700/quarter" corresponds
 * to ~$18.8k/yr — i.e. roughly the no-QBI total; it follows from the same
 * QBI question.
 */
describe('golden example — $80,000 net profit, single, no W-2 wages', () => {
  const r = calculateTax(80_000, 'US', { filingStatus: 'single' });
  const comp = (type: string) => r.breakdown.find((c) => c.type === type)?.amountCents;

  it('computes SE tax of exactly $11,303.64', () => {
    expect(comp('se_social_security')).toBe(916_112); // $9,161.12
    expect(comp('se_medicare')).toBe(214_252); // $2,142.52
    expect(comp('se_additional_medicare')).toBeUndefined();
    expect((comp('se_social_security') ?? 0) + (comp('se_medicare') ?? 0)).toBe(1_130_364);
  });

  it('computes income tax of $5,343.82 with correct §199A ordering (see header)', () => {
    expect(comp('income_tax')).toBe(534_382);
    const qbiStep = r.workpapers[0]?.steps.find((s) => s.label === 'QBI deduction');
    expect(qbiStep?.amountCents).toBe(1_164_964); // $11,649.64 — the lesser-of cap binds
  });

  it('documents the no-QBI comparison figure the owner expected (~$7,500)', () => {
    // Tax on $58,248.18 of taxable income with no QBI deduction:
    // 10% × 12,400 + 12% × 38,000 + 22% × 7,848.18 = 1,240 + 4,560 + 1,726.60
    const noQbiTaxCents = 124_000 + 456_000 + 172_660;
    expect(noQbiTaxCents).toBe(752_660); // $7,526.60 — the owner's ballpark
    expect(comp('income_tax')).toBeLessThan(noQbiTaxCents);
  });

  it('totals $16,647.46 and spreads the safe harbor exactly', () => {
    expect(r.totalLiabilityCents).toBe(1_664_746);
    expect(r.status).toBe('OK');
    expect(r.safeHarbor.federal.basis).toBe('current_only');
    expect(r.safeHarbor.requiredAnnualPaymentCents).toBe(1_498_271); // 90% of current
    // Quarters round half-up ($3,745.68); the last quarter absorbs the
    // remainder so the four sum EXACTLY to $14,982.71.
    expect(r.schedule.map((p) => p.amountDueCents)).toEqual([374_568, 374_568, 374_568, 374_567]);
    expect(r.schedule.reduce((a, p) => a + p.amountDueCents, 0)).toBe(1_498_271);
  });

  it('lands the 2026 due dates on the statutory business days', () => {
    expect(r.dueDates).toEqual(['2026-04-15', '2026-06-15', '2026-09-15', '2027-01-15']);
  });

  it('carries a reproducible workpaper: config version + hash + trace', () => {
    expect(r.workpapers).toHaveLength(1);
    const wp = r.workpapers[0];
    expect(wp?.configVersion).toContain('2026');
    expect(wp?.configHash).toMatch(/^[0-9a-f]{64}$/);
    const labels = wp?.steps.map((s) => s.label) ?? [];
    expect(labels).toContain('Net SE earnings');
    expect(labels).toContain('Deductible part of SE tax');
    expect(labels).toContain('Adjusted gross income');
    expect(labels).toContain('Total federal tax');
  });
});
