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
    // AGI $74,348.18 − $5,900 std ded → 1/2/4/6/8% lines = $2,770.97,
    // less the $158 personal exemption credit (modeled since config
    // 2026.3.0) = $2,612.97. (Pre-credit golden was $2,770.97 — the
    // credit is the ONLY change; see the exemption-credit suite below.)
    expect(r.state?.status).toBe('OK');
    expect(r.state?.totalTaxCents).toBe(261_297);
  });

  it('carries the QBI before/after fields on federal only, never on the state result', () => {
    expect(r.federal.incomeTaxBeforeQbiCents).toBe(752_660);
    expect(r.federal.incomeTaxCents).toBe(534_382);
    expect(r.state?.incomeTaxBeforeQbiCents).toBeUndefined();
    expect(r.state?.qbiDeductionCents).toBeUndefined();
    expect(r.state?.incomeTaxCents).toBeUndefined();
  });

  it('combines federal + state in the total and breakdown', () => {
    expect(r.totalLiabilityCents).toBe(1_664_746 + 261_297);
    expect(r.breakdown.some((c) => c.jurisdiction === 'US-CA' && c.type === 'state_income_tax')).toBe(true);
    expect(r.workpapers).toHaveLength(2);
  });

  it('uses the CA 30/40/0/30 installment pattern — Q3 combined is federal-only', () => {
    // CA safe harbor = 90% of the post-credit tax: 90% × $2,612.97 = $2,351.67.
    expect(r.safeHarbor.requiredAnnualPaymentCents).toBe(1_498_271 + 235_167);
    expect(r.schedule).toHaveLength(4);
    const q3 = r.schedule[2];
    expect(q3?.parts?.find((p) => p.jurisdiction === 'US-CA')?.amountDueCents).toBe(0);
    expect(q3?.amountDueCents).toBe(374_568);
    expect(r.schedule.reduce((a, p) => a + p.amountDueCents, 0)).toBe(1_733_438);
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
    // $25,246.83 over $102,356 = $2,347.96 → $6,087.41, less the $158
    // personal exemption credit = $5,929.41. (The retired joint stand-in
    // returned $4,798.47 — it understated HoH tax at this income.)
    const r = calculateTax(150_000, 'US-CA', { filingStatus: 'head_of_household' });
    expect(r.state?.status).toBe('OK');
    expect(r.state?.totalTaxCents).toBe(592_941);
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

describe('California — personal exemption credit is MODELED (config 2026.3.0)', () => {
  // Built 2026-11-01 (gap sweep S3). Before this build the credit was a
  // documented omission and the golden CA tax on this profile was
  // $2,770.97; the ONLY change is the credit: −$158.00 → $2,612.97.
  const r = calculateTax(80_000, 'US-CA', GOLDEN);

  it('appears in the breakdown as a negative credit component', () => {
    const credit = r.breakdown.find((c) => c.type === 'state_exemption_credit');
    expect(credit?.jurisdiction).toBe('US-CA');
    expect(credit?.amountCents).toBe(-15_800); // −$158.00 single
    // Bracket tax component stays gross; credit + tax = the state total.
    const tax = r.breakdown.find((c) => c.type === 'state_income_tax');
    expect(tax?.amountCents).toBe(277_097);
    expect((tax?.amountCents ?? 0) + (credit?.amountCents ?? 0)).toBe(r.state?.totalTaxCents);
  });

  it('is traced in the CA workpaper with its amount', () => {
    const caPaper = r.workpapers.find((w) => w.jurisdiction === 'US-CA');
    const step = caPaper?.steps.find((s) => s.label === 'Personal exemption credit');
    expect(step).toBeDefined();
    expect(step?.amountCents).toBe(-15_800);
    expect(step?.detail).toContain('$158');
    expect(step?.detail).toContain('no phaseout applies');
    // Config discipline: the workpaper records the bumped config version.
    expect(caPaper?.configVersion).toBe('2026.3.0');
  });

  it('gives joint filers two exemptions — $316', () => {
    // Hand-checked: AGI $74,348.18 − $11,800 std ded → taxable $62,548.18;
    // Schedule Y lines 1/2/4% = $229.12/$628.04/$329.37 = $1,186.53,
    // less $316 credit = $870.53.
    const mfj = calculateTax(80_000, 'US-CA', { filingStatus: 'married_filing_jointly' });
    expect(mfj.breakdown.find((c) => c.type === 'state_exemption_credit')?.amountCents).toBe(-31_600);
    expect(mfj.state?.totalTaxCents).toBe(87_053);
  });

  it('phases out by $6 per whole $2,500 of federal AGI over $260,778 (single)', () => {
    // $300k profit → federal AGI $284,543.77 → excess $23,765.77 →
    // ceil(23,765.77 / 2,500) = 10 steps × $6 = $60 → credit $158 − $60 = $98.
    const hi = calculateTax(300_000, 'US-CA', GOLDEN);
    expect(hi.breakdown.find((c) => c.type === 'state_exemption_credit')?.amountCents).toBe(-9_800);
    const step = hi.state?.workpaper.steps.find((s) => s.label === 'Personal exemption credit');
    expect(step?.detail).toContain('10 whole $2,500 step(s)');
  });

  it('uses $1,250 phaseout steps for married filing separately', () => {
    // Same AGI as the single case → ceil(23,765.77 / 1,250) = 20 steps
    // × $6 = $120 → credit $158 − $120 = $38.
    const mfs = calculateTax(300_000, 'US-CA', { filingStatus: 'married_filing_separately' });
    expect(mfs.breakdown.find((c) => c.type === 'state_exemption_credit')?.amountCents).toBe(-3_800);
  });

  it('is nonrefundable — it cannot take the tax below $0', () => {
    const tiny = calculateTax(1_000, 'US-CA', GOLDEN);
    expect(tiny.state?.totalTaxCents).toBe(0);
    expect(tiny.breakdown.some((c) => c.type === 'state_exemption_credit')).toBe(false);
  });

  it('never offsets the Mental Health Services Tax, and is fully phased out at $1.5M', () => {
    // Federal AGI $1,468,474.87 → 484 phaseout steps × $6 = $2,904 > $158,
    // so the credit is $0 and the pre-credit state total is unchanged;
    // the 1% MHST on taxable over $1M stands in full.
    const big = calculateTax(1_500_000, 'US-CA', GOLDEN);
    expect(big.breakdown.some((c) => c.type === 'state_exemption_credit')).toBe(false);
    expect(big.breakdown.find((c) => c.type === 'state_surtax')?.amountCents).toBe(462_575);
    expect(big.state?.totalTaxCents).toBe(16_470_753);
  });

  it('does not leak the CA credit into other states', () => {
    const ny = calculateTax(80_000, 'US-NY', GOLDEN);
    expect(ny.state?.notes.some((n) => n.includes('exemption credit'))).toBe(false);
    const nyPaper = ny.workpapers.find((w) => w.jurisdiction === 'US-NY');
    expect(nyPaper?.steps.some((s) => s.label.includes('exemption credit'))).toBe(false);
    expect(ny.breakdown.some((c) => c.type === 'state_exemption_credit')).toBe(false);
  });
});

describe('California — dependent exemption credit omission is disclosed, not silent', () => {
  const r = calculateTax(80_000, 'US-CA', GOLDEN);

  it('carries the remaining omission notice in the CA workpaper steps', () => {
    const caPaper = r.workpapers.find((w) => w.jurisdiction === 'US-CA');
    const step = caPaper?.steps.find((s) => s.label.includes('Dependent exemption credit'));
    expect(step).toBeDefined();
    expect(step?.detail).toContain('NOT modeled');
    expect(step?.detail).toContain('$491');
    expect(step?.detail).toContain('overstate');
  });

  it('carries the remaining omission notice in the CA result notes', () => {
    expect(
      r.state?.notes.some(
        (n) => n.includes('dependent exemption credit') && n.includes('NOT modeled'),
      ),
    ).toBe(true);
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
    // $2,351.67 (state payments default to 0, separately namespaced).
    expect(r.schedule.reduce((a, p) => a + p.amountDueCents, 0)).toBe(733_438);
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
    expect(r.state?.totalTaxCents).toBe(261_297);
  });
});
