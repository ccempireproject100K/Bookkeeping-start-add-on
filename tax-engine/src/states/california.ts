import { CALIFORNIA_2026 } from '../config/california-2026.js';
import { progressiveCalculator } from './engine.js';

export const config = CALIFORNIA_2026;
export const calculate = progressiveCalculator(CALIFORNIA_2026);
