# Pip File Door — provenance (Phase 1, Bookkeeping receipts)

Phase 1 of the Pip File Door (design: `~/workspace/buildout/pip-file-door/DESIGN.md`,
owner go-ahead 2026-11-05). Everything under `src/pip/` is original code for
this repo except the vendored core noted below.

## Vendored core — `@empire/pip-door-core` (in-house, Phase 0)

`src/pip/core/` is a **byte-identical copy** of the Phase-0 package built and
proven standalone (`~/workspace/buildout/pip-file-door/phase0/`, 22/22 node
tests, classifier 35/35 on its fixture corpus):

| Vendored path | Source (Phase 0) | Byte-identical? |
|---|---|---|
| `core/src/sniff.js` | `phase0/src/sniff.js` | yes |
| `core/src/tray.js` | `phase0/src/tray.js` | yes |
| `core/src/config.js` | `phase0/src/config.js` | yes |
| `core/src/index.js` | `phase0/src/index.js` | yes (its header comment still says "Phase 0" — kept verbatim on purpose) |
| `core/test/fixtures.js` | `phase0/test/fixtures.js` | yes |
| `core/test/sniff.nodetest.js` | `phase0/test/sniff.test.js` | content yes — **renamed only**, so CRA/jest does not pick up the `node:test` suites |
| `core/test/tray.nodetest.js` | `phase0/test/tray.test.js` | content yes — renamed only, same reason |

Run the vendored tests in place with node's runner (they are `node:test`
suites, deliberately outside jest's testMatch):

```bash
node --test src/pip/core/test/sniff.nodetest.js src/pip/core/test/tray.nodetest.js
```

Verified in place for this PR: 21/21 pass (11 sniff + 10 tray — the same
per-file counts as Phase 0).

## Character + motion system

- The Pip character (`PipCharacter.jsx`) is an original inline SVG for this
  product (teal runner, Books amber-tinted belly/antenna per DESIGN §5.7 —
  colour-coordinated, not uniformed). No external assets.
- The **movement system is adapted from the owner's desktop Pip**
  (`ts-spaces/desktop-character`, in-house): per-tick walking loop, the
  `stroll | brisk | zoom` speed type with the same per-tick rates
  (0.16 / 0.34 / 0.62), persisted speed + pause settings, stop-and-ask on
  drop, celebration on done. Adapted — not copied: the walk is
  transform-only, the poses are driven by the Phase-0 tray state machine
  (real adapter events only), and the layout follows this app's shell.
  `canvas-confetti` (ISC, already carried by this repo — see
  `frontend/PROVENANCE.md`) fires the celebration burst, lazily imported.

## Host-side glue (original, this PR)

- `config.js` — feature flags (`pip_door.enabled`, kill switch) + the one
  enabled adapter declaration. **This is the only place Phase 1 scope is
  widened or narrowed.**
- `receiptsAdapter.js` — the `books.receipts` adapter (DESIGN §5.2). It
  calls the exact endpoints the Receipts page calls, through the same
  `lib/api` client. It contains **no** post/expense call — by construction.
- `PipDoor.jsx` — the shell door (dropzone, queue, cards, tray). It holds
  no endpoints and no business verbs; it drives the vendored tray store
  with real events only.
- Tray history persists locally per user+tenant (`pip-tray:<tenantId>` in
  localStorage, metadata only — the core store refuses blob fields). File
  objects themselves are memory-only and are gone after a reload; a queued
  entry whose file is gone fails honestly instead of pretending.
- Sniffer evidence strings come from the shared core and are English (the
  core is product-shared); all PipDoor UI chrome is localized via
  `src/locales/<lng>/pip.json`, merged in `lib/i18n.js` exactly like the
  existing `voice.json` files.

## Known scope fences (DESIGN §3.4 / §6.1)

Phase 1 routes **receipts only**. CSVs are signposted to the Cleanup
Console (Phase 2 forks), backup JSON gets the danger wording and no
handling (Phase 3 card), knowledge documents are not an AI Office route
here — in Books they get the honesty card. Pip never scans without an
explicit click, and never posts an expense.
