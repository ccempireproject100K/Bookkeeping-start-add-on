// Pure helpers + catalogs for the DataClean API shell (Phase 3):
// the console page (keys / usage / docs), the public playground and
// the storefront. Framework-free so the shaping rules are unit-tested
// without rendering. Anything that looks like a number here is either
// a documented API fact (paths, error codes) or a fallback the pages
// replace with the backend's config the moment it loads — the
// fallback pricing is the backend's placeholder default, flagged as
// placeholder exactly like the backend flags it.

/** The external surface, as implemented by routers/dataclean_api.py.
 * The docs tab renders from this catalog — if an endpoint is not
 * here, it is not documented; if it is here, it exists. */
export const API_ENDPOINTS = [
  { method: "POST", path: "/api/v1/dataclean/cleanup/preview", auth: "key", metered: true, descKey: "datacleanApi.docsPreview" },
  { method: "POST", path: "/api/v1/dataclean/cleanup/clean", auth: "key", metered: true, descKey: "datacleanApi.docsClean" },
  { method: "POST", path: "/api/v1/dataclean/dedupe/scan", auth: "key", metered: true, descKey: "datacleanApi.docsDedupe" },
  { method: "POST", path: "/api/v1/dataclean/normalize", auth: "key", metered: true, descKey: "datacleanApi.docsNormalize" },
  { method: "GET", path: "/api/v1/dataclean/usage", auth: "key", metered: false, descKey: "datacleanApi.docsUsage" },
  { method: "GET", path: "/api/v1/dataclean/pricing", auth: "none", metered: false, descKey: "datacleanApi.docsPricing" },
  { method: "POST", path: "/api/v1/dataclean/playground/preview", auth: "none", metered: false, descKey: "datacleanApi.docsPlayground" },
];

/** Error codes the surface can return, as implemented. */
export const ERROR_CODES = [
  { code: "invalid_api_key", status: 401, descKey: "datacleanApi.errInvalidKey" },
  { code: "rate_limited", status: 429, descKey: "datacleanApi.errRateLimited" },
  { code: "quota_exceeded", status: 429, descKey: "datacleanApi.errQuota" },
  { code: "payload_too_large", status: 413, descKey: "datacleanApi.errTooLarge" },
  { code: "playground_limit", status: 413, descKey: "datacleanApi.errPlaygroundLimit" },
  { code: "unknown_kind", status: 422, descKey: "datacleanApi.errUnknownKind" },
  { code: "unknown_field", status: 422, descKey: "datacleanApi.errUnknownField" },
  { code: "unknown_type", status: 422, descKey: "datacleanApi.errUnknownType" },
  { code: "invalid_payload", status: 422, descKey: "datacleanApi.errInvalidPayload" },
];

/** A key's display status: revoked (with its timestamp) or active. */
export function keyStatus(key) {
  return key?.revoked_at ? "revoked" : "active";
}

/** Normalize the usage endpoint's by_day rows into a chart series:
 * ascending by date, numeric, gaps kept as real zeros (the backend
 * zero-fills; this guards the shape, it never invents a day). */
export function usageSeries(byDay) {
  return (byDay || [])
    .map((d) => ({ date: d.date, calls: Number(d.calls) || 0, rows: Number(d.rows) || 0 }))
    .sort((a, b) => String(a.date).localeCompare(String(b.date)));
}

/** Bar height 0..1 against the series' busiest day; 0 for an
 * all-zero series (no fake minimum bars). */
export function barFraction(value, series, field = "calls") {
  const max = Math.max(0, ...(series || []).map((d) => Number(d[field]) || 0));
  if (!max) return 0;
  return (Number(value) || 0) / max;
}

/** The demo's built-in sample: small, visibly messy, every row
 * fixable by a real rule (phone formats, email case, name spacing). */
export const SAMPLE_CSV = [
  "Client,E-mail,Phone,Address",
  "Acme  Supply Inc,OPS@ACME.EXAMPLE,(512) 555-0134,\"9 Oak Ave, Dallas, TX 75001\"",
  "Lone Star Plumbing,,512.555.0142,\"77 Ranch Rd, Austin, TX 78701\"",
  "\"ACME SUPPLY, INC.\",ops@acme.example,512-555-0134,\"9 Oak Ave, Dallas, TX 75001\"",
  "Bluebonnet Bakery,hello@bluebonnet.example, (512) 555-0177 ,\"1200 S Lamar Blvd, Austin, TX 78704\"",
].join("\n");

/** Playground caps before the pricing config loads — the backend's
 * own defaults, replaced by the real config on load. */
export const FALLBACK_CAPS = { max_file_bytes: 65536, max_rows: 200, max_columns: 24 };

/** Storefront pricing before the config loads — mirrors the
 * backend's config and carries the same flags: the plans are the
 * backend's placeholder defaults, and the bundle mirrors the
 * owner-set price with its per-half honesty flags (DataClean live,
 * Security not built — coming soon, never dressed up as available). */
export const FALLBACK_PRICING = {
  currency: "USD",
  placeholder: true,
  billing_mode: "test",
  plans: [
    { id: "starter", name: "Starter", monthly_price_cents: 0, rows_included: 10000, overage_per_1000_rows_cents: 0 },
    { id: "growth", name: "Growth", monthly_price_cents: 0, rows_included: 50000, overage_per_1000_rows_cents: 0 },
    { id: "scale", name: "Scale", monthly_price_cents: 0, rows_included: 250000, overage_per_1000_rows_cents: 0 },
  ],
  bundles: [
    {
      id: "dataclean-security",
      name: "DataClean + Security",
      monthly_price_cents: 1899,
      placeholder: false,
      components: [
        { id: "dataclean", name: "DataClean", status: "live", built: true, available: true },
        { id: "security", name: "Security", status: "coming_soon", built: false, available: false },
      ],
    },
  ],
};

/** A bundle half's display state, derived from the config's own
 * flags — never from the half's identity: "available" only when the
 * backend says built AND available, otherwise "coming_soon". */
export function bundleComponentState(component) {
  return component?.built && component?.available ? "available" : "coming_soon";
}

/** "$12.00" from integer cents; free reads as the number, not a
 * promise — placeholder plans show 0 and the page badges them. */
export function priceLabel(cents) {
  const n = Number(cents) || 0;
  return `$${(n / 100).toFixed(2)}`;
}
