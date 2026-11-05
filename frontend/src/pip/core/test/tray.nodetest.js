import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createTrayStore, TRAY_EVENTS } from '../src/index.js';

const addReceipt = (store, tenantId, extra = {}) =>
  store.add({ tenantId, name: 'receipt.png', kind: 'image', ...extra });

test('happy path: received → sniffing → ready → staging → delivered', () => {
  const store = createTrayStore();
  const e = addReceipt(store, 'tenant-a');
  assert.equal(e.state, 'received');
  store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.SNIFF_START });
  const ready = store.dispatch('tenant-a', e.id, {
    type: TRAY_EVENTS.SNIFF_READY,
    patch: { route: 'books.receipts' },
  });
  assert.equal(ready.state, 'ready');
  assert.equal(ready.route, 'books.receipts');
  store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.STAGE_START });
  const delivered = store.dispatch('tenant-a', e.id, {
    type: TRAY_EVENTS.DELIVER,
    patch: { link: '/receipts/rcpt_123' },
  });
  assert.equal(delivered.state, 'delivered');
  assert.equal(delivered.link, '/receipts/rcpt_123');
});

test('question path: sniffing → needs-answer → ready', () => {
  const store = createTrayStore();
  const e = addReceipt(store, 'tenant-a');
  store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.SNIFF_START });
  const asked = store.dispatch('tenant-a', e.id, {
    type: TRAY_EVENTS.SNIFF_NEEDS_ANSWER,
    patch: { questionId: 'pdf.purpose' },
  });
  assert.equal(asked.state, 'needs-answer');
  assert.equal(asked.questionId, 'pdf.purpose');
  const ready = store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.SNIFF_READY, patch: { route: 'aioffice.kb' } });
  assert.equal(ready.state, 'ready');
  assert.equal(ready.questionId, undefined, 'question id clears once answered');
});

test('unsupported is reachable and terminal', () => {
  const store = createTrayStore();
  const e = store.add({ tenantId: 'tenant-a', name: 'setup.exe' });
  const un = store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.MARK_UNSUPPORTED });
  assert.equal(un.state, 'unsupported');
  assert.throws(
    () => store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.RETRY }),
    /illegal transition/,
  );
});

test('illegal transitions throw — no skipping states, no timer-faked progress', () => {
  const store = createTrayStore();
  const e = addReceipt(store, 'tenant-a');
  assert.throws(() => store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.DELIVER }), /illegal transition/);
  assert.throws(() => store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.STAGE_START }), /illegal transition/);
  assert.throws(() => store.dispatch('tenant-a', e.id, { type: 'TELEPORT' }), /unknown event type/);
  assert.equal(store.get('tenant-a', e.id).state, 'received', 'failed dispatches leave state untouched');
});

test('failed entries can retry back to received; delivered cannot move', () => {
  const store = createTrayStore();
  const e = addReceipt(store, 'tenant-a');
  store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.SNIFF_START });
  const failed = store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.FAIL, patch: { error: 'network down' } });
  assert.equal(failed.error, 'network down');
  const retry = store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.RETRY });
  assert.equal(retry.state, 'received');
});

test('tenant isolation: tenant A entries are invisible to tenant B', () => {
  const store = createTrayStore();
  const a = addReceipt(store, 'tenant-a');
  addReceipt(store, 'tenant-b', { name: 'other.png' });
  assert.equal(store.list('tenant-a').length, 1);
  assert.equal(store.list('tenant-b').length, 1);
  assert.equal(store.get('tenant-b', a.id), undefined, "A's id must not resolve under B");
  assert.throws(() => store.dispatch('tenant-b', a.id, { type: TRAY_EVENTS.SNIFF_START }), /no entry/);
});

test('wipe(tenantId) clears only that tenant (logout / tenant switch)', () => {
  const store = createTrayStore();
  addReceipt(store, 'tenant-a');
  addReceipt(store, 'tenant-a', { name: 'second.png' });
  addReceipt(store, 'tenant-b');
  const removed = store.wipe('tenant-a');
  assert.equal(removed, 2);
  assert.equal(store.list('tenant-a').length, 0);
  assert.equal(store.list('tenant-b').length, 1, "B's entries survive A's wipe");
});

test('tenantId and metadata-only are enforced', () => {
  const store = createTrayStore();
  assert.throws(() => store.add({ name: 'x.png' }), /tenantId/);
  assert.throws(() => store.add({ tenantId: 't', name: 'x.png', blob: {} }), /metadata only/);
  assert.throws(() => store.add({ tenantId: 't', name: 'x.png', bytes: new Uint8Array(2) }), /metadata only/);
  assert.throws(() => store.add({ tenantId: 't', name: 'x.png', secretField: 1 }), /unknown field/);
});

test('subscribers are notified of adds, transitions, and wipes (UI announcements)', () => {
  const store = createTrayStore();
  const notes = [];
  const unsubscribe = store.subscribe((n) => notes.push(n));
  const e = addReceipt(store, 'tenant-a');
  store.dispatch('tenant-a', e.id, { type: TRAY_EVENTS.SNIFF_START });
  store.wipe('tenant-a');
  assert.deepEqual(notes.map((n) => n.action), ['added', 'transition', 'wiped']);
  unsubscribe();
  addReceipt(store, 'tenant-a');
  assert.equal(notes.length, 3, 'no notifications after unsubscribe');
});

test('duplicate ids within a tenant are rejected; same id across tenants is fine', () => {
  const store = createTrayStore();
  store.add({ tenantId: 'tenant-a', id: 'fixed', name: 'a.png' });
  assert.throws(() => store.add({ tenantId: 'tenant-a', id: 'fixed', name: 'b.png' }), /duplicate/);
  assert.doesNotThrow(() => store.add({ tenantId: 'tenant-b', id: 'fixed', name: 'b.png' }));
});
