import { applyRate, toCents } from './money.js';
import type { Bracket } from './config/types.js';

export interface BracketLine {
  rate: number;
  fromCents: number;
  /** Exclusive upper bound in cents, or null for the open-ended top bracket. */
  toCents: number | null;
  taxableCents: number;
  taxCents: number;
}

export interface BracketTaxResult {
  totalCents: number;
  lines: BracketLine[];
}

/**
 * Progressive bracket tax. Each bracket line is rounded half-up to the cent
 * (a defined rounding point) and the total is the sum of the lines, so the
 * workpapers always add up exactly to the reported tax.
 */
export function computeBracketTax(taxableCents: number, brackets: Bracket[]): BracketTaxResult {
  const lines: BracketLine[] = [];
  let totalCents = 0;
  let lowerCents = 0;
  const income = Math.max(0, taxableCents);

  for (const bracket of brackets) {
    const upperCents = bracket.upTo === null ? null : toCents(bracket.upTo);
    const inBracket =
      upperCents === null
        ? Math.max(0, income - lowerCents)
        : Math.max(0, Math.min(income, upperCents) - lowerCents);
    const taxCents = inBracket > 0 ? applyRate(inBracket, bracket.rate) : 0;
    lines.push({
      rate: bracket.rate,
      fromCents: lowerCents,
      toCents: upperCents,
      taxableCents: inBracket,
      taxCents,
    });
    totalCents += taxCents;
    if (upperCents === null) break;
    lowerCents = upperCents;
  }

  return { totalCents, lines };
}
