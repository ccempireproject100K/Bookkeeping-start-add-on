/**
 * @empire/pip-door-core — deterministic file sniffer (DESIGN §3.2, §5.3).
 *
 * sniffFile is a PURE function: no I/O, no network, no AI, no randomness.
 * Same input → same output, always. Classification is by the file's own bytes
 * and text, never by vibes:
 *
 *   1. Container signature — magic bytes; ZIP containers (XLSX/DOCX) are told
 *      apart ONLY by a fixed internal-path check over the header bytes
 *      (DESIGN §5.6: identified by container structure, never unzipped beyond
 *      the fixed internal-path check). If the provided bytes cannot prove
 *      XLSX vs DOCX, the result is `office-container` WITH a question spec —
 *      never a silent guess.
 *   2. CSV header fingerprinting — header names scored against the known
 *      books schemas (customers / vendors / items / banking), reusing the
 *      importer's auto-map concept (DESIGN §3.2 step 2).
 *   3. Context hints — filename tokens + the page/product the file was
 *      dropped from (§3.2 step 3). Hints adjust confidence; they never
 *      override hard byte evidence.
 *
 * AI classification (§3.2 step 4) is NOT implemented in Phase 0 — ambiguous
 * cases come back as `needsQuestion` for a human to answer, which is the
 * honest Phase-0 behaviour.
 *
 * Every result carries human-readable `evidence` strings (§5.3).
 */

import { DEFAULT_LIMITS } from './config.js';

/**
 * @typedef {Object} SniffInput
 * @property {string} filename
 * @property {string} [mimeType]        The browser/OS claim — treated as a hint, never as proof.
 * @property {Uint8Array} headerBytes   First ~8 KB of the file.
 * @property {string} [textSample]      First KBs of text, for CSV/JSON/text files (and PDF text extracts).
 * @property {number} sizeBytes         Total file size.
 * @property {{ product?: string, page?: string }} [pageContext]
 */

/**
 * @typedef {Object} RouteCandidate
 * @property {string} adapterId
 * @property {number} confidence        0..1, deterministic.
 * @property {string[]} evidence
 */

/**
 * @typedef {Object} QuestionSpec
 * @property {string} id
 * @property {string} prompt
 * @property {{ id: string, label: string, adapterId?: string }[]} options
 */

/**
 * @typedef {Object} SniffResult
 * @property {string} kind              image | pdf | csv | spreadsheet | document |
 *                                      office-container | backup | json | video-signpost | unknown
 * @property {RouteCandidate[]} candidates  Best first; empty when no door applies.
 * @property {QuestionSpec} [needsQuestion] Present when a human must answer one question.
 * @property {'unsupported'|'oversize'|'corrupt'} [terminal]  Present when no card can be offered.
 * @property {boolean} [danger]         True for the backup danger path (§4.3).
 * @property {{ adapterId: string, action: string }} [signpost]  For signpost-only kinds (video).
 * @property {string[]} evidence        Top-level evidence for the verdict itself.
 */

// ---------------------------------------------------------------------------
// byte helpers
// ---------------------------------------------------------------------------

/** @param {Uint8Array} bytes @param {number[]} sig @param {number} [offset] */
function startsWith(bytes, sig, offset = 0) {
  if (bytes.length < offset + sig.length) return false;
  return sig.every((b, i) => bytes[offset + i] === b);
}

/** True when `bytes` is a proper (truncated) prefix of a known signature. */
function isTruncatedSignature(bytes, sig) {
  if (bytes.length === 0 || bytes.length >= sig.length) return false;
  return sig.every((b, i) => i >= bytes.length || bytes[i] === b) &&
    sig.slice(0, bytes.length).every((b, i) => bytes[i] === b);
}

/** Decode bytes as latin-1 so binary structures (ZIP names) stay searchable. */
function latin1(bytes) {
  let out = '';
  const CHUNK = 4096;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    out += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return out;
}

const SIG = {
  png: [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a],
  jpeg: [0xff, 0xd8, 0xff],
  pdf: [0x25, 0x50, 0x44, 0x46, 0x2d], // "%PDF-"
  zip: [0x50, 0x4b, 0x03, 0x04], // "PK\x03\x04"
  zipEmpty: [0x50, 0x4b, 0x05, 0x06], // empty archive end-record
  webm: [0x1a, 0x45, 0xdf, 0xa3],
  gif: [0x47, 0x49, 0x46, 0x38], // "GIF8"
};

