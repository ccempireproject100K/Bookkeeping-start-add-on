import { NEW_YORK_2026 } from '../config/newyork-2026.js';
import { progressiveCalculator } from './engine.js';

export const config = NEW_YORK_2026;
export const calculate = progressiveCalculator(NEW_YORK_2026, {
  localityNote: (locality) =>
    locality === 'NYC'
      ? 'New York City resident income tax (3.078%–3.876%) is NOT included — the NYC locality module is reserved in the registry design but not implemented. State figures only.'
      : `Locality tax for ${locality} is not implemented and is NOT included in these figures.`,
});
