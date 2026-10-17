// Single source of truth for the backend base URL.
//
// The production incident behind this module: the web service was built
// without REACT_APP_BACKEND_URL, so the bundle baked the literal string
// "undefined/api" as its API base. Every call then hit the static-site
// origin, which answered 200 + the SPA's own index.html (SPA rewrite),
// and pages crashed on the HTML string (".map"/".find" on a string) —
// the app blanked to a black screen. Two defences against a repeat:
//   1. scripts/check-build-env.js fails the production build when the
//      variable is missing or invalid, so a broken bundle cannot ship.
//   2. resolveApiBase never produces the string "undefined": with no
//      backend configured it falls back to a relative "/api", and the
//      response-shape guards in lib/packs.js degrade pages gracefully
//      instead of crashing.

export function backendUrlProblem(env = process.env) {
  const raw = (env.REACT_APP_BACKEND_URL || "").trim();
  if (!raw) return "REACT_APP_BACKEND_URL is not set";
  if (raw === "undefined" || raw === "null") {
    return `REACT_APP_BACKEND_URL is the literal string "${raw}"`;
  }
  let url;
  try {
    url = new URL(raw);
  } catch {
    return `REACT_APP_BACKEND_URL "${raw}" is not a valid URL`;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return `REACT_APP_BACKEND_URL "${raw}" must start with http:// or https://`;
  }
  return null;
}

export function resolveApiBase(env = process.env) {
  const raw = (env.REACT_APP_BACKEND_URL || "").trim().replace(/\/+$/, "");
  return raw ? `${raw}/api` : "/api";
}
