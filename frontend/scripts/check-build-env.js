#!/usr/bin/env node
/* Build-time guard: refuse to produce a production bundle without a
 * valid REACT_APP_BACKEND_URL.
 *
 * Why this exists: the bookkeeping web service was once built with the
 * variable unset. CRA inlined the literal string "undefined" and the
 * bundle's API base became "undefined/api", so every API call hit the
 * static site itself, got 200 + index.html, and the SPA crashed to a
 * black screen while its industry pages never loaded. The build must
 * fail loudly instead of shipping that bundle again.
 *
 * This script is intentionally standalone (no imports from src/) so it
 * runs before bundling in any environment. The runtime half of the same
 * rules lives in src/lib/apiBase.js.
 */
const raw = (process.env.REACT_APP_BACKEND_URL || "").trim();

function fail(message) {
  console.error("");
  console.error("[check-build-env] BUILD REFUSED: " + message);
  console.error(
    "Set REACT_APP_BACKEND_URL to the API origin, e.g. " +
      "REACT_APP_BACKEND_URL=https://bookkeeping-api-ymh6.onrender.com npm run build"
  );
  console.error("");
  process.exit(1);
}

if (!raw) {
  fail("REACT_APP_BACKEND_URL is not set — the bundle would bake a bogus API base.");
}
if (raw === "undefined" || raw === "null") {
  fail(`REACT_APP_BACKEND_URL is the literal string "${raw}".`);
}
let url;
try {
  url = new URL(raw);
} catch {
  fail(`REACT_APP_BACKEND_URL "${raw}" is not a valid URL.`);
}
if (url.protocol !== "http:" && url.protocol !== "https:") {
  fail(`REACT_APP_BACKEND_URL "${raw}" must start with http:// or https://.`);
}
if (
  (url.hostname === "localhost" || url.hostname === "127.0.0.1") &&
  process.env.ALLOW_LOCAL_BACKEND !== "1"
) {
  fail(
    `REACT_APP_BACKEND_URL points at ${url.hostname} — that only works on a dev machine. ` +
      "(Set ALLOW_LOCAL_BACKEND=1 to override for a local production-build test.)"
  );
}

console.log(`[check-build-env] REACT_APP_BACKEND_URL ok: ${url.origin}`);