/** @param {Uint8Array} b */
function asciiAt(b, offset, length) {
  return latin1(b.subarray(offset, offset + length));
}

// ---------------------------------------------------------------------------
// ZIP container check — fixed internal paths ONLY (DESIGN §5.6)
// ---------------------------------------------------------------------------

/**
 * Scan the header bytes for ZIP entry path names. Local-file-header filenames
 * appear verbatim in the byte stream; we look for the two fixed path families
 * that distinguish the Office containers. This is NOT a general unzip: no
 * decompression, no entry extraction — a name-presence check only.
 *
 * @param {Uint8Array} bytes
 * @returns {{ isXlsx: boolean, isDocx: boolean, entryNames: string[] }}
 */
function scanZipPaths(bytes) {
  const text = latin1(bytes);
  const runs = text.match(/[\x20-\x7e]{4,}/g) || [];
  const entryNames = runs.filter((r) => r.includes('/'));
  const isXlsx = entryNames.some((n) => n.startsWith('xl/') || n.includes('xl/workbook'));
  const isDocx = entryNames.some((n) => n.startsWith('word/') || n.includes('word/document'));
  return { isXlsx, isDocx, entryNames };
}

// ---------------------------------------------------------------------------
// CSV header fingerprinting (DESIGN §3.2 step 2 / §4.2)
// ---------------------------------------------------------------------------

/** Weighted header-token sets per books schema. Strong tokens weigh 2. */
const CSV_SCHEMAS = {
  customers: {
    adapterId: 'books.importer.customers',
    label: 'customer list',
    weights: {
      customer_name: 2, full_name: 2, first_name: 1, last_name: 1, name: 1, company: 1,
      email: 1, email_address: 1, phone: 1, phone_number: 1, address: 1, city: 1,
      state: 1, zip: 1, zip_code: 1, postal_code: 1,
    },
  },
  vendors: {
    adapterId: 'books.importer.vendors',
    label: 'vendor list',
    weights: {
      vendor_name: 2, supplier_name: 2, supplier: 2, contact_name: 2, payment_terms: 2,
      email: 1, phone: 1, terms: 1, account_number: 1, website: 1,
    },
  },
  items: {
    adapterId: 'books.importer.items',
    label: 'items / price list',
    weights: {
      item_name: 2, sku: 2, unit_price: 2, item: 1, description: 1, price: 1, cost: 1,
      quantity: 1, qty: 1, unit: 1, rate: 1, service: 1,
    },
  },
  banking: {
    adapterId: 'books.banking',
    label: 'bank statement',
    weights: {
      transaction_date: 2, posted_date: 2, debit: 2, credit: 2, balance: 2, amount: 2,
      date: 2, description: 1, memo: 1, payee: 1, type: 1,
    },
  },
};

/** Split a CSV header line on commas, honouring simple double-quoted fields. */
function splitCsvLine(line) {
  const out = [];
  let cur = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++; } else inQuotes = false;
      } else cur += ch;
    } else if (ch === '"') inQuotes = true;
    else if (ch === ',') { out.push(cur); cur = ''; }
    else cur += ch;
  }
  out.push(cur);
  return out;
}

/** @param {string} token */
function normaliseHeader(token) {
  return token
    .replace(/^\uFEFF/, '')
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_')
    .replace(/[^a-z0-9_]/g, '');
}

/**
 * Score a header row against every known schema.
 * @param {string[]} headers normalised header tokens
 * @returns {{ key: string, adapterId: string, label: string, score: number, matched: string[] }[]}
 */
function scoreCsvHeaders(headers) {
  return Object.entries(CSV_SCHEMAS).map(([key, schema]) => {
    const total = Object.values(schema.weights).reduce((a, b) => a + b, 0);
    let got = 0;
    const matched = [];
    for (const h of headers) {
      if (schema.weights[h]) { got += schema.weights[h]; matched.push(h); }
    }
    return { key, adapterId: schema.adapterId, label: schema.label, score: got / total, matched };
  }).sort((a, b) => b.score - a.score);
}

// ---------------------------------------------------------------------------
// text-evidence helpers
// ---------------------------------------------------------------------------

function countHits(text, needles) {
  const lower = text.toLowerCase();
  return needles.filter((n) => lower.includes(n));
}

