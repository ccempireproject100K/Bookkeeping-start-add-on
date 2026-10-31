/**
 * WelcomeGift page — the onboarding gift surface. The API layer is
 * mocked; the gift endpoints themselves (session auth, engine reuse,
 * honesty paths, deliveries) are covered by
 * backend/tests/test_welcome_gift.py. What these tests pin is the
 * page's contract: it posts the file to /welcome-gift/clean on the
 * tenant's own session (no DataClean key anywhere), shows a
 * NOT_IMPLEMENTED note verbatim on a PARTIAL estimate, and renders an
 * honest unavailable panel — never a zero — when the engine isn't
 * built.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '@/lib/i18n';
import { api } from '@/lib/api';
import WelcomeGift from './WelcomeGift';

jest.mock('@/lib/api', () => ({
  api: { post: jest.fn(), get: jest.fn(), downloadCsv: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail?.message || 'Request failed',
}));

jest.mock('sonner', () => ({
  toast: { error: jest.fn(), success: jest.fn() },
}));

const META = {
  gifts: {
    clean: { done: false, deliveries: 0, last_delivered_at: null },
    estimate: { done: false, deliveries: 0, last_delivered_at: null },
  },
  kinds: ['customers', 'items', 'vendors'],
  engine_states: ['CA', 'NY', 'PA'],
  stub_states: ['PA'],
  filing_statuses: ['single', 'married_filing_jointly',
    'married_filing_separately', 'head_of_household'],
};

const CLEAN_OUT = {
  gift: 'clean', kind: 'customers', rows: 3,
  records: [
    { name: 'Acme Supply Inc', email: 'ops@acme.example', phone: '5125550134', address: '9 Oak Ave' },
  ],
  changes: [{ row: 2, field: 'phone', rule: 'phone_us_format', before: 'x', after: 'y' }],
  changes_by_rule: { phone_us_format: 1 },
  dedupe: {
    engine: 'deterministic', engine_note: null, scanned: 3, truncated: false,
    pairs: [{
      a: 'row-1', b: 'row-2', score: 0.97, records: [
        { id: 'row-1', name: 'Acme Supply Inc' }, { id: 'row-2', name: 'ACME SUPPLY INC' }],
      evidence: ['exact email match: ops@acme.example'],
    }],
  },
  delivery: { id: 'd1', gift: 'clean' },
};

const STUB_NOTE = 'State tax for US-PA is NOT_IMPLEMENTED and is EXCLUDED from the total below — the total is federal only.';

const ESTIMATE_PARTIAL = {
  gift: 'estimate', delivery_id: 'd9', status: 'PARTIAL',
  requested_jurisdiction: 'US-PA', computed_jurisdiction: 'US-PA',
  total_liability_cents: 900000,
  result: {
    status: 'PARTIAL', totalLiabilityCents: 900000, notes: [STUB_NOTE],
    breakdown: [{ jurisdiction: 'US', type: 'se_tax', label: 'Self-employment tax', amountCents: 900000 }],
    schedule: [{ quarter: 1, dueDate: '2026-04-15', amountDueCents: 225000 }],
    safeHarbor: { federal: { requiredAnnualPaymentCents: 810000, basis: 'current_90' } },
    workpapers: [],
  },
};

beforeEach(() => {
  jest.clearAllMocks();
  api.get.mockResolvedValue(META);
});

const renderPage = () => render(
  <MemoryRouter>
    <WelcomeGift />
  </MemoryRouter>
);

test('clean gift: posts the file on the tenant session and offers both downloads', async () => {
  api.post.mockResolvedValue(CLEAN_OUT);
  renderPage();
  fireEvent.click(screen.getByTestId('gift-clean-sample'));
  fireEvent.click(screen.getByTestId('gift-clean-run'));
  await screen.findByTestId('gift-clean-result');
  expect(api.post).toHaveBeenCalledWith('/welcome-gift/clean', {
    kind: 'customers', csv_text: expect.stringContaining('Acme Supply Inc'),
  });
  expect(screen.getByTestId('gift-clean-download')).toBeTruthy();
  expect(screen.getByTestId('gift-report-download')).toBeTruthy();
  expect(screen.getByTestId('gift-clean-pairs').textContent).toContain('exact email match');
});

test('estimate gift: a stub state warns before the run and the NOTE shows verbatim after', async () => {
  api.post.mockResolvedValue(ESTIMATE_PARTIAL);
  renderPage();
  fireEvent.change(screen.getByTestId('gift-est-net-profit'), { target: { value: '50000' } });
  fireEvent.change(screen.getByTestId('gift-est-state'), { target: { value: 'PA' } });
  // The stub list arrives with the /welcome-gift meta fetch.
  await screen.findByTestId('gift-est-stub-hint');
  fireEvent.click(screen.getByTestId('gift-est-run'));
  await screen.findByTestId('gift-est-result');
  expect(api.post).toHaveBeenCalledWith('/welcome-gift/estimate', {
    net_profit: 50000, filing_status: 'single', state: 'PA',
  });
  expect(screen.getByTestId('gift-est-notes').textContent).toContain(STUB_NOTE);
  expect(screen.getByTestId('gift-est-total').textContent).toContain('$9,000.00');
  expect(screen.getByTestId('gift-est-download-pdf')).toBeTruthy();
});

test('estimate gift: locked on the no-card door, with the door switch offered', async () => {
  api.get.mockResolvedValue({ ...META, estimate_unlocked: false });
  api.post.mockResolvedValue({});
  renderPage();
  await screen.findByTestId('gift-est-locked');
  // The form is not shown; the clean card is untouched (ungated).
  expect(screen.queryByTestId('gift-est-run')).toBeNull();
  expect(screen.getByTestId('gift-clean-run')).toBeTruthy();
  fireEvent.click(screen.getByTestId('gift-est-switch-door'));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/trial/door', { door: 'card' }));
});

test('estimate gift: engine_not_built renders the unavailable panel, never a number', async () => {
  api.post.mockRejectedValue({
    response: {
      status: 503,
      data: { detail: { code: 'engine_not_built', message: 'engine missing' } },
    },
  });
  renderPage();
  fireEvent.change(screen.getByTestId('gift-est-net-profit'), { target: { value: '50000' } });
  fireEvent.click(screen.getByTestId('gift-est-run'));
  await screen.findByTestId('gift-est-unavailable');
  expect(screen.queryByTestId('gift-est-result')).toBeNull();
  expect(screen.queryByTestId('gift-est-total')).toBeNull();
});
