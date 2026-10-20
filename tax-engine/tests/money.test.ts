import { describe, expect, it } from 'vitest';
import { applyRate, half, mulDivRound, toCents, toDollars } from '../src/money.js';

describe('money primitives', () => {
  it('converts dollars to integer cents at the boundary', () => {
    expect(toCents(80_000)).toBe(8_000_000);
    expect(toCents(11_303.64)).toBe(1_130_364);
    expect(toCents(0.1 + 0.2)).toBe(30); // the classic float trap, neutralized
    expect(toCents(-5)).toBe(-500);
  });

  it('converts back to dollars only for display', () => {
    expect(toDollars(1_130_364)).toBe(11_303.64);
  });

  it('mulDivRound rounds half-up', () => {
    expect(mulDivRound(101, 1, 2)).toBe(51); // 50.5 → 51
    expect(mulDivRound(100, 1, 2)).toBe(50);
    expect(mulDivRound(99, 1, 2)).toBe(50); // 49.5 → 50
    expect(mulDivRound(98, 1, 2)).toBe(49);
    expect(mulDivRound(0, 153, 1000)).toBe(0);
  });

  it('applyRate uses exact rational arithmetic (no float drift)', () => {
    // 92.35% of $80,000 = $73,880.00 exactly
    expect(applyRate(8_000_000, 0.9235)).toBe(7_388_000);
    // 15.3% combined would be $11,304.00 on the unrounded base; the engine
    // splits SS/Medicare instead — see golden test for the exact split.
    expect(applyRate(7_388_000, 0.124)).toBe(916_112);
    expect(applyRate(7_388_000, 0.029)).toBe(214_252);
  });

  it('half rounds half-up', () => {
    expect(half(101)).toBe(51);
    expect(half(100)).toBe(50);
  });

  it('rejects non-finite money and zero denominators', () => {
    expect(() => toCents(Number.NaN)).toThrow();
    expect(() => toCents(Number.POSITIVE_INFINITY)).toThrow();
    expect(() => mulDivRound(100, 1, 0)).toThrow();
  });

  it('rounds halves away from zero for negative amounts', () => {
    expect(mulDivRound(-101, 1, 2)).toBe(-51);
    expect(mulDivRound(101, -1, 2)).toBe(-51);
    expect(mulDivRound(-100, 1, 2)).toBe(-50);
  });
});