const RECEIPT_WORDS = ['total', 'subtotal', 'sub-total', 'change due', 'cash', 'thank you', 'receipt', 'sales tax', 'amount due', 'visa', 'mastercard'];
const STATEMENT_WORDS = ['statement', 'account number', 'beginning balance', 'ending balance', 'deposits', 'withdrawals', 'transactions', 'statement period'];
const DOC_WORDS = ['price list', 'handbook', 'manual', 'policy', 'chapter', 'table of contents', 'warranty', 'instructions', 'guide'];

const FILENAME_HINTS = [
  { token: 'statement', kind: 'statement' },
  { token: 'receipt', kind: 'receipt' },
  { token: 'invoice', kind: 'invoice' },
  { token: 'price', kind: 'doc' },
  { token: 'prices', kind: 'doc' },
  { token: 'manual', kind: 'doc' },
  { token: 'handbook', kind: 'doc' },
  { token: 'customers', kind: 'customers' },
  { token: 'vendors', kind: 'vendors' },
  { token: 'backup', kind: 'backup' },
];

/** Extension without the dot, lower-case ('' when none). */
function extOf(filename) {
  const m = /\.([a-z0-9]+)$/i.exec(filename || '');
  return m ? m[1].toLowerCase() : '';
}

function fmtMB(bytes) {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Oversize rule: when every plausible destination for this kind has a ceiling
 * in the limits map and the file exceeds ALL of them, the file is terminally
 * oversize (§4.7 honesty card, quoting the real limits). If any plausible
 * destination can take the file, it is not terminal.
 *
 * @returns {string[] | null} evidence lines when terminally oversize, else null
 */
function oversizeEvidence(sizeBytes, adapterIds, limits) {
  const capped = adapterIds.filter((id) => typeof limits[id] === 'number');
  if (capped.length === 0 || capped.length !== adapterIds.length) return null;
  if (!capped.every((id) => sizeBytes > limits[id])) return null;
  return capped.map(
    (id) => `File is ${fmtMB(sizeBytes)}; the ${id} limit is ${fmtMB(limits[id])}`,
  );
}

// ---------------------------------------------------------------------------
// per-kind classifiers — each returns a SniffResult
// ---------------------------------------------------------------------------

function classifyImage(format, ctx, evidence) {
  const candidates = [];
  const receiptsOk = format === 'png' || format === 'jpeg' || format === 'webp';
  if (receiptsOk) {
    candidates.push({
      adapterId: 'books.receipts',
      confidence: ctx.product === 'books' ? 0.95 : 0.8,
      evidence: [`${format.toUpperCase()} image — an accepted receipt format`, 'Receipts accept JPG/PNG/WEBP'],
    });
  } else {
    evidence.push(`${format.toUpperCase()} is an image, but Receipts accept only JPG/PNG/WEBP — not offered as a receipt route`);
  }
  candidates.push({
    adapterId: 'aioffice.photo_estimates',
    confidence: ctx.product === 'aioffice' || /job|estimate/i.test(ctx.page || '') ? 0.95 : 0.8,
    evidence: [`${format.toUpperCase()} image — photo estimates take image uploads`],
  });
  candidates.sort((a, b) => b.confidence - a.confidence);
  return { kind: 'image', candidates, evidence };
}

function classifyPdf(input, ctx, evidence) {
  evidence.push('Bytes start with %PDF- — this is a PDF (by content, not by name)');
  const text = input.textSample || '';
  const receiptHits = countHits(text, RECEIPT_WORDS);
  const statementHits = countHits(text, STATEMENT_WORDS);
  const docHits = countHits(text, DOC_WORDS);
  const nameHint = FILENAME_HINTS.find((h) => (input.filename || '').toLowerCase().includes(h.token));

  const receiptCand = {
    adapterId: 'books.receipts',
    confidence: 0.3,
    evidence: ['PDFs are an accepted receipt format'],
  };
  const kbCand = {
    adapterId: 'aioffice.kb',
    confidence: 0.3,
    evidence: ['PDFs are an accepted knowledge-base format'],
  };
  if (receiptHits.length) receiptCand.evidence.push(`Receipt wording found: ${receiptHits.join(', ')}`);
  if (docHits.length) kbCand.evidence.push(`Document wording found: ${docHits.join(', ')}`);
  if (ctx.product === 'books') receiptCand.confidence += 0.05;
  if (ctx.product === 'aioffice') kbCand.confidence += 0.05;

  const pdfQuestion = {
    id: 'pdf.purpose',
    prompt: 'Is this PDF a receipt, or a document for the knowledge base?',
    options: [
      { id: 'receipt', label: 'A receipt', adapterId: 'books.receipts' },
      { id: 'knowledge', label: 'Knowledge for the AI', adapterId: 'aioffice.kb' },
      { id: 'neither', label: 'Neither of these' },
    ],
  };

  // Clear bank statement: statement wording dominates. Books banking takes
  // CSV only, so there is NO confident door — ask, with the evidence shown.
  if (statementHits.length >= 2 && statementHits.length > receiptHits.length) {
    evidence.push(`Bank-statement wording found: ${statementHits.join(', ')}`);
    evidence.push('Banking import takes CSV, not PDF — no direct door for a statement PDF');
    kbCand.confidence = 0.35;
    return { kind: 'pdf', candidates: [kbCand, receiptCand], needsQuestion: pdfQuestion, evidence };
  }
  // Clear receipt.
  if (receiptHits.length >= 3 && receiptHits.length > docHits.length + 1) {
    receiptCand.confidence = 0.92;
    kbCand.confidence = 0.3;
    return { kind: 'pdf', candidates: [receiptCand, kbCand], evidence };
  }
  // Clear knowledge document.
  if (docHits.length >= 2 && docHits.length > receiptHits.length + 1) {
    kbCand.confidence = 0.92;
    receiptCand.confidence = 0.3;
    return { kind: 'pdf', candidates: [kbCand, receiptCand], evidence };
  }
  // Filename-only hint is a nudge, never proof.
  if (nameHint?.kind === 'receipt') evidence.push('The file name mentions "receipt", but the page text does not prove it');
  if (nameHint?.kind === 'statement') evidence.push('The file name mentions "statement", but the page text does not prove it');
  evidence.push('The first-page text does not settle receipt vs knowledge document — asking once instead of guessing (§3.2)');
  return { kind: 'pdf', candidates: [receiptCand, kbCand], needsQuestion: pdfQuestion, evidence };
}

function classifyZip(input, ctx, evidence) {
  if (startsWith(input.headerBytes, SIG.zipEmpty)) {
    return {
      kind: 'office-container',
      candidates: [],
      terminal: 'unsupported',
      evidence: [...evidence, 'ZIP end-of-archive record with no entries — the archive is empty'],
    };
  }
  const { isXlsx, isDocx, entryNames } = scanZipPaths(input.headerBytes);
  if (entryNames.length) {
    evidence.push(`ZIP container entries visible in the header bytes: ${entryNames.slice(0, 4).join(', ')}${entryNames.length > 4 ? ', …' : ''}`);
  }
  if (isXlsx && !isDocx) {
    evidence.push('Internal path "xl/" found — this is an Office spreadsheet (XLSX), whatever the file name says');
    return {
      kind: 'spreadsheet',
      candidates: [{
        adapterId: 'aioffice.onboarding',
        confidence: 0.95,
        evidence: ['XLSX container with xl/ workbook paths — onboarding import accepts XLSX'],
      }],
      evidence,
    };
  }
  if (isDocx && !isXlsx) {
    evidence.push('Internal path "word/" found — this is an Office document (DOCX), whatever the file name says');
    return {
      kind: 'document',
      candidates: [{
        adapterId: 'aioffice.kb',
        confidence: 0.95,
        evidence: ['DOCX container with word/ paths — the knowledge base accepts DOCX'],
      }],
      evidence,
    };
  }
  // Both or neither proven from the bytes provided: NEVER guess (§5.6).
  evidence.push('ZIP container, but the header bytes provided cannot prove spreadsheet vs document — asking instead of guessing');
  return {
    kind: 'office-container',
    candidates: [
      { adapterId: 'aioffice.onboarding', confidence: 0.4, evidence: ['Could be an XLSX spreadsheet (unproven from the bytes provided)'] },
      { adapterId: 'aioffice.kb', confidence: 0.4, evidence: ['Could be a DOCX document (unproven from the bytes provided)'] },
    ],
    needsQuestion: {
      id: 'office-container.kind',
      prompt: 'This is an Office file, but Pip cannot tell which kind from its bytes. Which is it?',
      options: [
        { id: 'spreadsheet', label: 'A spreadsheet (Excel)', adapterId: 'aioffice.onboarding' },
        { id: 'document', label: 'A document (Word)', adapterId: 'aioffice.kb' },
        { id: 'neither', label: 'Neither — it is a zip of other files' },
      ],
    },
    evidence,
  };
}

/** Encrypted-backup signature: ciphertext payload + KDF material + backup marker. */
function looksLikeBackup(text) {
  const t = text;
  const hasCiphertext = /"ciphertext"|"cipher_text"|"encrypted_data"/.test(t);
  const hasKdf = /"iv"|"salt"|"kdf"|"nonce"/.test(t);
  const hasMarker = /backup/i.test(t) || /"format"\s*:\s*"[^"]*backup/.test(t);
  return hasCiphertext && hasKdf && hasMarker;
}

