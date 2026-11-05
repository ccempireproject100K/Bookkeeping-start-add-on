/**
 * @empire/pip-door-core — the Pip tray store (DESIGN §5.4).
 *
 * A small framework-agnostic store implementing the tray state machine:
 *
 *   received → sniffing → (needs-answer) → ready → staging → delivered | failed
 *   plus the terminal tray state `unsupported`.
 *
 * Rules enforced here (DESIGN §5.3/§5.4/§5.6):
 *  - Entries are METADATA ONLY (names, kinds, routes, states, links, evidence).
 *    File blobs/bytes are refused — the tray never persists file content.
 *  - Every entry is tenant-namespaced; tenantId is required.
 *  - wipe(tenantId) clears a tenant's entries (logout / tenant switch).
 *  - NO timers and NO faked progress: states change only when the host
 *    dispatches an explicit adapter/sniffer event.
 *  - Subscribers are notified of every change (UI announcements / a11y).
 */

/** @type {readonly string[]} */
export const TRAY_STATES = Object.freeze([
  'received',
  'sniffing',
  'needs-answer',
  'ready',
  'staging',
  'delivered',
  'failed',
  'unsupported',
]);

/**
 * Adapter/sniffer event types the host may dispatch.
 * @type {Readonly<Record<string, string>>}
 */
export const TRAY_EVENTS = Object.freeze({
  SNIFF_START: 'SNIFF_START',
  SNIFF_NEEDS_ANSWER: 'SNIFF_NEEDS_ANSWER',
  SNIFF_READY: 'SNIFF_READY',
  MARK_UNSUPPORTED: 'MARK_UNSUPPORTED',
  STAGE_START: 'STAGE_START',
  DELIVER: 'DELIVER',
  FAIL: 'FAIL',
  RETRY: 'RETRY',
});

/** Event → target state. */
const EVENT_TARGET = Object.freeze({
  SNIFF_START: 'sniffing',
  SNIFF_NEEDS_ANSWER: 'needs-answer',
  SNIFF_READY: 'ready',
  MARK_UNSUPPORTED: 'unsupported',
  STAGE_START: 'staging',
  DELIVER: 'delivered',
  FAIL: 'failed',
  RETRY: 'received',
});

/** Legal transitions, keyed by current state → set of reachable states. */
const LEGAL = Object.freeze({
  received: new Set(['sniffing', 'unsupported', 'failed']),
  sniffing: new Set(['needs-answer', 'ready', 'unsupported', 'failed']),
  'needs-answer': new Set(['ready', 'failed']),
  ready: new Set(['staging', 'failed']),
  staging: new Set(['delivered', 'failed']),
  delivered: new Set([]),
  failed: new Set(['received']),
  unsupported: new Set([]),
});

/** Fields the tray is allowed to hold. Anything else is rejected. */
const ALLOWED_FIELDS = new Set([
  'id',
  'tenantId',
  'name',
  'kind',
  'route',
  'link',
  'state',
  'evidence',
  'questionId',
  'error',
]);

/** Field names that would imply blob persistence — refused outright. */
const BLOB_FIELDS = ['blob', 'bytes', 'file', 'data', 'dataUrl', 'base64', 'buffer', 'content'];

/**
 * @typedef {Object} TrayEntry
 * @property {string} id
 * @property {string} tenantId
 * @property {string} name            File name (metadata only).
 * @property {string} [kind]          Sniffed kind, once known.
 * @property {string} [route]         Chosen adapter id, once known.
 * @property {string} [link]          Deep link to the canonical destination record.
 * @property {string} state           One of TRAY_STATES.
 * @property {string[]} [evidence]    Human-readable classification evidence.
 * @property {string} [questionId]    Pending clarification question id, if any.
 * @property {string} [error]         Destination's own honest error text, on failure.
 */

/**
 * Create an empty tray store.
 *
 * @returns {{
 *   add: (entry: Omit<TrayEntry, 'id'|'state'> & { id?: string, state?: string }) => TrayEntry,
 *   get: (tenantId: string, id: string) => TrayEntry | undefined,
 *   list: (tenantId: string) => TrayEntry[],
 *   dispatch: (tenantId: string, id: string, event: { type: string, patch?: Partial<TrayEntry> }) => TrayEntry,
 *   wipe: (tenantId: string) => number,
 *   subscribe: (listener: (note: { action: string, tenantId: string, entry?: TrayEntry }) => void) => () => void,
 * }}
 */
