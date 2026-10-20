import { describe, expect, it } from 'vitest';
import { validateJurisdictions } from '../src/validation.js';
import { ALL_CONFIGS } from '../src/config/index.js';
import { FEDERAL_2026 } from '../src/config/federal-2026.js';
import { CALIFORNIA_2026 } from '../src/config/california-2026.js';
import { NEW_YORK_2026 } from '../src/config/newyork-2026.js';
import { OTHER_STATES_2026 } from '../src/config/other-states-2026.js';

describe('validateJurisdictions — freshness', () => {
  it('stays quiet on fresh data (verified 2026-10-20, checked the same day)', () => {
    const health = validateJurisdictions({ asOf: '2026-10-20' });
    expect(health.length).toBe(ALL_CONFIGS.length);
    expect(health.every((h) => h.status === 'ok')).toBe(true);
    expect(health.every((h) => h.issues.length === 0)).toBe(true);
  });

  it('still quiet at exactly 90 days', () => {
    const health = validateJurisdictions({ asOf: '2027-01-18' }); // 90 days after 2026-10-20
    expect(health.every((h) => h.status === 'ok')).toBe(true);
  });

  it('fires on stale data at 91+ days', () => {
    const health = validateJurisdictions({ asOf: '2027-01-19' });
    expect(health.every((h) => h.status === 'stale')).toBe(true);
    expect(health[0]?.ageInDays).toBe(91);
    expect(health[0]?.issues[0]).toContain('days old');
  });

  it('reports age in days', () => {
    const health = validateJurisdictions({ asOf: '2027-06-01' });
    expect(health[0]?.ageInDays).toBe(224);
  });

  it('reports the provisional flag in the health list without failing the config', () => {
    const health = validateJurisdictions({ asOf: '2026-10-20' });
    const ca = health.find((h) => h.jurisdiction === 'US-CA');
    const ny = health.find((h) => h.jurisdiction === 'US-NY');
    const us = health.find((h) => h.jurisdiction === 'US');
    // CA and NY were verified against FTB / NY DTF sources on 2026-10-20 —
    // their provisional flags are lifted.
    expect(ca?.provisional).toBe(false);
    expect(ny?.provisional).toBe(false);
    expect(us?.provisional).toBe(false);
    // A config still marked provisional is flagged in the list, not failed.
    const synth = validateJurisdictions({
      asOf: '2026-10-20',
      configs: [{ ...NEW_YORK_2026, provisional: true }],
    });
    expect(synth[0]?.provisional).toBe(true);
    expect(synth[0]?.status).toBe('ok');
  });
});

