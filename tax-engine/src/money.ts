/**
 * Money handling for the tax engine.
 *
 * Rule: money is ALWAYS integer cents inside the engine. Floats are accepted
 * only at the public boundary (dollar inputs) and are converted exactly once
 * via {@link toCents}. Rates are applied with exact rational arithmetic
 * (BigInt), never with float multiplication, so a rate can never smear a
 * fractional cent across steps.
 *
 * Rounding rule (defined, engine-wide): round HALF-UP (away from zero) at the
 * exact points where a tax form would produce a number — each tax component,
 * each bracket line, each deduction. Quarterly amounts are split so the four
 * quarters sum EXACTLY to the annual figure (see schedule.ts).
 */

/** Convert a dollar amount (public input) to integer cents, half-up. */
export function toCents(dollars: number): number {
  if (!Number.isFinite(dollars)) {
    throw new Error(`Money input must be finite, got ${dollars}`);
  }
  // The epsilon nudge counters binary representation error on values like
  // 1.005 dollars, which must round to 101 cents, not 100.
  const sign = dollars < 0 ? -1 : 1;
  return sign * Math.round(Math.abs(dollars) * 100 + 1e-9);
}

/** Convert integer cents back to dollars (display / boundary only). */
export function toDollars(cents: number): number {
  return cents / 100;
}

/**
 * Exact rational multiply-divide with half-up rounding:
 * returns round(cents * num / den) computed in BigInt so it is exact for any
 * realistic magnitude.
 */
export function mulDivRound(cents: number, num: number, den: number): number {
  if (den === 0) throw new Error('mulDivRound: denominator must not be zero');
  const n = BigInt(Math.trunc(cents)) * BigInt(Math.trunc(num));
  const d = BigInt(Math.trunc(den));
  const negative = n < 0n !== d < 0n;
  const absN = n < 0n ? -n : n;
  const absD = d < 0n ? -d : d;
  const q = absN / absD;
  const r = absN % absD;
  const rounded = r * 2n >= absD ? q + 1n : q;
  return Number(negative ? -rounded : rounded);
}

/**
 * Apply a decimal rate (e.g. 0.124, 0.9235) to integer cents, exactly.
 * Rates are converted to a fraction over 1_000_000, which exactly represents
 * every rate used in the 2026 tables (max 4 decimal places).
 */
export function applyRate(cents: number, rate: number): number {
  const num = Math.round(rate * 1_000_000);
  return mulDivRound(cents, num, 1_000_000);
}

/** Half of an amount, rounded half-up (used for the half-SE-tax deduction). */
export function half(cents: number): number {
  return mulDivRound(cents, 1, 2);
}

export function maxCents(...values: number[]): number {
  return values.reduce((a, b) => (b > a ? b : a), values[0] ?? 0);
}

export function minCents(...values: number[]): number {
  return values.reduce((a, b) => (b < a ? b : a), values[0] ?? 0);
}
