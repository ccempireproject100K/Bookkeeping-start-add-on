/**
 * Trial doors — frontend helpers (the backend machine is lib/trial.py
 * + routers/trial.py; this mirrors its vocabulary for rendering).
 *
 * The split, stated plainly everywhere it appears:
 *  - Everyone (both doors, even before any choice): the DataClean
 *    welcome gift — one file cleaned free, yours to keep.
 *  - Card door only: the tax-estimate welcome gift, 50% off the first
 *    paid month, and referral access from day one.
 */

// The one-tap cancel reasons — the owner's set, verbatim, mirroring
// backend lib/trial.py CANCEL_REASONS. Cancellation never requires
// one; there are no dark patterns.
export const CANCEL_REASONS = [
  'Not ready yet',
  'Too expensive',
  'Missing a feature',
  'Switching to another tool',
  'Just testing it out',
];

export const DOORS = [
  {
    id: 'nocard',
    title: 'Free trial, no card',
    points: [
      '14 days free with the full product',
      'Your first file cleaned free during setup (the DataClean gift)',
      'No card, nothing auto-charges — the trial just ends',
    ],
  },
  {
    id: 'card',
    title: 'Free trial with card',
    points: [
      'Everything in the no-card trial',
      'Plus your first quarterly tax estimate in writing',
      'Plus half off your first paid month',
      'Plus referral access from day one',
      'Your card auto-converts when the trial ends — cancel any time before then and you are never charged',
    ],
  },
];

export const money = (cents) =>
  '$' + ((Number(cents) || 0) / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function trialStateLabel(state) {
  switch (state) {
    case 'trialing': return 'Trial running';
    case 'converted': return 'Subscribed';
    case 'expired': return 'Trial ended — not converted';
    case 'canceled': return 'Trial canceled';
    default: return 'No trial on record';
  }
}

/** The price line for a plan's terms object from GET /trial. */
export function termsLine(terms) {
  if (!terms) return '';
  if (terms.discount_percent > 0) {
    return `First month ${money(terms.first_month_cents)} (${terms.discount_percent}% off ${money(terms.list_cents)}), then ${money(terms.month_two_cents)}/mo`;
  }
  return `${money(terms.list_cents)}/mo from the first month`;
}
