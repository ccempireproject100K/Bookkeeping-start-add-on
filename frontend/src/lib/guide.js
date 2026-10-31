/**
 * AI expert guide — presentation-layer config and panel logic.
 *
 * The character is a SWAPPABLE SKIN over the conversation (owner
 * ruling 2026-10-31): bookkeeping's guide is a NEW character — a
 * full-body bear in a vest and pants, wearing glasses — on the shared
 * empire mascot rig (the same state list as Malcolm's: idle, walk,
 * errand-run, talking, celebrate, error). This module is the slot
 * spec: swap the skin's artwork here when the owner's final art
 * lands; nothing else in the guide changes. NO final artwork ships
 * in this build — `placeholder: true` renders a labeled silhouette.
 */
export const GUIDE_CHARACTER = {
  slot: 'bookkeeping-guide',
  skin: 'bear',
  name: 'The Bookkeeping Bear',
  description:
    'Full-body bear, vest and pants, glasses. Placeholder silhouette ' +
    'until the owner rules on final artwork.',
  placeholder: true,
  states: ['idle', 'walk', 'errand-run', 'talking', 'celebrate', 'error'],
};

/** Character state -> the CSS animation class the rig applies. */
export const STATE_CLASSES = {
  idle: 'gc-idle',
  walk: 'gc-walk',
  'errand-run': 'gc-errand-run',
  talking: 'gc-talking',
  celebrate: 'gc-celebrate',
  error: 'gc-error',
};

export function stateClass(state) {
  return STATE_CLASSES[state] || STATE_CLASSES.idle;
}

/** Which input the panel shows for a script step. */
export function inputKindForStep(step) {
  if (step === 'clean_gift') return 'file';
  if (step === 'estimate_gift') return 'estimate';
  if (!step) return 'none';
  return 'text';
}

/** Build the /guide/answer body for the panel's current input. */
export function answerBody(sessionId, step, input) {
  const kind = inputKindForStep(step);
  if (kind === 'file') {
    if (input.skip) return { session_id: sessionId, text: 'skip' };
    return {
      session_id: sessionId,
      payload: { kind: input.kind || 'customers', csv_text: input.csvText || '' },
    };
  }
  if (kind === 'estimate') {
    return {
      session_id: sessionId,
      payload: {
        net_profit: Number(input.netProfit || 0),
        filing_status: input.filingStatus || 'single',
        w2_wages: input.w2Wages === '' || input.w2Wages === undefined
          ? undefined : Number(input.w2Wages),
      },
    };
  }
  return { session_id: sessionId, text: input.text || '' };
}
