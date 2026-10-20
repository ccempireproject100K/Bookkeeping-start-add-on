/**
 * Federal holidays and estimated-tax due dates.
 *
 * Due dates roll forward past weekends AND federal holidays. A weekend-only
 * engine is wrong in the US: the IRS sits in Washington DC, so DC
 * Emancipation Day (Apr 16) is an IRS holiday — when it is observed on
 * Fri Apr 15 or Mon Apr 17 it moves April deadlines. This module carries a
 * real holiday table (fixed dates with weekend-observance shifts, floating
 * holidays, and Emancipation Day) rather than a stub.
 *
 * All dates are ISO `yyyy-mm-dd` strings handled in UTC to keep weekday math
 * timezone-proof.
 */

function iso(year: number, month: number, day: number): string {
  const mm = String(month).padStart(2, '0');
  const dd = String(day).padStart(2, '0');
  return `${year}-${mm}-${dd}`;
}

function parseIso(date: string): { y: number; m: number; d: number } {
  const [y, m, d] = date.split('-').map(Number) as [number, number, number];
  return { y, m, d };
}

/** 0 = Sunday … 6 = Saturday (UTC). */
export function weekdayOf(date: string): number {
  const { y, m, d } = parseIso(date);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay();
}

export function addDays(date: string, days: number): string {
  const { y, m, d } = parseIso(date);
  const t = new Date(Date.UTC(y, m - 1, d) + days * 86_400_000);
  return iso(t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate());
}

/** Day-of-month of the n-th given weekday (e.g. 3rd Monday of January). */
function nthWeekday(year: number, month: number, weekday: number, n: number): string {
  const first = iso(year, month, 1);
  const offset = (weekday - weekdayOf(first) + 7) % 7;
  return iso(year, month, 1 + offset + (n - 1) * 7);
}

/** Day-of-month of the last given weekday of the month. */
function lastWeekday(year: number, month: number, weekday: number): string {
  const nextMonth = month === 12 ? iso(year + 1, 1, 1) : iso(year, month + 1, 1);
  const lastDay = addDays(nextMonth, -1);
  const { d } = parseIso(lastDay);
  const back = (weekdayOf(lastDay) - weekday + 7) % 7;
  return iso(year, month, d - back);
}

const SUNDAY = 0;
const MONDAY = 1;
const THURSDAY = 4;
const SATURDAY = 6;

/**
 * Observed federal closure dates for a year, as a map ISO date → holiday
 * name. Fixed-date holidays falling on Saturday are observed the Friday
 * before; on Sunday, the Monday after (standard federal observance).
 */
export function federalHolidays(year: number): Map<string, string> {
  const holidays = new Map<string, string>();

  const fixed: { month: number; day: number; name: string }[] = [
    { month: 1, day: 1, name: "New Year's Day" },
    { month: 4, day: 16, name: 'DC Emancipation Day' },
    { month: 6, day: 19, name: 'Juneteenth National Independence Day' },
    { month: 7, day: 4, name: 'Independence Day' },
    { month: 11, day: 11, name: 'Veterans Day' },
    { month: 12, day: 25, name: 'Christmas Day' },
  ];
  for (const f of fixed) {
    let date = iso(year, f.month, f.day);
    const wd = weekdayOf(date);
    if (wd === SATURDAY) date = addDays(date, -1);
    else if (wd === SUNDAY) date = addDays(date, 1);
    holidays.set(date, f.name);
  }

  holidays.set(nthWeekday(year, 1, MONDAY, 3), 'Birthday of Martin Luther King, Jr.');
  holidays.set(nthWeekday(year, 2, MONDAY, 3), "Washington's Birthday");
  holidays.set(lastWeekday(year, 5, MONDAY), 'Memorial Day');
  holidays.set(nthWeekday(year, 9, MONDAY, 1), 'Labor Day');
  holidays.set(nthWeekday(year, 10, MONDAY, 2), 'Columbus Day');
  holidays.set(nthWeekday(year, 11, THURSDAY, 4), 'Thanksgiving Day');

  return holidays;
}

export function isBusinessDay(date: string): boolean {
  const wd = weekdayOf(date);
  if (wd === SUNDAY || wd === SATURDAY) return false;
  const { y } = parseIso(date);
  return !federalHolidays(y).has(date);
}

/** Roll a date forward to the next business day (itself if it already is). */
export function rollForwardToBusinessDay(date: string): string {
  let current = date;
  for (let i = 0; i < 10; i++) {
    if (isBusinessDay(current)) return current;
    current = addDays(current, 1);
  }
  throw new Error(`No business day within 10 days of ${date}`);
}

export interface DueDateInfo {
  quarter: number;
  statutoryDate: string;
  dueDate: string;
}

/**
 * The four federal estimated-tax due dates for an estimate (tax) year:
 * Apr 15, Jun 15, Sep 15 of the year and Jan 15 of the FOLLOWING year, each
 * rolled forward past weekends and federal holidays.
 */
export function estimatedTaxDueDates(taxYear: number): DueDateInfo[] {
  const statutory = [
    iso(taxYear, 4, 15),
    iso(taxYear, 6, 15),
    iso(taxYear, 9, 15),
    iso(taxYear + 1, 1, 15),
  ];
  return statutory.map((statutoryDate, i) => ({
    quarter: i + 1,
    statutoryDate,
    dueDate: rollForwardToBusinessDay(statutoryDate),
  }));
}
