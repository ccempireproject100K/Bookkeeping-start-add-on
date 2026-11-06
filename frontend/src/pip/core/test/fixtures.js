/**
 * Synthetic fixture corpus for the Phase-0 sniffer.
 *
 * Every fixture is built in code: synthetic header bytes + text samples.
 * `expect` is the ground truth the sniffer must reach:
 *  - topAdapter:    the confident first-card route (implies NO question, NO terminal)
 *  - needsQuestion: the ONLY acceptable outcome is a needsQuestion verdict
 *  - terminal:      the ONLY acceptable outcome is that terminal verdict
 */

const enc = (s) => new TextEncoder().encode(s);

function concatBytes(...parts) {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of parts) { out.set(p, off); off += p.length; }
  return out;
}

export const bytes = {
  png: () => concatBytes(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), enc('IHDR....synthetic')),
  jpeg: () => concatBytes(new Uint8Array([0xff, 0xd8, 0xff, 0xe0]), enc('JFIF\0synthetic-jpeg-body')),
  webp: () => concatBytes(enc('RIFF'), new Uint8Array([0x24, 0, 0, 0]), enc('WEBPVP8 synthetic')),
  gif: () => enc('GIF89a synthetic gif body'),
  pdf: () => enc('%PDF-1.7\n1 0 obj\n<<>>\nendobj\nsynthetic-pdf-body'),
  /** Minimal local-file-header stream carrying the given entry names verbatim. */
  zip: (entryNames) => concatBytes(
    ...entryNames.map((name) => concatBytes(
      new Uint8Array([0x50, 0x4b, 0x03, 0x04]),
      new Uint8Array(26),
      enc(name),
      new Uint8Array([0xaa, 0xbb]),
    )),
  ),
  xlsx: () => bytes.zip(['[Content_Types].xml', 'xl/workbook.xml', 'xl/worksheets/sheet1.xml']),
  docx: () => bytes.zip(['[Content_Types].xml', 'word/document.xml', 'word/styles.xml']),
  zipUnknown: () => bytes.zip(['photos/img1.jpg', 'photos/img2.jpg', 'readme.txt']),
  mp4: () => concatBytes(new Uint8Array([0, 0, 0, 0x18]), enc('ftypisom....synthetic-video')),
  webm: () => concatBytes(new Uint8Array([0x1a, 0x45, 0xdf, 0xa3]), enc('synthetic-webm')),
  exe: () => concatBytes(enc('MZ'), new Uint8Array([0x90, 0x00, 0x03, 0x00, 0xff, 0x11, 0x42])),
  truncatedPng: () => new Uint8Array([0x89, 0x50, 0x4e]),
  garbage: () => new Uint8Array([0x00, 0x13, 0x77, 0xfe, 0x01, 0x99, 0x42, 0x10, 0xde, 0xad]),
};

const MB = 1024 * 1024;
const BOOKS = { product: 'books' };
const AIOFFICE = { product: 'aioffice' };

