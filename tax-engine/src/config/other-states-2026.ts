import type { StateConfig } from './types.js';

/**
 * Config records for the remaining registry states — tax year 2026.
 *
 * No-tax states carry an explanatory note and produce a TRUE zero-liability
 * result: zero is the legally correct answer there. Stub states carry no
 * rate table at all and produce an explicit NOT_IMPLEMENTED status — a stub
 * must never masquerade as a zero.
 */

function noTax(
  stateCode: string,
  note: string,
  source: string,
): StateConfig {
  return {
    jurisdiction: `US-${stateCode}`,
    kind: 'state',
    stateCode,
    implementation: 'no-tax',
    taxYear: 2026,
    version: '2026.1.0',
    source,
    lastVerified: '2026-10-20',
    provisional: false,
    noTaxNote: note,
  };
}

function stub(stateCode: string, reason: string): StateConfig {
  return {
    jurisdiction: `US-${stateCode}`,
    kind: 'state',
    stateCode,
    implementation: 'stub',
    taxYear: 2026,
    version: '2026.1.0',
    source: 'Module status record — no rate table has been implemented or verified yet.',
    lastVerified: '2026-10-20',
    provisional: false,
    stubReason: reason,
  };
}

export const OTHER_STATES_2026: StateConfig[] = [
  noTax(
    'TX',
    'Texas imposes no personal income tax. State liability is genuinely $0.',
    'Texas Constitution art. VIII §24 / Tax Code — no personal income tax (settled law).',
  ),
  noTax(
    'FL',
    'Florida imposes no personal income tax. State liability is genuinely $0.',
    'Florida Constitution art. VII §5 — no personal income tax (settled law).',
  ),
  noTax(
    'WA',
    'Washington imposes no tax on earned or self-employment income. (Its 7% excise tax applies to large long-term capital gains only, which is outside this estimator\u2019s scope.) State liability on SE income is genuinely $0.',
    'RCW 82.87 (capital-gains excise tax only); no Washington personal income tax (settled law).',
  ),
  noTax(
    'NV',
    'Nevada imposes no personal income tax. State liability is genuinely $0.',
    'Nevada Constitution art. 10 §1(9) — no personal income tax (settled law).',
  ),
  noTax(
    'SD',
    'South Dakota imposes no personal income tax. State liability is genuinely $0.',
    'SDCL Title 10-43 covers corporate/financial institutions; no individual income tax (settled law).',
  ),
  noTax(
    'WY',
    'Wyoming imposes no personal income tax. State liability is genuinely $0.',
    'Wyoming Statutes Title 39 — no individual income tax (settled law).',
  ),
  noTax(
    'AK',
    'Alaska imposes no personal income tax. State liability is genuinely $0.',
    'AS 43.20 covers corporations only; no individual income tax (settled law).',
  ),
  noTax(
    'TN',
    'Tennessee does not tax wages or self-employment income. Its former Hall income tax reached only interest and dividends and was fully repealed effective January 1, 2021. State liability on SE income is genuinely $0.',
    'Tenn. Code Ann. §67-2-101 et seq.; Hall tax repeal completed tax year 2021 (settled law).',
  ),
  noTax(
    'NH',
    'New Hampshire does not tax wages or self-employment income. Its former interest-and-dividends tax was fully repealed effective January 1, 2025. State liability on SE income is genuinely $0.',
    'RSA 77 (repealed effective 2025); no New Hampshire tax on earned/SE income (settled law).',
  ),
  stub('PA', 'Pennsylvania module not implemented yet. PA levies a flat 3.07% personal income tax with its own net-income classes and no standard deduction; it needs a dedicated module rather than the generic progressive engine.'),
  stub('IL', 'Illinois module not implemented yet. IL levies a flat 4.95% income tax with a per-person exemption allowance; scheduled for a data-only addition once its 2026 figures are verified.'),
  stub('NC', 'North Carolina module not implemented yet. NC levies a flat individual rate (4.25% for 2026, scheduled to fall further) with a standard deduction; scheduled for a data-only addition once verified.'),
  stub('GA', 'Georgia module not implemented yet. GA has moved to a flat rate structure (5.39% for 2025, stepping down by statute); scheduled for a data-only addition once the 2026 rate is verified.'),
  stub('AZ', 'Arizona module not implemented yet. AZ levies a flat 2.5% income tax conforming closely to federal AGI; scheduled for a data-only addition once verified.'),
];
