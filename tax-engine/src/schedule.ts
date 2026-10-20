import { applyRate } from './money.js';
import type { QuarterlyPayment } from './types.js';
import type { DueDateInfo } from './holidays.js';

/**
 * Build a quarterly payment schedule from a required annual amount.
 *
 * Guarantees: the four amounts are non-negative and sum EXACTLY to
 * `remainingCents`. Each quarter is its configured percentage of the annual
 * amount (rounded half-up); the LAST quarter with a non-zero percentage
 * absorbs the rounding remainder, deterministically. Jurisdictions with an
 * uneven installment pattern (e.g. California 30/40/0/30) therefore still
 * produce an exactly-summing schedule, and a 0% quarter stays exactly $0.
 */
export function buildSchedule(
  remainingCents: number,
  installmentPcts: number[],
  dates: DueDateInfo[],
): QuarterlyPayment[] {
  const remaining = Math.max(0, remainingCents);
  let lastNonZero = -1;
  installmentPcts.forEach((pct, i) => {
    if (pct > 0) lastNonZero = i;
  });

  const amounts = installmentPcts.map((pct, i) => {
    if (pct <= 0 || i === lastNonZero) return 0; // last non-zero set below
    return applyRate(remaining, pct);
  });
  if (lastNonZero >= 0) {
    const assigned = amounts.reduce((a, b) => a + b, 0);
    amounts[lastNonZero] = Math.max(0, remaining - assigned);
  }

  return dates.map((d, i) => ({
    quarter: d.quarter,
    statutoryDate: d.statutoryDate,
    dueDate: d.dueDate,
    amountDueCents: amounts[i] ?? 0,
  }));
}

/** Merge per-jurisdiction schedules into one chronological schedule by due date. */
export function mergeSchedules(
  parts: { jurisdiction: string; schedule: QuarterlyPayment[] }[],
): QuarterlyPayment[] {
  const byDate = new Map<string, QuarterlyPayment>();
  for (const { jurisdiction, schedule } of parts) {
    for (const payment of schedule) {
      const existing = byDate.get(payment.dueDate);
      if (existing) {
        existing.amountDueCents += payment.amountDueCents;
        existing.parts = [
          ...(existing.parts ?? []),
          { jurisdiction, amountDueCents: payment.amountDueCents },
        ];
      } else {
        byDate.set(payment.dueDate, {
          quarter: payment.quarter,
          statutoryDate: payment.statutoryDate,
          dueDate: payment.dueDate,
          amountDueCents: payment.amountDueCents,
          parts: [{ jurisdiction, amountDueCents: payment.amountDueCents }],
        });
      }
    }
  }
  return [...byDate.values()].sort((a, b) => a.dueDate.localeCompare(b.dueDate));
}
