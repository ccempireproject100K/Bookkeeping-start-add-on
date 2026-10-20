import { describe, expect, it } from 'vitest';
import {
  estimatedTaxDueDates,
  federalHolidays,
  isBusinessDay,
} from '../src/holidays.js';

const dueDates = (year: number) => estimatedTaxDueDates(year).map((d) => d.dueDate);
const weekday = (d: string) => new Date(`${d}T00:00:00Z`).getUTCDay();

describe('estimated tax due dates', () => {
  it('2026 estimates: Apr 15 (Wed), Jun 15, Sep 15, Jan 15 2027 (Fri) — all unadjusted', () => {
    expect(dueDates(2026)).toEqual(['2026-04-15', '2026-06-15', '2026-09-15', '2027-01-15']);
    expect(weekday('2026-04-15')).toBe(3); // Wednesday
    expect(weekday('2027-01-15')).toBe(5); // Friday
    // Statutory dates are preserved alongside the rolled dates.
    expect(estimatedTaxDueDates(2026).map((d) => d.statutoryDate)).toEqual([
      '2026-04-15',
      '2026-06-15',
      '2026-09-15',
      '2027-01-15',
    ]);
  });

  it('2028 Q1 rolls past the weekend AND DC Emancipation Day to Tue Apr 18', () => {
    // Apr 15 2028 is a Saturday; Emancipation Day (Apr 16, Sunday) is
    // observed Monday Apr 17, which pushes DC/federal deadlines to Apr 18.
    expect(estimatedTaxDueDates(2028)[0]?.dueDate).toBe('2028-04-18');
  });

  it('2028 Q4 rolls past the weekend AND MLK Day to Tue Jan 18 2028', () => {
    // Jan 15 2028 is a Saturday; Mon Jan 17 2028 is MLK Day.
    expect(estimatedTaxDueDates(2027)[3]?.dueDate).toBe('2028-01-18');
  });

  it('Jan 15 2029 is MLK Day itself, so Q4 2028 estimates roll to Jan 16', () => {
    expect(estimatedTaxDueDates(2028)[3]?.dueDate).toBe('2029-01-16');
  });

  it('every due date 2026–2032 lands on a business day', () => {
    for (let year = 2026; year <= 2032; year++) {
      for (const d of dueDates(year)) {
        expect(isBusinessDay(d), `${d} should be a business day`).toBe(true);
      }
    }
  });
});

describe('federal holiday table', () => {
  it('includes Emancipation Day and observes it on Friday when Apr 16 is a Saturday', () => {
    const h2022 = federalHolidays(2022); // Apr 16 2022 was a Saturday
    expect(h2022.get('2022-04-15')).toBe('DC Emancipation Day');
  });

  it('observes fixed holidays that land on Saturday on the preceding Friday', () => {
    const h2026 = federalHolidays(2026); // Jul 4 2026 is a Saturday
    expect(h2026.get('2026-07-03')).toBe('Independence Day');
  });

  it('Juneteenth: Jun 19 2026 is a Friday — a holiday, not a business day', () => {
    expect(isBusinessDay('2026-06-19')).toBe(false);
    expect(isBusinessDay('2026-06-18')).toBe(true);
  });

  it('weekends are not business days', () => {
    expect(isBusinessDay('2026-04-18')).toBe(false); // Saturday
    expect(isBusinessDay('2026-04-19')).toBe(false); // Sunday
    expect(isBusinessDay('2026-04-20')).toBe(true); // Monday
  });
});
