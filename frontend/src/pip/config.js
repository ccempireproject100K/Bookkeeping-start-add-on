/**
 * Pip File Door — host configuration for Bookkeeping (DESIGN §5.1, §5.6).
 *
 * Feature flags first: `pip_door.enabled` is the kill switch. Turning it
 * off removes the shell dropzone entirely while leaving every existing
 * page door (the Receipts page's own picker/drop box) untouched — rollback
 * is "turn it off", never a migration (DESIGN §5.6).
 *
 * Phase 1 scope: exactly ONE adapter is enabled — books.receipts. CSV
 * forks are Phase 2 and the backup danger card UI is Phase 3; both are
 * declared here as disabled so the widening, when it comes, happens in
 * this one file.
 */

export const PIP_DOOR_FLAGS = Object.freeze({
  // Master switch for the shell door in this product.
  'pip_door.enabled': true,
  // Per-adapter flags (DESIGN §5.6). Only books.receipts is live in Phase 1.
  'pip_door.adapters.books_receipts': true,
  'pip_door.adapters.books_importer': false, // Phase 2
  'pip_door.adapters.books_banking': false, // Phase 2
  'pip_door.adapters.books_dataclean': false, // Phase 2 (signpost only, forever)
  'pip_door.adapters.books_backup': false, // Phase 3 (danger link only, forever)
  // AI fallback classification (DESIGN §3.2 step 4): OFF — Phases 0–2 ship
  // fully deterministic, and enabling it is a per-product owner call (§6.3).
  'pip_door.ai_fallback': false,
});

export function pipFlag(name) {
  return PIP_DOOR_FLAGS[name] === true;
}

/** localStorage keys for Pip's per-user settings (host-scoped, §5.6). */
export const PIP_SETTING_KEYS = Object.freeze({
  speed: 'pip-speed',
  paused: 'pip-paused',
  buttonMode: 'pip-button-mode',
});

export const RECEIPTS_ADAPTER_ID = 'books.receipts';