function classifyJson(text, evidence) {
  if (looksLikeBackup(text)) {
    evidence.push('JSON carries an encrypted-backup signature (ciphertext + iv/salt + backup marker)');
    evidence.push('Backup restore is admin-only and replaces the whole business dataset — Pip never stages, previews, or carries a restore (§4.3)');
    return {
      kind: 'backup',
      danger: true,
      candidates: [{
        adapterId: 'books.backup',
        confidence: 1,
        evidence: ['Danger link only: the card links to Backup & Restore (admin); Pip does not handle the file'],
      }],
      evidence,
    };
  }
  return {
    kind: 'json',
    candidates: [],
    terminal: 'unsupported',
    evidence: [...evidence, 'JSON file, but not an encrypted backup. No file door in either product accepts plain JSON — the only JSON door is the admin backup page, for signed backups.'],
  };
}

function classifyCsv(input, ctx, evidence) {
  const text = input.textSample ?? latin1(input.headerBytes);
  const firstLine = (text.split(/\r?\n/).find((l) => l.trim().length > 0) || '');
  const headers = splitCsvLine(firstLine).map(normaliseHeader).filter(Boolean);
  evidence.push(`CSV header row: ${headers.join(', ') || '(none readable)'}`);
  const ranked = scoreCsvHeaders(headers);
  const [top, second] = ranked;
  const matchedNote = (r) => (r.matched.length ? `headers match the ${r.label} schema (${r.matched.join(', ')})` : `no ${r.label} headers matched`);

  const forkQuestion = {
    id: 'csv.purpose',
    prompt: 'What kind of list is this CSV?',
    options: [
      { id: 'customers', label: 'Customers', adapterId: 'books.importer.customers' },
      { id: 'vendors', label: 'Vendors', adapterId: 'books.importer.vendors' },
      { id: 'items', label: 'Items / prices', adapterId: 'books.importer.items' },
      { id: 'banking', label: 'Bank transactions', adapterId: 'books.banking' },
      { id: 'clean', label: 'Just clean it', adapterId: 'books.dataclean' },
    ],
  };

  const nameHint = FILENAME_HINTS.find((h) => (input.filename || '').toLowerCase().includes(h.token));

  // Onboarding mirrors the same fingerprints inside AI Office.
  const onboardingCand = (conf, ev) => ({ adapterId: 'aioffice.onboarding', confidence: conf, evidence: ev });

  if (top.score >= 0.35 && top.score - (second?.score ?? 0) >= 0.15) {
    const conf = Math.min(0.97, 0.55 + top.score * 0.6);
    evidence.push(matchedNote(top));
    if (nameHint && ((nameHint.kind === 'statement' && top.key !== 'banking') || (nameHint.kind === 'customers' && top.key !== 'customers') || (nameHint.kind === 'vendors' && top.key !== 'vendors'))) {
      evidence.push(`The file name hints "${nameHint.token}", but the header row is stronger evidence and wins`);
    }
    if (ctx.product === 'aioffice') {
      return {
        kind: 'csv',
        candidates: [
          onboardingCand(conf, [`Headers fingerprint as a ${top.label} — onboarding import takes CSV lists like this`]),
          { adapterId: top.adapterId, confidence: conf * 0.7, evidence: [matchedNote(top)] },
        ],
        evidence,
      };
    }
    const candidates = [
      { adapterId: top.adapterId, confidence: conf, evidence: [matchedNote(top)] },
      { adapterId: 'books.dataclean', confidence: 0.3, evidence: ['Any CSV can be cleaned/deduped first instead — DataClean stays a signpost, suggestions only'] },
    ];
    if (ctx.product !== 'books') {
      candidates.push(onboardingCand(conf * 0.8, [`Headers fingerprint as a ${top.label} — in AI Office this shape goes to onboarding import`]));
    }
    return { kind: 'csv', candidates, evidence };
  }

  // Tie or no signal: the fork question (§4.2). A suggestion is never an auto-route.
  evidence.push(
    top.score > 0
      ? `Headers partly match more than one schema (best: ${top.label} at ${top.score.toFixed(2)}) — a tie is a question, never a silent route`
      : 'Headers match no known schema — asking once instead of guessing',
  );
  const candidates = ranked
    .filter((r) => r.score > 0)
    .map((r) => ({ adapterId: r.adapterId, confidence: Math.max(0.25, r.score), evidence: [matchedNote(r)] }));
  candidates.push({ adapterId: 'books.dataclean', confidence: 0.25, evidence: ['Cleaning is always a safe first step for an unrecognised list'] });
  return { kind: 'csv', candidates, needsQuestion: forkQuestion, evidence };
}

