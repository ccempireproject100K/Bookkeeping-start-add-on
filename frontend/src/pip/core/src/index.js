/**
 * @empire/pip-door-core — Phase 0 public surface.
 *
 * Phase 0 proves the classifier + tray skeleton BEFORE any product routing
 * exists: no adapters are live, no uploads happen, nothing is wired into
 * Bookkeeping or AI Office. See README.md and ../DESIGN.md §5–§6.
 */

export { sniffFile } from './sniff.js';
export { createTrayStore, TRAY_STATES, TRAY_EVENTS } from './tray.js';
export { DEFAULT_LIMITS, PIP_DOOR_CONFIG_EXAMPLE, MB } from './config.js';
