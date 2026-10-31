/**
 * Welcome Gift helpers (lib/welcomeGift.js) — the pure logic under
 * the gift page. What these pin is the honesty surface: a PARTIAL
 * estimate stays visibly partial with the engine's notes, totals are
 * formatted from the engine's cents (never recomputed), and the
 * engine_not_built error code survives to the page.
 */
import {
  giftRecordsToCsv, duplicateReportText, estimateSummary, estimateBody,
  giftErrorCode, giftErrorMessage,
} from './welcomeGift';

const RESULT_OK = {
  status: 'OK',
  totalLiabilityCents: 1664746,
  notes: [],
  breakdown: [
    { jurisdiction: 'US', type: 'se_tax', label: 'Self-employment tax', amountCents: 1130364 },
    { jurisdiction: 'US-CA', type: 'state_income_tax', label: 'California income tax', amountCents: 100000 },
  ],
  schedule: [{ quarter: 1, dueDate: '2026-04-15', amountDueCents: 416187 }],
  safeHarbor: { federal: { requiredAnnualPaymentCents: 1017328, basis: 'current_90' } },
  workpapers: [{
    jurisdiction: 'US', configVersion: 'v2026.2.0', configHash: 'abcdef0123456789',
    provisional: false,
    steps: [{ label: 'Net earnings', detail: '92.35% of net profit', amountCents: 7388000 }],
  }],
};

test('giftRecordsToCsv: kind fields, quoting, nulls', () => {
  const csv = giftRecordsToCsv([
    { name: 'Acme, Inc', email: 'a@x', phone: '512', address: null, extra: 'ignored' },
  ], 'customers');
  expect(csv.split('\n')[0]).toBe('name,email,phone,address');
  expect(csv).toContain('"Acme, Inc",a@x,512,');
  expect(csv).not.toContain('ignored');
});

test('duplicateReportText: pairs and the empty case', () => {
  const txt = duplicateReportText(
    [{ score: 0.97, evidence: ['exact email match'], records: [{ name: 'A' }, { name: 'B' }] }],
    'customers');
  expect(txt).toContain('1 likely duplicate pair(s)');
  expect(txt).toContain('A  <>  B');
  expect(duplicateReportText([], 'vendors')).toContain('No likely duplicates found.');
});

test('estimateSummary: OK result flattens with formatted money', () => {
  const s = estimateSummary(RESULT_OK);
  expect(s.partial).toBe(false);
  expect(s.total).toBe('$16,647.46');
  expect(s.breakdown[0].amount).toBe('$11,303.64');
  expect(s.schedule[0]).toEqual({ quarter: 1, dueDate: '2026-04-15', amount: '$4,161.87' });
  expect(s.safeHarbor.amount).toBe('$10,173.28');
  expect(s.workpapers[0].config).toContain('v2026.2.0');
  expect(s.workpapers[0].steps[0].amount).toBe('$73,880.00');
});

test('estimateSummary: PARTIAL keeps the NOT_IMPLEMENTED note front and centre', () => {
  const note = 'State tax for US-PA is NOT_IMPLEMENTED and is EXCLUDED from the total below';
  const s = estimateSummary({ ...RESULT_OK, status: 'PARTIAL', notes: [note] });
  expect(s.partial).toBe(true);
  expect(s.notes).toEqual([note]);
});

test('estimateBody: dollars pass through, blanks are omitted', () => {
  expect(estimateBody({
    netProfit: '80000', filingStatus: 'single', state: 'ca', w2Wages: '', paymentsMade: '500',
  })).toEqual({
    net_profit: 80000, filing_status: 'single', state: 'CA', payments_already_made: 500,
  });
  expect(estimateBody({ netProfit: 0, filingStatus: 'single', state: '' }))
    .toEqual({ net_profit: 0, filing_status: 'single', state: undefined });
});

test('giftErrorCode / giftErrorMessage read the API error shape', () => {
  const err = { response: { status: 503, data: { detail: { code: 'engine_not_built', message: 'engine missing' } } } };
  expect(giftErrorCode(err)).toBe('engine_not_built');
  expect(giftErrorMessage(err, 'fallback')).toBe('engine missing');
  expect(giftErrorCode(new Error('x'))).toBeNull();
  expect(giftErrorMessage(new Error('x'), 'fallback')).toBe('fallback');
});
