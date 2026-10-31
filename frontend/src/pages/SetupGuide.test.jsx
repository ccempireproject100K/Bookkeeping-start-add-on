/**
 * SetupGuide page — pins the honesty surfaces: without an AI key the
 * page renders the unavailable state (and the bear in its error
 * state), and with one it renders the conversation, the character in
 * the state the backend reports, and the per-step inputs.
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '@/lib/i18n';
import { api } from '@/lib/api';
import SetupGuide from './SetupGuide';

jest.mock('@/lib/api', () => ({
  api: { post: jest.fn(), get: jest.fn(), downloadCsv: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail?.message || 'Request failed',
}));

jest.mock('sonner', () => ({
  toast: { error: jest.fn(), success: jest.fn() },
}));

const renderPage = () => render(
  <MemoryRouter>
    <SetupGuide />
  </MemoryRouter>
);

test('AI off: the honest unavailable state, checklist untouched', async () => {
  api.get.mockResolvedValue({ ai_configured: false, character: { skin: 'bear' } });
  renderPage();
  await screen.findByTestId('guide-unavailable');
  expect(api.post).not.toHaveBeenCalled();
  const ch = screen.getByTestId('guide-character');
  expect(ch.getAttribute('data-state')).toBe('error');
  expect(ch.getAttribute('data-skin')).toBe('bear');
});

test('AI on: the interview opens, answers post, character follows the backend state', async () => {
  api.get.mockResolvedValue({ ai_configured: true, character: { skin: 'bear' } });
  api.post
    .mockResolvedValueOnce({
      session_id: 's1', step: 'business_name', flagged: false, done: false,
      message: "Hi — I'm your setup guide. First: What's your business called?",
      character_state: 'talking', progress: { done: 0, total: 9, percent: 0 },
    })
    .mockResolvedValueOnce({
      session_id: 's1', step: 'trade', flagged: false, done: false,
      message: 'What kind of work do you do?',
      character_state: 'talking', progress: { done: 1, total: 9, percent: 11 },
    });
  renderPage();
  // The start response lands asynchronously; wait for its progress.
  await screen.findByTestId('guide-progress');
  expect(screen.getByTestId('guide-progress').textContent).toContain('0');
  fireEvent.change(screen.getByTestId('guide-text-input'), { target: { value: 'Maple HVAC' } });
  fireEvent.click(screen.getByTestId('guide-send'));
  await screen.findByText('What kind of work do you do?');
  expect(api.post).toHaveBeenLastCalledWith('/guide/answer',
    { session_id: 's1', text: 'Maple HVAC' });
  expect(screen.getByTestId('guide-character').getAttribute('data-state')).toBe('talking');
});
