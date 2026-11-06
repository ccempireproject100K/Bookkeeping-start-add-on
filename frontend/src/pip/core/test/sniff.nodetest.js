import { test } from 'node:test';
import assert from 'node:assert/strict';

import { sniffFile, DEFAULT_LIMITS, MB } from '../src/index.js';
import { corpus, bytes } from './fixtures.js';

/** A corpus case is correct when the FIRST CARD matches the ground truth. */
function isCorrect(result, expect) {
  if (expect.kind && result.kind !== expect.kind) return false;
  if (expect.terminal) return result.terminal === expect.terminal;
  if (expect.needsQuestion) return Boolean(result.needsQuestion) && !result.terminal;
  if (expect.danger) return result.danger === true && result.candidates[0]?.adapterId === expect.topAdapter;
  if (expect.kind === 'video-signpost') {
    return result.signpost?.adapterId === 'aioffice.video_estimates' && result.candidates.length === 0;
  }
  return (
    !result.needsQuestion &&
    !result.terminal &&
    result.candidates[0]?.adapterId === expect.topAdapter
  );
}

test('corpus: ≥95% correct first-card classification (DESIGN §6.2)', () => {
  const misses = [];
  let correct = 0;
  for (const c of corpus) {
    const result = sniffFile(c.input);
    if (isCorrect(result, c.expect)) correct++;
    else misses.push(`${c.name} → got kind=${result.kind} top=${result.candidates[0]?.adapterId ?? '—'} q=${Boolean(result.needsQuestion)} term=${result.terminal ?? '—'}`);
  }
  const accuracy = correct / corpus.length;
  assert.ok(
    accuracy >= 0.95,
    `accuracy ${(accuracy * 100).toFixed(1)}% (${correct}/${corpus.length}) below the 95% bar.\nMisses:\n${misses.join('\n')}`,
  );
});

test('corpus: ZERO silent misroutes — ambiguity always comes back as a question', () => {
  for (const c of corpus) {
    const result = sniffFile(c.input);
    if (c.expect.needsQuestion) {
      assert.ok(result.needsQuestion, `${c.name}: ambiguous case must ask, got a verdict instead`);
      assert.ok(
        !result.candidates.some((cand) => cand.confidence >= 0.8),
        `${c.name}: ambiguous case carries a confident candidate — that is a silent-misroute risk`,
      );
    }
    if (c.expect.topAdapter && !c.expect.terminal) {
      assert.equal(
        result.candidates[0]?.adapterId,
        c.expect.topAdapter,
        `${c.name}: confident first card points at the wrong door`,
      );
    }
  }
});

test('every corpus result carries human-readable evidence (§5.3)', () => {
  for (const c of corpus) {
    const result = sniffFile(c.input);
    assert.ok(Array.isArray(result.evidence) && result.evidence.length > 0, `${c.name}: no evidence`);
    for (const cand of result.candidates) {
      assert.ok(cand.evidence.length > 0, `${c.name}: candidate ${cand.adapterId} has no evidence`);
      assert.ok(cand.confidence >= 0 && cand.confidence <= 1, `${c.name}: confidence out of range`);
    }
  }
});

test('determinism: same input → identical output', () => {
  for (const c of corpus) {
    assert.deepEqual(sniffFile(c.input), sniffFile(c.input), c.name);
  }
});

test('backup JSON is a danger card, never a route Pip carries (§4.3)', () => {
  const c = corpus.find((x) => x.expect.kind === 'backup');
  const result = sniffFile(c.input);
  assert.equal(result.danger, true);
  assert.equal(result.candidates[0].adapterId, 'books.backup');
  assert.match(result.evidence.join(' '), /admin-only/);
});

test('video is a signpost, never a drop target (§4.6)', () => {
  const result = sniffFile({ filename: 'tour.mp4', headerBytes: bytes.mp4(), sizeBytes: 40 * MB, pageContext: { product: 'aioffice' } });
  assert.equal(result.kind, 'video-signpost');
  assert.equal(result.candidates.length, 0);
  assert.equal(result.signpost.action, 'create-request-and-send-link');
});

test('size ceilings are configurable per destination', () => {
  const input = { filename: 'huge-receipt.jpg', headerBytes: bytes.jpeg(), sizeBytes: 12 * MB, pageContext: { product: 'books' } };
  assert.equal(sniffFile(input).terminal, 'oversize');
  const relaxed = sniffFile(input, { limits: { 'books.receipts': 50 * MB, 'aioffice.photo_estimates': 50 * MB } });
  assert.equal(relaxed.terminal, undefined);
  assert.equal(relaxed.candidates[0].adapterId, 'books.receipts');
});

test('default limits mirror DESIGN §1', () => {
  assert.equal(DEFAULT_LIMITS['books.receipts'], 10 * MB);
  assert.equal(DEFAULT_LIMITS['aioffice.onboarding'], 5 * MB);
  assert.equal(DEFAULT_LIMITS['aioffice.kb'], 10 * MB);
  assert.equal(DEFAULT_LIMITS['aioffice.video_estimates'], 100 * MB);
});

test('ZIP whose paths cannot be proven asks instead of guessing (§5.6)', () => {
  const result = sniffFile({ filename: 'office.zip', headerBytes: bytes.zip(['_rels/.rels', 'docProps/core.xml']), sizeBytes: 50_000, pageContext: { product: 'aioffice' } });
  assert.equal(result.kind, 'office-container');
  assert.ok(result.needsQuestion);
});

test('browser mimeType claim never overrides bytes', () => {
  const result = sniffFile({
    filename: 'lying.png', mimeType: 'application/pdf',
    headerBytes: bytes.png(), sizeBytes: 10_000, pageContext: { product: 'books' },
  });
  assert.equal(result.kind, 'image');
});

test('extension claim never overrides bytes', () => {
  const result = sniffFile({ filename: 'statement.pdf', headerBytes: bytes.jpeg(), sizeBytes: 10_000, pageContext: { product: 'books' } });
  assert.equal(result.kind, 'image');
  assert.match(result.evidence.join(' '), /bytes are a JPEG image/);
});