function classifyTextDoc(input, ctx, evidence) {
  const ext = extOf(input.filename);
  const isMd = ext === 'md' || (input.textSample || '').trimStart().startsWith('#');
  evidence.push(isMd ? 'Markdown text document' : 'Plain-text document');
  return {
    kind: 'document',
    candidates: [{
      adapterId: 'aioffice.kb',
      confidence: 0.9,
      evidence: [`${isMd ? 'MD' : 'TXT'} is an accepted knowledge-base format (the confirmation card still applies — upload publishes)`],
    }],
    evidence,
  };
}

// ---------------------------------------------------------------------------
// main entry
// ---------------------------------------------------------------------------

/**
 * Classify a file from its own bytes/text. Pure and deterministic.
 *
 * @param {SniffInput} input
 * @param {{ limits?: Record<string, number> }} [options] Per-destination size
 *   ceilings; defaults to DEFAULT_LIMITS (DESIGN §1). Pass a custom map to
 *   mirror a different destination's numbers.
 * @returns {SniffResult}
 */
export function sniffFile(input, options = {}) {
  const limits = { ...DEFAULT_LIMITS, ...(options.limits || {}) };
  const headerBytes = input.headerBytes || new Uint8Array(0);
  const sizeBytes = typeof input.sizeBytes === 'number' ? input.sizeBytes : headerBytes.length;
  const ctx = input.pageContext || {};
  const ext = extOf(input.filename);
  const evidence = [];

  // --- empty -------------------------------------------------------------
  if (sizeBytes === 0 || (headerBytes.length === 0 && !input.textSample)) {
    return {
      kind: 'unknown',
      candidates: [],
      terminal: 'unsupported',
      evidence: ['The file is empty (0 bytes) — there is nothing to classify or route'],
    };
  }

  // --- truncated signature → corrupt --------------------------------------
  for (const [name, sig] of [['PNG', SIG.png], ['PDF', SIG.pdf], ['ZIP', SIG.zip]]) {
    if (isTruncatedSignature(headerBytes, sig)) {
      return {
        kind: 'unknown',
        candidates: [],
        terminal: 'corrupt',
        evidence: [`The file starts with a truncated ${name} signature and stops — it looks damaged or cut off mid-copy`],
      };
    }
  }
  if ((ext === 'pdf' || input.mimeType === 'application/pdf') &&
      asciiAt(headerBytes, 0, 3) === '%PD' && !startsWith(headerBytes, SIG.pdf)) {
    return {
      kind: 'unknown',
      candidates: [],
      terminal: 'corrupt',
      evidence: ['The file claims to be a PDF but its header is damaged ("%PD" without a valid %PDF- signature)'],
    };
  }

  /** Apply the oversize rule for a classified result, preserving its kind. */
  function finish(result) {
    if (result.terminal || result.kind === 'backup') return result;
    const ids = result.candidates.map((c) => c.adapterId);
    if (result.kind === 'video-signpost') ids.push('aioffice.video_estimates');
    const over = oversizeEvidence(sizeBytes, [...new Set(ids)], limits);
    if (over) return { ...result, candidates: result.candidates, terminal: 'oversize', evidence: [...result.evidence, ...over] };
    return result;
  }

  // --- images --------------------------------------------------------------
  let imageFormat = null;
  if (startsWith(headerBytes, SIG.png)) imageFormat = 'png';
  else if (startsWith(headerBytes, SIG.jpeg)) imageFormat = 'jpeg';
  else if (startsWith(headerBytes, SIG.gif)) imageFormat = 'gif';
  else if (asciiAt(headerBytes, 0, 4) === 'RIFF' && asciiAt(headerBytes, 8, 4) === 'WEBP') imageFormat = 'webp';
  if (imageFormat) {
    if (ext && !['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(ext)) {
      evidence.push(`The file name ends .${ext}, but the bytes are a ${imageFormat.toUpperCase()} image — bytes win over names`);
    }
    evidence.push(`Image signature found: ${imageFormat.toUpperCase()}`);
    return finish(classifyImage(imageFormat, ctx, evidence));
  }

  // --- video (signpost only — §4.6: Pip never accepts a video directly) ----
  const isMp4 = asciiAt(headerBytes, 4, 4) === 'ftyp';
  const isWebm = startsWith(headerBytes, SIG.webm);
  const isAvi = asciiAt(headerBytes, 0, 4) === 'RIFF' && asciiAt(headerBytes, 8, 4) === 'AVI ';
  if (isMp4 || isWebm || isAvi) {
    evidence.push(`Video signature found (${isMp4 ? 'MP4/MOV' : isWebm ? 'WEBM' : 'AVI'})`);
    evidence.push('Walkthrough videos enter through a customer upload link, not the shell drop target (§4.6) — signpost only');
    return finish({
      kind: 'video-signpost',
      candidates: [],
      signpost: { adapterId: 'aioffice.video_estimates', action: 'create-request-and-send-link' },
      evidence,
    });
  }

  // --- PDF -------------------------------------------------------------------
  if (startsWith(headerBytes, SIG.pdf)) {
    return finish(classifyPdf(input, ctx, evidence));
  }

  // --- ZIP containers (XLSX / DOCX / unknown) ---------------------------------
  if (startsWith(headerBytes, SIG.zip) || startsWith(headerBytes, SIG.zipEmpty)) {
    if (ext && ext !== 'xlsx' && ext !== 'docx' && ext !== 'zip') {
      evidence.push(`The file name ends .${ext}, but the bytes are a ZIP container — bytes win over names`);
    }
    return finish(classifyZip(input, ctx, evidence));
  }

  // --- text-shaped files: JSON, CSV, TXT/MD -----------------------------------
  const textSample = input.textSample ?? null;
  const headerText = latin1(headerBytes);
  const candidateText = textSample ?? headerText;
  const trimmed = candidateText.trimStart();

  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    return finish(classifyJson(candidateText, evidence));
  }

  // CSV: an explicit .csv name, or a comma-bearing first line that is not JSON.
  const firstLine = (candidateText.split(/\r?\n/).find((l) => l.trim().length > 0) || '');
  const looksCsv = ext === 'csv' || (firstLine.includes(',') && splitCsvLine(firstLine).length >= 2 && !firstLine.includes('{'));
  if (looksCsv && (textSample || headerBytes.length > 0)) {
    if (imageFormat) { /* unreachable — images returned above */ }
    return finish(classifyCsv(input, ctx, evidence));
  }

  if (ext === 'txt' || ext === 'md' || input.mimeType === 'text/plain' || input.mimeType === 'text/markdown') {
    return finish(classifyTextDoc(input, ctx, evidence));
  }

  // Readable text with no commas and no document claim: treat a .txt-like
  // sample conservatively — if it is mostly printable, it is a document.
  if (textSample && /^[\x09\x0a\x0d\x20-\x7e]+$/.test(textSample.slice(0, 512))) {
    return finish(classifyTextDoc(input, ctx, evidence));
  }

  // --- unknown / unsupported ---------------------------------------------------
  evidence.push('No recognised signature: not an image, PDF, Office container, CSV, JSON backup, text document, or video this system can identify');
  if (ext) evidence.push(`The name ends .${ext}, which no file door in either product accepts`);
  evidence.push('This app takes: receipts (JPG/PNG/WEBP/PDF), CSV lists, spreadsheets (XLSX), documents (PDF/DOCX/TXT/MD), and signed backups (admin page only)');
  return { kind: 'unknown', candidates: [], terminal: 'unsupported', evidence };
}
