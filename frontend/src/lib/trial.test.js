/**
 * Trial-door helpers — pins the cancel-reason set verbatim (it must
 * match the backend's CANCEL_REASONS exactly; the reasons are
 * recorded verbatim for owner reporting) and the exact-cents terms
 * rendering (Light 1899 -> first month 949 after the 950 discount).
 */
import { CANCEL_REASONS, DOORS, money, termsLine, trialStateLabel } from './trial';

test('cancel reasons are the owner set, verbatim and in order', () => {
  expect(CANCEL_REASONS).toEqual([
    'Not ready yet',
    'Too expensive',
    'Missing a feature',
    'Switching to another tool',
    'Just testing it out',
  ]);
});

test('two doors, and only the card door carries the extras', () => {
  expect(DOORS.map((d) => d.id)).toEqual(['nocard', 'card']);
  const nocard = DOORS[0].points.join(' ');
  const card = DOORS[1].points.join(' ');
  expect(nocard).toContain('cleaned free');
  expect(nocard).not.toContain('tax estimate');
  expect(card).toContain('tax estimate');
  expect(card).toContain('half off');
  expect(card).toContain('referral');
});

test('termsLine renders exact cents for both paths', () => {
  expect(termsLine({
    list_cents: 1899, discount_percent: 50, discount_cents: 950,
    first_month_cents: 949, month_two_cents: 1899,
  })).toBe('First month $9.49 (50% off $18.99), then $18.99/mo');
  expect(termsLine({
    list_cents: 3999, discount_percent: 0, discount_cents: 0,
    first_month_cents: 3999, month_two_cents: 3999,
  })).toBe('$39.99/mo from the first month');
  expect(money(2000)).toBe('$20.00');
});

test('trial state labels are plain', () => {
  expect(trialStateLabel('expired')).toBe('Trial ended — not converted');
  expect(trialStateLabel('trialing')).toBe('Trial running');
});
