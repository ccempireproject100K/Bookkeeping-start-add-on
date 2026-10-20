import type { JurisdictionConfig } from './types.js';
import { FEDERAL_2026 } from './federal-2026.js';
import { CALIFORNIA_2026 } from './california-2026.js';
import { NEW_YORK_2026 } from './newyork-2026.js';
import { OTHER_STATES_2026 } from './other-states-2026.js';

export { FEDERAL_2026, CALIFORNIA_2026, NEW_YORK_2026, OTHER_STATES_2026 };

/** Every jurisdiction config the engine knows about, for validation. */
export const ALL_CONFIGS: JurisdictionConfig[] = [
  FEDERAL_2026,
  CALIFORNIA_2026,
  NEW_YORK_2026,
  ...OTHER_STATES_2026,
];
