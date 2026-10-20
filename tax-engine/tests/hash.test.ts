import { describe, expect, it } from 'vitest';
import { configHash } from '../src/hash.js';
import { FEDERAL_2026 } from '../src/config/federal-2026.js';

describe('configHash — workpaper reproducibility', () => {
  it('is deterministic for the same config', () => {
    expect(configHash(FEDERAL_2026)).toBe(configHash(FEDERAL_2026));
    expect(configHash(FEDERAL_2026)).toMatch(/^[0-9a-f]{64}$/);
  });

  it('is independent of object key order', () => {
    const a = { x: 1, nested: { p: 'a', q: [1, 2, 3] } };
    const b = { nested: { q: [1, 2, 3], p: 'a' }, x: 1 };
    expect(configHash(a)).toBe(configHash(b));
  });

  it('changes when any figure changes (a rate edit cannot hide)', () => {
    const tweaked = { ...FEDERAL_2026, version: '2026.9.9' };
    expect(configHash(tweaked)).not.toBe(configHash(FEDERAL_2026));
  });

  it('treats array order as significant (bracket order matters)', () => {
    expect(configHash({ v: [1, 2] })).not.toBe(configHash({ v: [2, 1] }));
  });
});