export function createTrayStore() {
  /** @type {Map<string, Map<string, TrayEntry>>} tenantId -> id -> entry */
  const byTenant = new Map();
  /** @type {Set<(note: object) => void>} */
  const listeners = new Set();
  let counter = 0;

  function notify(action, tenantId, entry) {
    const note = { action, tenantId, entry: entry ? { ...entry } : undefined };
    for (const listener of [...listeners]) listener(note);
  }

  function bucket(tenantId) {
    let b = byTenant.get(tenantId);
    if (!b) {
      b = new Map();
      byTenant.set(tenantId, b);
    }
    return b;
  }

  /**
   * Add a new entry in the `received` state. Metadata only; tenantId required.
   */
  function add(entry) {
    if (!entry || typeof entry !== 'object') {
      throw new TypeError('tray.add: entry object is required');
    }
    for (const field of BLOB_FIELDS) {
      if (field in entry) {
        throw new TypeError(
          `tray.add: "${field}" is not allowed — the tray stores metadata only, never file content`,
        );
      }
    }
    for (const key of Object.keys(entry)) {
      if (!ALLOWED_FIELDS.has(key)) {
        throw new TypeError(`tray.add: unknown field "${key}" — the tray stores metadata only`);
      }
    }
    if (!entry.tenantId || typeof entry.tenantId !== 'string') {
      throw new TypeError('tray.add: tenantId (string) is required — tray entries are tenant-namespaced');
    }
    if (!entry.name || typeof entry.name !== 'string') {
      throw new TypeError('tray.add: name (string) is required');
    }
    const id = entry.id || `pip-${++counter}`;
    const b = bucket(entry.tenantId);
    if (b.has(id)) throw new Error(`tray.add: duplicate entry id "${id}" for this tenant`);
    /** @type {TrayEntry} */
    const stored = { ...entry, id, state: 'received' };
    b.set(id, stored);
    notify('added', entry.tenantId, stored);
    return { ...stored };
  }

  function get(tenantId, id) {
    const found = byTenant.get(tenantId)?.get(id);
    return found ? { ...found } : undefined;
  }

  /** List ONLY the given tenant's entries — other tenants are invisible. */
  function list(tenantId) {
    const b = byTenant.get(tenantId);
    return b ? [...b.values()].map((e) => ({ ...e })) : [];
  }

  /**
   * Advance an entry via an explicit adapter/sniffer event. There is no other
   * way to change state — no timers, no implicit progress.
   *
   * @param {{ type: string, patch?: Partial<TrayEntry> }} event
   */
  function dispatch(tenantId, id, event) {
    const b = byTenant.get(tenantId);
    const current = b?.get(id);
    if (!current) {
      throw new Error(`tray.dispatch: no entry "${id}" for this tenant`);
    }
    const target = EVENT_TARGET[event?.type];
    if (!target) {
      throw new Error(`tray.dispatch: unknown event type "${event?.type}"`);
    }
    if (!LEGAL[current.state].has(target)) {
      throw new Error(
        `tray.dispatch: illegal transition ${current.state} → ${target} (event ${event.type})`,
      );
    }
    const patch = event.patch || {};
    for (const field of BLOB_FIELDS) {
      if (field in patch) {
        throw new TypeError(`tray.dispatch: patch may not carry "${field}" — metadata only`);
      }
    }
    const next = { ...current, ...patch, id: current.id, tenantId: current.tenantId, state: target };
    if (target !== 'needs-answer') delete next.questionId;
    b.set(id, next);
    notify('transition', tenantId, next);
    return { ...next };
  }

  /** Remove every entry belonging to a tenant (logout / tenant switch). Returns the count removed. */
  function wipe(tenantId) {
    const b = byTenant.get(tenantId);
    const count = b ? b.size : 0;
    byTenant.delete(tenantId);
    if (count > 0) notify('wiped', tenantId, undefined);
    return count;
  }

  /** Subscribe to tray changes (for UI announcements). Returns an unsubscribe function. */
  function subscribe(listener) {
    if (typeof listener !== 'function') throw new TypeError('tray.subscribe: listener must be a function');
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return { add, get, list, dispatch, wipe, subscribe };
}
