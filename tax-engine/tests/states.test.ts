import { describe, expect, it } from 'vitest';
import {
  calculateTax,
  getStateModule,
  listJurisdictions,
  UnknownJurisdictionError,
} from '../src/index.js';
import { calculate as calculateNY } from '../src/states/newyork.js';
import { progressiveCalculator } from '../src/states/engine.js';
import { CALIFORNIA_2026 } from '../src/config/california-2026.js';
import { NEW_YORK_2026 } from '../src/config/newyork-2026.js';

const CTX = {
  netProfitCents: 8_000_000,
  federalAgiCents: 7_434_818,
  profile: { filingStatus: 'single' } as const,
};

const GOLDEN = { filingStatus: 'single' } as const;

describe('California (real implementation)', () => {
  const r = calculateTax(80_000, 'US-CA', GOLDEN);

  it('computes a sane, hand-checked state tax on the golden profile', () => {
    // Verified 2026 table; hand-checked: taxable $68,448.18 on federal
    // AGI $74,348.18 − $5,900 std ded → 1/2/4/6/8% lines = $2,770.97.
    expect(r.state?.status).toBe('OK');
    expect(r.state?.totalTaxCents).toBe(277_097);
  });

  it('carries the QBI before/after fields on federal only, never on the state result', () => {
    expect(r.federal.incomeTaxBeforeQbiCents).toBe(752_660);
    expect(r.federal.incomeTaxCents).toBe(534_382);
    expect(r.state?.incomeTaxBeforeQbiCents).toBeUndefined();
    expect(r.state?.qbiDeductionCents).toBeUndefined();
    expect(r.state?.incomeTaxCents).toBeUndefined();
  });

  it('combines federal + state in the total and breakdown', () => {
    expect(r.totalLiabilityCents).toBe(1_664_746 + 277_097);
    expect(r.breakdown.some((c) => c.jurisdiction === 'US-CA' && c.type === 'state_income_tax')).toBe(true);
    expect(r.workpapers).toHaveLength(2);
  });

  it('uses the CA 30/40/0/30 installment pattern — Q3 combined is federal-only', () => {
    expect(r.safeHarbor.requiredAnnualPaymentCents).toBe(1_498_271 + 249_387);
    expect(r.schedule).toHaveLength(4);
    const q3 = r.schedule[2];
    expect(q3?.parts?.find((p) => p.jurisdiction === 'US-CA')?.amountDueCents).toBe(0);
    expect(q3?.amountDueCents).toBe(374_568);
    expect(r.schedule.reduce((a, p) => a + p.amountDueCents, 0)).toBe(1_747_658);
  });
});

describe('New York (real implementation) + NYC locality', () => {
  it('computes a sane, hand-checked state tax on the golden profile', () => {
    // Verified 2026 table; hand-checked: taxable $66,348.18 on federal
    // AGI $74,348.18 − $8,000 std ded → 3.9/4.4/5.15/5.4% lines = $3,417.80.
    const r = calculateTax(80_000, 'US-NY', GOLDEN);
    expect(r.state?.totalTaxCents).toBe(341_780);
    expect(r.totalLiabilityCents).toBe(1_664_746 + 341_780);
    expect(r.status).toBe('OK');
  });

  it('US-NY-NYC computes state tax and flags the locality as NOT_IMPLEMENTED', () => {
    const withCity = calculateTax(80_000, 'US-NY-NYC', GOLDEN);
    const stateOnly = calculateTax(80_000, 'US-NY', GOLDEN);
    expect(withCity.state?.locality).toBe('NYC');
    expect(withCity.state?.localityStatus).toBe('NOT_IMPLEMENTED');
    // NYC tax is excluded — the state figure is identical, never padded.
    expect(withCity.state?.totalTaxCents).toBe(stateOnly.state?.totalTaxCents);
    expect(withCity.status).toBe('PARTIAL');
    expect(withCity.notes.some((n) => n.includes('NYC'))).toBe(true);
  });
});

describe('California — head of household uses FTB Schedule Z', () => {
  it('has its own HoH table, not the joint table', () => {
    expect(CALIFORNIA_2026.brackets?.head_of_household[0]?.upTo).toBe(22_927);
    expect(CALIFORNIA_2026.brackets?.head_of_household).not.toEqual(
      CALIFORNIA_2026.brackets?.married_filing_jointly,
    );
  });

  it('computes the hand-checked Schedule Z tax at $150k profit', () => {
    // Federal AGI $139,402.83 (SE tax $21,194.33, half-SE $10,597.17) −
    // $11,800 HoH std ded → taxable $127,602.83. Schedule Z lines:
    // 1/2/4/6/8% = $229.27/$627.78/$628.08/$998.16/$1,256.16, plus 9.3% on
    // $25,246.83 over $102,356 = $2,347.96 → $6,087.41. (The retired joint
    // stand-in returned $4,798.47 — it understated HoH tax at this income.)
    const r = calculateTax(150_000, 'US-CA', { filingStatus: 'head_of_household' });
    expect(r.state?.status).toBe('OK');
    expect(r.state?.totalTaxCents).toBe(608_741);
  });
});