/** @type {{ name: string, input: object, expect: object }[]} */
export const corpus = [
  {
    name: 'PNG receipt photo (Books)',
    input: { filename: 'receipt.jpg.png', headerBytes: bytes.png(), sizeBytes: 420_000, pageContext: BOOKS },
    expect: { kind: 'image', topAdapter: 'books.receipts' },
  },
  {
    name: 'JPEG job-site photo (AI Office, Jobs page)',
    input: { filename: 'broken-unit.jpg', headerBytes: bytes.jpeg(), sizeBytes: 2 * MB, pageContext: { product: 'aioffice', page: 'jobs' } },
    expect: { kind: 'image', topAdapter: 'aioffice.photo_estimates' },
  },
  {
    name: 'WEBP receipt photo (Books)',
    input: { filename: 'receipt.webp', headerBytes: bytes.webp(), sizeBytes: 300_000, pageContext: BOOKS },
    expect: { kind: 'image', topAdapter: 'books.receipts' },
  },
  {
    name: 'PNG misnamed as .csv — bytes win',
    input: { filename: 'customers.csv', headerBytes: bytes.png(), sizeBytes: 100_000, pageContext: BOOKS },
    expect: { kind: 'image', topAdapter: 'books.receipts' },
  },
  {
    name: 'PDF receipt with totals wording',
    input: {
      filename: 'supply-house.pdf', headerBytes: bytes.pdf(), sizeBytes: 80_000, pageContext: BOOKS,
      textSample: 'SUPPLY HOUSE\nRECEIPT #4417\nSubtotal 112.00\nSales Tax 9.24\nTOTAL 121.24\nCash 130.00\nThank you!',
    },
    expect: { kind: 'pdf', topAdapter: 'books.receipts' },
  },
  {
    name: 'PDF bank statement — no CSV door, must ask',
    input: {
      filename: 'march-statement.pdf', headerBytes: bytes.pdf(), sizeBytes: 200_000, pageContext: BOOKS,
      textSample: 'Statement Period: March\nAccount Number 8831\nBeginning Balance 4,102.11\nDeposits 9,300.00\nWithdrawals 5,112.40\nEnding Balance 8,289.71',
    },
    expect: { kind: 'pdf', needsQuestion: true },
  },
  {
    name: 'PDF with one weak receipt word — ambiguous, must ask',
    input: {
      filename: 'doc.pdf', headerBytes: bytes.pdf(), sizeBytes: 90_000, pageContext: AIOFFICE,
      textSample: 'Total: $50.00\nThank you for your business.',
    },
    expect: { kind: 'pdf', needsQuestion: true },
  },
  {
    name: 'PDF named receipt but no text evidence — must ask',
    input: { filename: 'receipt.pdf', headerBytes: bytes.pdf(), sizeBytes: 90_000, pageContext: BOOKS },
    expect: { kind: 'pdf', needsQuestion: true },
  },
  {
    name: 'PDF price-list handbook (AI Office KB)',
    input: {
      filename: 'price-guide.pdf', headerBytes: bytes.pdf(), sizeBytes: 500_000, pageContext: AIOFFICE,
      textSample: 'Price List 2026\nChapter 1 — Handbook\nThis manual covers warranty policy and the full price list.',
    },
    expect: { kind: 'pdf', topAdapter: 'aioffice.kb' },
  },
  {
    name: 'CSV customers (clear fingerprint)',
    input: {
      filename: 'customers.csv', sizeBytes: 12_000, pageContext: BOOKS,
      headerBytes: enc('customer_name,email,phone,address,city,state,zip\n'),
      textSample: 'customer_name,email,phone,address,city,state,zip\nAda Lovelace,ada@example.com,555-0100,1 Main St,Fort Worth,TX,76101\n',
    },
    expect: { kind: 'csv', topAdapter: 'books.importer.customers' },
  },
  {
    name: 'CSV vendors (clear fingerprint)',
    input: {
      filename: 'vendors.csv', sizeBytes: 8_000, pageContext: BOOKS,
      headerBytes: enc('vendor_name,contact_name,email,phone,payment_terms\n'),
      textSample: 'vendor_name,contact_name,email,phone,payment_terms\nSupply Co,Sam,sam@supply.example,555-0111,Net 30\n',
    },
    expect: { kind: 'csv', topAdapter: 'books.importer.vendors' },
  },
  {
    name: 'CSV items / price list (clear fingerprint)',
    input: {
      filename: 'items.csv', sizeBytes: 8_000, pageContext: BOOKS,
      headerBytes: enc('item_name,sku,description,unit_price,quantity\n'),
      textSample: 'item_name,sku,description,unit_price,quantity\nFilter 16x25,FLT-1625,Air filter,12.50,40\n',
    },
    expect: { kind: 'csv', topAdapter: 'books.importer.items' },
  },
  {
    name: 'CSV bank statement (clear fingerprint)',
    input: {
      filename: 'bank-export.csv', sizeBytes: 20_000, pageContext: BOOKS,
      headerBytes: enc('date,description,debit,credit,balance\n'),
      textSample: 'date,description,debit,credit,balance\n2026-03-01,SUPPLY HOUSE,121.24,,8102.55\n',
    },
    expect: { kind: 'csv', topAdapter: 'books.banking' },
  },
  {
    name: 'CSV named "statement" but headers are customers — headers win',
    input: {
      filename: 'statement.csv', sizeBytes: 12_000, pageContext: BOOKS,
      headerBytes: enc('customer_name,email,phone,address,city,state,zip\n'),
      textSample: 'customer_name,email,phone,address,city,state,zip\nAda Lovelace,ada@example.com,555-0100,1 Main St,Fort Worth,TX,76101\n',
    },
    expect: { kind: 'csv', topAdapter: 'books.importer.customers' },
  },
  {
    name: 'CSV tied headers (banking vs customers) — must ask',
    input: {
      filename: 'export.csv', sizeBytes: 9_000, pageContext: BOOKS,
      headerBytes: enc('date,amount,name,phone\n'),
      textSample: 'date,amount,name,phone\n2026-03-01,50.00,Ada,555-0100\n',
    },
    expect: { kind: 'csv', needsQuestion: true },
  },
  {
    name: 'CSV unknown headers — must ask',
    input: {
      filename: 'data.csv', sizeBytes: 4_000, pageContext: BOOKS,
      headerBytes: enc('foo,bar,baz\n'),
      textSample: 'foo,bar,baz\n1,2,3\n',
    },
    expect: { kind: 'csv', needsQuestion: true },
  },
  {
    name: 'CSV customers inside AI Office — onboarding route',
    input: {
      filename: 'old-customers.csv', sizeBytes: 12_000, pageContext: AIOFFICE,
      headerBytes: enc('customer_name,email,phone,address,city,state,zip\n'),
      textSample: 'customer_name,email,phone,address,city,state,zip\nAda Lovelace,ada@example.com,555-0100,1 Main St,Fort Worth,TX,76101\n',
    },
    expect: { kind: 'csv', topAdapter: 'aioffice.onboarding' },
  },
  {
    name: 'XLSX spreadsheet',
    input: { filename: 'prices.xlsx', headerBytes: bytes.xlsx(), sizeBytes: 60_000, pageContext: AIOFFICE },
    expect: { kind: 'spreadsheet', topAdapter: 'aioffice.onboarding' },
  },
  {
    name: 'XLSX renamed .csv — container wins',
    input: { filename: 'prices.csv', headerBytes: bytes.xlsx(), sizeBytes: 60_000, pageContext: AIOFFICE },
    expect: { kind: 'spreadsheet', topAdapter: 'aioffice.onboarding' },
  },
  {
    name: 'DOCX document',
    input: { filename: 'warranty.docx', headerBytes: bytes.docx(), sizeBytes: 70_000, pageContext: AIOFFICE },
    expect: { kind: 'document', topAdapter: 'aioffice.kb' },
  },
  {
    name: 'DOCX renamed .pdf — container wins',
    input: { filename: 'manual.pdf', headerBytes: bytes.docx(), sizeBytes: 70_000, pageContext: AIOFFICE },
    expect: { kind: 'document', topAdapter: 'aioffice.kb' },
  },
  {
    name: 'ZIP of photos — container unproven, must ask',
    input: { filename: 'bundle.zip', headerBytes: bytes.zipUnknown(), sizeBytes: 3 * MB, pageContext: AIOFFICE },
    expect: { kind: 'office-container', needsQuestion: true },
  },
  {
    name: 'Encrypted backup JSON — danger path',
    input: {
      filename: 'backup-2026-11-01.json', sizeBytes: 900_000, pageContext: BOOKS,
      headerBytes: enc('{"format":"empire-backup","version":3,"ciphertext":"aGVsbG8=","iv":"AAEC","salt":"c2FsdA==","kdf":"pbkdf2"}'),
      textSample: '{"format":"empire-backup","version":3,"ciphertext":"aGVsbG8=","iv":"AAEC","salt":"c2FsdA==","kdf":"pbkdf2"}',
    },
    expect: { kind: 'backup', topAdapter: 'books.backup', danger: true },
  },
  {
    name: 'Plain JSON export — unsupported',
    input: {
      filename: 'export.json', sizeBytes: 2_000, pageContext: BOOKS,
      headerBytes: enc('{"customers":[{"name":"Ada"}]}'),
      textSample: '{"customers":[{"name":"Ada"}]}',
    },
    expect: { kind: 'json', terminal: 'unsupported' },
  },
  {
    name: 'TXT knowledge document',
    input: {
      filename: 'policies.txt', sizeBytes: 3_000, pageContext: AIOFFICE,
      headerBytes: enc('Office policies\nAll jobs require a signed estimate.\n'),
      textSample: 'Office policies\nAll jobs require a signed estimate.\n',
    },
    expect: { kind: 'document', topAdapter: 'aioffice.kb' },
  },
  {
    name: 'Markdown knowledge document',
    input: {
      filename: 'guide.md', sizeBytes: 3_000, pageContext: AIOFFICE,
      headerBytes: enc('# Service Guide\n## Pricing\n'),
      textSample: '# Service Guide\n## Pricing\n',
    },
    expect: { kind: 'document', topAdapter: 'aioffice.kb' },
  },
  {
    name: 'MP4 walkthrough video — signpost only',
    input: { filename: 'walkthrough.mp4', headerBytes: bytes.mp4(), sizeBytes: 50 * MB, pageContext: AIOFFICE },
    expect: { kind: 'video-signpost' },
  },
  {
    name: 'WEBM video — signpost only',
    input: { filename: 'clip.webm', headerBytes: bytes.webm(), sizeBytes: 10 * MB, pageContext: AIOFFICE },
    expect: { kind: 'video-signpost' },
  },
  {
    name: 'Empty file',
    input: { filename: 'nothing.csv', headerBytes: new Uint8Array(0), sizeBytes: 0, pageContext: BOOKS },
    expect: { kind: 'unknown', terminal: 'unsupported' },
  },
  {
    name: 'Truncated PNG signature — corrupt',
    input: { filename: 'photo.png', headerBytes: bytes.truncatedPng(), sizeBytes: 3, pageContext: BOOKS },
    expect: { kind: 'unknown', terminal: 'corrupt' },
  },
  {
    name: 'Windows executable — unsupported',
    input: { filename: 'setup.exe', headerBytes: bytes.exe(), sizeBytes: 5 * MB, pageContext: BOOKS },
    expect: { kind: 'unknown', terminal: 'unsupported' },
  },
  {
    name: 'Random garbage bytes — unsupported',
    input: { filename: 'mystery.bin', headerBytes: bytes.garbage(), sizeBytes: 10, pageContext: AIOFFICE },
    expect: { kind: 'unknown', terminal: 'unsupported' },
  },
  {
    name: 'Oversize JPEG receipt (12 MB > 10 MB everywhere)',
    input: { filename: 'huge-receipt.jpg', headerBytes: bytes.jpeg(), sizeBytes: 12 * MB, pageContext: BOOKS },
    expect: { kind: 'image', terminal: 'oversize' },
  },
  {
    name: 'Oversize XLSX (6 MB > 5 MB onboarding)',
    input: { filename: 'big-prices.xlsx', headerBytes: bytes.xlsx(), sizeBytes: 6 * MB, pageContext: AIOFFICE },
    expect: { kind: 'spreadsheet', terminal: 'oversize' },
  },
  {
    name: 'Oversize video (150 MB > 100 MB signpost ceiling)',
    input: { filename: 'long-tour.mp4', headerBytes: bytes.mp4(), sizeBytes: 150 * MB, pageContext: AIOFFICE },
    expect: { kind: 'video-signpost', terminal: 'oversize' },
  },
];