describe('validateJurisdictions — schema checks', () => {
  it('rejects a bracket table whose ceilings are not ascending', () => {
    const broken = {
      ...NEW_YORK_2026,
      brackets: {
        ...NEW_YORK_2026.brackets,
        single: NEW_YORK_2026.brackets.single.map((b, i) =>
          i === 1 ? { ...b, upTo: 100 } : b,
        ),
      },
    };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.some((i) => i.includes('not strictly above'))).toBe(true);
  });

  it('rejects a rate outside (0, 1)', () => {
    const broken = {
      ...NEW_YORK_2026,
      brackets: {
        ...NEW_YORK_2026.brackets,
        single: NEW_YORK_2026.brackets.single.map((b, i) =>
          i === 0 ? { ...b, rate: 1.5 } : b,
        ),
      },
    };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.some((i) => i.includes('outside (0, 1)'))).toBe(true);
  });

  it('rejects a stub that smuggles in a rate table', () => {
    const pa = OTHER_STATES_2026.find((c) => c.stateCode === 'PA');
    if (!pa) throw new Error('PA config missing');
    const smuggled = { ...pa, brackets: NEW_YORK_2026.brackets };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [smuggled] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.some((i) => i.includes('masquerade'))).toBe(true);
  });

  it('rejects an unparseable lastVerified date', () => {
    const broken = { ...NEW_YORK_2026, lastVerified: 'not-a-date' };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.ageInDays).toBeNull();
  });

  it('rejects installment percentages that do not sum to 1 (federal)', () => {
    const broken = { ...FEDERAL_2026, installmentPcts: [0.25, 0.25, 0.25, 0.24] };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.some((i) => i.includes('sum to'))).toBe(true);
  });

  it('rejects an empty source note', () => {
    const broken = { ...FEDERAL_2026, source: '' };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
  });

  it('rejects a non-positive Social Security wage base', () => {
    const broken = {
      ...FEDERAL_2026,
      se: { ...FEDERAL_2026.se, socialSecurityWageBase: 0 },
    };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.some((i) => i.includes('wage base'))).toBe(true);
  });

  it('rejects an empty bracket table', () => {
    const broken = {
      ...NEW_YORK_2026,
      brackets: { ...NEW_YORK_2026.brackets, single: [] },
    };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.some((i) => i.includes('empty'))).toBe(true);
  });

  it('rejects a closed top bracket and regressive rates', () => {
    const closed = {
      ...NEW_YORK_2026,
      brackets: {
        ...NEW_YORK_2026.brackets,
        single: NEW_YORK_2026.brackets.single.map((b, i, arr) =>
          i === arr.length - 1 ? { ...b, upTo: 99_000_000 } : b,
        ),
      },
    };
    const h1 = validateJurisdictions({ asOf: '2026-10-20', configs: [closed] });
    expect(h1[0]?.issues.some((i) => i.includes('open-ended'))).toBe(true);

    const regressive = {
      ...NEW_YORK_2026,
      brackets: {
        ...NEW_YORK_2026.brackets,
        single: NEW_YORK_2026.brackets.single.map((b, i) =>
          i === 2 ? { ...b, rate: 0.01 } : b,
        ),
      },
    };
    const h2 = validateJurisdictions({ asOf: '2026-10-20', configs: [regressive] });
    expect(h2[0]?.issues.some((i) => i.includes('lower than the previous rate'))).toBe(true);
  });

  it('rejects a no-tax config missing its explanatory note', () => {
    const txCfg = OTHER_STATES_2026.find((c) => c.stateCode === 'TX');
    if (!txCfg) throw new Error('TX config missing');
    const broken = { ...txCfg };
    delete broken.noTaxNote;
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.some((i) => i.includes('explanatory note'))).toBe(true);
  });

  it('rejects a "full" state config whose tables are missing', () => {
    const broken = { ...NEW_YORK_2026 };
    delete broken.brackets;
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.some((i) => i.includes('missing tables'))).toBe(true);
  });

  it('rejects an out-of-range surtax and a negative standard deduction', () => {
    if (!CALIFORNIA_2026.surtax) throw new Error('CA surtax missing');
    const badSurtax = {
      ...CALIFORNIA_2026,
      surtax: { ...CALIFORNIA_2026.surtax, threshold: -5 },
    };
    const h1 = validateJurisdictions({ asOf: '2026-10-20', configs: [badSurtax] });
    expect(h1[0]?.issues.some((i) => i.includes('surtax'))).toBe(true);

    const badDeduction = {
      ...FEDERAL_2026,
      standardDeduction: { ...FEDERAL_2026.standardDeduction, single: -1 },
    };
    const h2 = validateJurisdictions({ asOf: '2026-10-20', configs: [badDeduction] });
    expect(h2[0]?.issues.some((i) => i.includes('standard deduction'))).toBe(true);
  });

  it('rejects an empty config version and an implausible tax year', () => {
    const broken = { ...NEW_YORK_2026, version: '', taxYear: 1999 };
    const health = validateJurisdictions({ asOf: '2026-10-20', configs: [broken] });
    expect(health[0]?.status).toBe('invalid');
    expect(health[0]?.issues.length).toBeGreaterThanOrEqual(2);
  });

  it('runs with default options (all registered configs, today as the as-of)', () => {
    const health = validateJurisdictions();
    expect(health).toHaveLength(ALL_CONFIGS.length);
  });
});