describe('New York — head of household uses its own threshold table', () => {
  it('has its own HoH table, not the single table', () => {
    expect(NEW_YORK_2026.brackets?.head_of_household[0]?.upTo).toBe(12_800);
    expect(NEW_YORK_2026.brackets?.head_of_household).not.toEqual(
      NEW_YORK_2026.brackets?.single,
    );
  });

  it('computes the hand-checked HoH-table tax at $150k profit', () => {
    // Federal AGI $139,402.83 − $11,200 HoH std ded → taxable $128,202.83.
    // HoH lines: 3.9/4.4/5.15/5.4% = $499.20/$213.40/$167.38/$4,684.50,
    // plus 5.9% on $20,552.83 over $107,650 = $1,212.62 → $6,777.10. (The
    // retired single-table stand-in returned $6,995.72 — it overstated.)
    const r = calculateTax(150_000, 'US-NY', { filingStatus: 'head_of_household' });
    expect(r.state?.status).toBe('OK');
    expect(r.state?.totalTaxCents).toBe(677_710);
  });
});

describe('California — exemption credit omission is disclosed, not silent', () => {
  const r = calculateTax(80_000, 'US-CA', GOLDEN);

  it('carries the omission notice in the CA workpaper steps', () => {
    const caPaper = r.workpapers.find((w) => w.jurisdiction === 'US-CA');
    const step = caPaper?.steps.find((s) => s.label.includes('Personal exemption credit'));
    expect(step).toBeDefined();
    expect(step?.detail).toContain('NOT modeled');
    expect(step?.detail).toContain('$158');
    expect(step?.detail).toContain('$316');
    expect(step?.detail).toContain('overstate');
  });

  it('carries the omission notice in the CA result notes', () => {
    expect(
      r.state?.notes.some(
        (n) => n.includes('personal exemption credit') && n.includes('NOT modeled'),
      ),
    ).toBe(true);
  });

  it('does not leak the CA omission into other states', () => {
    const ny = calculateTax(80_000, 'US-NY', GOLDEN);
    expect(ny.state?.notes.some((n) => n.includes('exemption credit'))).toBe(false);
    const nyPaper = ny.workpapers.find((w) => w.jurisdiction === 'US-NY');
    expect(nyPaper?.steps.some((s) => s.label.includes('exemption credit'))).toBe(false);
  });
});

describe('no-tax states — a TRUE zero, with the reason in writing', () => {
  it('Texas returns zero state tax with an explanatory note', () => {
    const r = calculateTax(80_000, 'US-TX', GOLDEN);
    expect(r.state?.status).toBe('OK');
    expect(r.state?.totalTaxCents).toBe(0);
    expect(r.state?.notes.some((n) => n.includes('no personal income tax'))).toBe(true);
    expect(r.totalLiabilityCents).toBe(1_664_746); // federal only
    expect(r.status).toBe('OK');
  });

  it.each(['US-FL', 'US-WA', 'US-NV', 'US-SD', 'US-WY', 'US-AK', 'US-TN', 'US-NH'])(
    '%s also returns a true zero',
    (key) => {
      const r = calculateTax(80_000, key, GOLDEN);
      expect(r.state?.status).toBe('OK');
      expect(r.state?.totalTaxCents).toBe(0);
      expect(r.state?.notes.length).toBeGreaterThan(0);
    },
  );

  it('New Hampshire notes its 2025 interest & dividends repeal', () => {
    const r = calculateTax(80_000, 'US-NH', GOLDEN);
    expect(r.state?.notes.some((n) => n.includes('repealed'))).toBe(true);
  });
});

describe('stub states — NOT_IMPLEMENTED, never a fake zero', () => {
  it.each(['US-PA', 'US-IL', 'US-NC', 'US-GA', 'US-AZ'])('%s is an explicit stub', (key) => {
    const r = calculateTax(80_000, key, GOLDEN);
    expect(r.state?.status).toBe('NOT_IMPLEMENTED');
    expect(r.status).toBe('PARTIAL');
    // The total is federal-only and the result SAYS the state is excluded.
    expect(r.totalLiabilityCents).toBe(1_664_746);
    expect(r.breakdown.every((c) => c.jurisdiction === 'US')).toBe(true);
    expect(r.notes.some((n) => n.includes('NOT_IMPLEMENTED'))).toBe(true);
    expect(r.state?.schedule).toHaveLength(0);
  });
});

