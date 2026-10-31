// Bridge to the merged tax engine (tax-engine/, PR #147) for the
// year-end tax packet. Reads one JSON job on stdin:
//   {"netProfitDollars": 80000, "jurisdiction": "US-CA",
//    "taxProfile": {"filingStatus": "single", ...}}
// Writes one JSON line on stdout: the engine's TaxResult, or
// {"error": "..."} with exit code 3 when the engine is not built.
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const entry = process.env.TAX_ENGINE_ENTRY
  || path.resolve(here, '..', '..', 'tax-engine', 'dist', 'src', 'index.js');

const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', async () => {
  if (!existsSync(entry)) {
    console.log(JSON.stringify({
      error: 'engine_not_built',
      detail: 'tax-engine dist missing — run: cd tax-engine && npm ci && npm run build',
    }));
    process.exit(3);
  }
  let job;
  try {
    job = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch (e) {
    console.log(JSON.stringify({ error: 'bad_job_json', detail: String(e) }));
    process.exit(2);
  }
  try {
    const engine = await import(entry);
    const result = engine.calculateTax(
      job.netProfitDollars, job.jurisdiction, job.taxProfile || {});
    console.log(JSON.stringify(result));
  } catch (e) {
    console.log(JSON.stringify({ error: 'engine_error', detail: String(e && e.message || e) }));
    process.exit(4);
  }
});
