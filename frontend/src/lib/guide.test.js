/**
 * Guide presentation logic — pins the character slot spec (the bear:
 * vest, pants, glasses; the empire rig's six states; placeholder
 * until the owner rules on art) and the panel's per-step input
 * routing (the clean step sends a file payload, the estimate step a
 * structured payload, everything else plain text).
 */
import { GUIDE_CHARACTER, stateClass, inputKindForStep, answerBody } from './guide';

test('character slot: the bear, on the rig states, art still placeholder', () => {
  expect(GUIDE_CHARACTER.skin).toBe('bear');
  expect(GUIDE_CHARACTER.description).toContain('vest');
  expect(GUIDE_CHARACTER.description).toContain('glasses');
  expect(GUIDE_CHARACTER.states).toEqual(
    ['idle', 'walk', 'errand-run', 'talking', 'celebrate', 'error']);
  expect(GUIDE_CHARACTER.placeholder).toBe(true);
});

test('stateClass maps every rig state and falls back to idle', () => {
  for (const s of GUIDE_CHARACTER.states) {
    expect(stateClass(s)).toBe(`gc-${s}`);
  }
  expect(stateClass('bogus')).toBe('gc-idle');
  expect(stateClass(undefined)).toBe('gc-idle');
});

test('input routing per step', () => {
  expect(inputKindForStep('clean_gift')).toBe('file');
  expect(inputKindForStep('estimate_gift')).toBe('estimate');
  expect(inputKindForStep('business_name')).toBe('text');
  expect(inputKindForStep(null)).toBe('none');
});

test('answerBody shapes: text, file payload, estimate payload, skip', () => {
  expect(answerBody('s1', 'trade', { text: 'HVAC' }))
    .toEqual({ session_id: 's1', text: 'HVAC' });
  expect(answerBody('s1', 'clean_gift', { kind: 'vendors', csvText: 'a,b' }))
    .toEqual({ session_id: 's1', payload: { kind: 'vendors', csv_text: 'a,b' } });
  expect(answerBody('s1', 'clean_gift', { skip: true }))
    .toEqual({ session_id: 's1', text: 'skip' });
  expect(answerBody('s1', 'estimate_gift', { netProfit: '80000', filingStatus: 'single', w2Wages: '' }))
    .toEqual({ session_id: 's1', payload: { net_profit: 80000, filing_status: 'single', w2_wages: undefined } });
});