describe('California — Mental Health Services Tax surtax', () => {
  it('adds 1% on taxable income over $1M', () => {
    // $1.5M profit → federal AGI $1,468,474.87 → CA taxable $1,462,574.87;
    // surtax = 1% × $462,574.87 = $4,625.75.
    const r = calculateTax(1_500_000, 'US-CA', GOLDEN);
    const surtax = r.breakdown.find((c) => c.type === 'state_surtax');
    expect(surtax?.amountCents).toBe(462_575);
  });

  it('does not apply at ordinary incomes', () => {
    const r = calculateTax(80_000, 'US-CA', GOLDEN);
    expect(r.breakdown.some((c) => c.type === 'state_surtax')).toBe(false);
  });
});

describe('state-namespaced profile overrides', () => {
  it('uses profile.state[CA] prior-year figures for the state safe harbor', () => {
    const r = calculateTax(80_000, 'US-CA', {
      filingStatus: 'single',
      state: { CA: { priorYearTax: 2_000, priorYearAGI: 100_000 } },
    });
    expect(r.state?.safeHarbor.basis).toBe('prior_100');
    expect(r.state?.safeHarbor.requiredAnnualPaymentCents).toBe(200_000);
    expect(r.safeHarbor.requiredAnnualPaymentCents).toBe(1_498_271 + 200_000);
  });

  it('never double-counts federal payments against the state schedule', () => {
    const r = calculateTax(80_000, 'US-CA', {
      filingStatus: 'single',
      paymentsAlreadyMade: 10_000,
    });
    // Federal remaining: $14,982.71 − $10,000. State remaining: full
    // $2,493.87 (state payments default to 0, separately namespaced).
    expect(r.schedule.reduce((a, p) => a + p.amountDueCents, 0)).toBe(747_658);
  });
});

describe('New York module — non-NYC locality fallback', () => {
  it('flags any locality segment as NOT_IMPLEMENTED with its name', () => {
    const r = calculateNY(
      {
        netProfitCents: 8_000_000,
        federalAgiCents: 7_434_818,
        profile: { filingStatus: 'single' },
      },
      'ALBANY',
    );
    expect(r.localityStatus).toBe('NOT_IMPLEMENTED');
    expect(r.notes.some((n) => n.includes('ALBANY'))).toBe(true);
  });
});

describe('state engine guards', () => {
  it('progressiveCalculator refuses a "full" config whose tables are missing', () => {
    const broken = { ...CALIFORNIA_2026 };
    delete broken.brackets;
    const calc = progressiveCalculator(broken);
    expect(() => calc(CTX)).toThrow('missing tables');
  });

  it('falls back to the default locality note when a module supplies none', () => {
    const calc = progressiveCalculator(NEW_YORK_2026);
    const r = calc(CTX, 'NYC');
    expect(r.localityStatus).toBe('NOT_IMPLEMENTED');
    expect(r.notes.some((n) => n.includes('Locality tax for NYC is not implemented'))).toBe(true);
  });
});

describe('registry hygiene', () => {
  it('exposes state modules through getStateModule', () => {
    expect(getStateModule('CA')).toBeDefined();
    expect(getStateModule('ZZ')).toBeUndefined();
  });

  it('rejects unknown jurisdictions loudly', () => {
    expect(() => calculateTax(80_000, 'US-ZZ', GOLDEN)).toThrow(UnknownJurisdictionError);
    expect(() => calculateTax(80_000, 'CA', GOLDEN)).toThrow(UnknownJurisdictionError);
    expect(() => calculateTax(80_000, 'US-NY-LA', GOLDEN)).toThrow(UnknownJurisdictionError);
    expect(() => calculateTax(80_000, 'US-XX-YY', GOLDEN)).toThrow(UnknownJurisdictionError);
  });

  it('lists every supported key, including the reserved NYC locality key', () => {
    const keys = listJurisdictions();
    expect(keys).toContain('US');
    expect(keys).toContain('US-CA');
    expect(keys).toContain('US-NY');
    expect(keys).toContain('US-NY-NYC');
    expect(keys).toContain('US-TX');
    expect(keys).toHaveLength(1 + 16 + 1);
  });

  it('is case-insensitive on jurisdiction keys', () => {
    const r = calculateTax(80_000, 'us-ca', GOLDEN);
    expect(r.jurisdiction).toBe('US-CA');
    expect(r.state?.totalTaxCents).toBe(277_097);
  });
});
