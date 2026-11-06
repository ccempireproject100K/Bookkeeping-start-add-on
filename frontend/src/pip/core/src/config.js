/**
 * @empire/pip-door-core — configuration shapes and the Phase-1 adapter example.
 *
 * Phase 0 ships NO live adapters. The example below only documents how the two
 * Phase-1 product adapters (books.receipts, aioffice.photo_estimates) will be
 * declared by their host products. Both are `enabled: false` placeholders.
 */

export const MB = 1024 * 1024;

/**
 * Default per-destination size ceilings, mirroring DESIGN §1 (verified limits):
 *  - books receipts:        10 MB (server-side MIME allow-list + size cap)
 *  - aioffice onboarding:    5 MB
 *  - aioffice knowledge:    10 MB
 *  - aioffice photo est.:   10 MB
 *  - aioffice video est.:  100 MB (signpost only — never a Pip drop target)
 *
 * @type {Readonly<Record<string, number>>}
 */
export const DEFAULT_LIMITS = Object.freeze({
  'books.receipts': 10 * MB,
  'books.importer': 5 * MB,
  'books.banking': 5 * MB,
  'books.dataclean': 5 * MB,
  'aioffice.onboarding': 5 * MB,
  'aioffice.kb': 10 * MB,
  'aioffice.photo_estimates': 10 * MB,
  'aioffice.video_estimates': 100 * MB,
});

/**
 * @typedef {Object} AdapterPlaceholder
 * @property {string} id            Stable adapter id, e.g. "books.receipts".
 * @property {boolean} enabled      Phase 0: always false. Hosts flip per-adapter flags in Phase 1+.
 * @property {number} phase         The build phase that activates this adapter.
 * @property {string} transport     The destination page's OWN transport ("multipart" | "csv_text-json" | "deep-link" | "danger-link").
 * @property {string} endpoint      The existing endpoint / surface the adapter mirrors (Phase 0: documentation only).
 * @property {number} [maxBytes]    Destination's own size ceiling (mirrored; the server stays authoritative).
 * @property {string} [note]        Honest-scope note.
 */

/**
 * Example host configuration for the two products' Phase-1 slice.
 * Adapters are DISABLED placeholders: Phase 0 performs no routing and no uploads.
 *
 * @type {{ products: Record<string, { product: string, adapters: AdapterPlaceholder[] }>, flags: Record<string, boolean> }}
 */
export const PIP_DOOR_CONFIG_EXAMPLE = Object.freeze({
  products: {
    books: {
      product: 'books',
      adapters: [
        {
          id: 'books.receipts',
          enabled: false,
          phase: 1,
          transport: 'multipart',
          endpoint: 'POST /receipts/upload',
          maxBytes: 10 * MB,
          note: 'Phase 1. One multipart request per file, exactly as the Receipt Inbox page sends today. Pip never posts the expense.',
        },
      ],
    },
    aioffice: {
      product: 'aioffice',
      adapters: [
        {
          id: 'aioffice.photo_estimates',
          enabled: false,
          phase: 1,
          transport: 'multipart',
          endpoint: 'POST /photo-estimates/upload (+ /{id}/analyze on explicit user action)',
          maxBytes: 10 * MB,
          note: 'Phase 1. Upload mirrors the estimate page; Analyze stays an explicit tray action because it spends vision money. Approval stays on the estimate screen.',
        },
      ],
    },
  },
  flags: {
    'pip_door.enabled': false,
    'pip_door.ai_fallback': false,
  },
});
