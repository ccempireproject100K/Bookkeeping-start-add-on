import { OTHER_STATES_2026 } from '../config/other-states-2026.js';
import { stubCalculator } from './engine.js';
import type { StateConfig } from '../config/types.js';

const cfg = OTHER_STATES_2026.find((c) => c.stateCode === 'PA');
if (!cfg) throw new Error('Missing PA config in OTHER_STATES_2026');
export const config: StateConfig = cfg;
export const calculate = stubCalculator(cfg);
