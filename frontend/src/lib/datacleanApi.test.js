import {
  API_ENDPOINTS, ERROR_CODES, FALLBACK_CAPS, FALLBACK_PRICING, SAMPLE_CSV,
  barFraction, bundleComponentState, keyStatus, priceLabel, usageSeries,
} from "./datacleanApi";

describe("keyStatus", () => {
  test("active until revoked_at is set", () => {
    expect(keyStatus({ revoked_at: null })).toBe("active");
    expect(keyStatus({})).toBe("active");
    expect(keyStatus({ revoked_at: "2026-10-30T00:00:00Z" })).toBe("revoked");
  });
});

describe("usageSeries + barFraction", () => {
  const rows = [
    { date: "2026-10-30", calls: 4, rows: 40 },
    { date: "2026-10-28", calls: 0, rows: 0 },
    { date: "2026-10-29", calls: "2", rows: 9 },
  ];
  test("sorts ascending and numericizes", () => {
    const s = usageSeries(rows);
    expect(s.map((d) => d.date)).toEqual(["2026-10-28", "2026-10-29", "2026-10-30"]);
    expect(s[1].calls).toBe(2);
  });
  test("bars scale to the busiest day; zero series stays flat", () => {
    const s = usageSeries(rows);
    expect(barFraction(4, s)).toBe(1);
    expect(barFraction(2, s)).toBe(0.5);
    expect(barFraction(0, usageSeries([{ date: "2026-10-30", calls: 0, rows: 0 }]))).toBe(0);
    expect(barFraction(5, [])).toBe(0);
  });
  test("empty input is an empty series, not invented days", () => {
    expect(usageSeries(null)).toEqual([]);
    expect(usageSeries(undefined)).toEqual([]);
  });
});

describe("docs catalogs mirror the implemented surface", () => {
  test("every endpoint is fully described", () => {
    expect(API_ENDPOINTS).toHaveLength(7);
    for (const e of API_ENDPOINTS) {
      expect(e.method).toMatch(/^(GET|POST)$/);
      expect(e.path).toMatch(/^\/api\/v1\/dataclean\//);
      expect(e.descKey).toMatch(/^datacleanApi\.docs/);
      expect(["key", "none"]).toContain(e.auth);
    }
    const paths = API_ENDPOINTS.map((e) => e.path);
    expect(new Set(paths).size).toBe(paths.length);
  });
  test("error codes cover the surface's failure vocabulary", () => {
    const codes = ERROR_CODES.map((e) => e.code);
    for (const c of ["invalid_api_key", "rate_limited", "quota_exceeded",
                     "payload_too_large", "playground_limit", "unknown_kind",
                     "unknown_field", "unknown_type", "invalid_payload"]) {
      expect(codes).toContain(c);
    }
    for (const e of ERROR_CODES) {
      expect(e.status).toBeGreaterThanOrEqual(400);
      expect(e.descKey).toMatch(/^datacleanApi\.err/);
    }
  });
});

describe("fallback config is honestly placeholder", () => {
  test("caps match the backend defaults", () => {
    expect(FALLBACK_CAPS).toEqual({ max_file_bytes: 65536, max_rows: 200, max_columns: 24 });
  });
  test("fallback pricing is flagged placeholder with zero prices", () => {
    expect(FALLBACK_PRICING.placeholder).toBe(true);
    expect(FALLBACK_PRICING.billing_mode).toBe("test");
    for (const p of FALLBACK_PRICING.plans) {
      expect(p.monthly_price_cents).toBe(0);
      expect(p.rows_included).toBeGreaterThan(0);
    }
  });
  test("priceLabel formats integer cents", () => {
    expect(priceLabel(0)).toBe("$0.00");
    expect(priceLabel(1299)).toBe("$12.99");
    expect(priceLabel(null)).toBe("$0.00");
  });
});

describe("fallback bundle mirrors the backend bundle config", () => {
  test("one bundle at the owner-set price, honestly flagged per half", () => {
    expect(FALLBACK_PRICING.bundles).toHaveLength(1);
    const b = FALLBACK_PRICING.bundles[0];
    expect(b.id).toBe("dataclean-security");
    expect(b.name).toBe("DataClean + Security");
    // Real owner-set price (2026-10-31) — NOT a $0 placeholder, even
    // though the plans above still are.
    expect(b.monthly_price_cents).toBe(1899);
    expect(b.placeholder).toBe(false);
    const halves = Object.fromEntries(b.components.map((c) => [c.id, c]));
    expect(Object.keys(halves).sort()).toEqual(["dataclean", "security"]);
    expect(halves.dataclean.status).toBe("live");
    expect(halves.dataclean.built).toBe(true);
    expect(halves.dataclean.available).toBe(true);
    // The security half is not built: coming soon, never available,
    // and no feature claims ride along.
    expect(halves.security.status).toBe("coming_soon");
    expect(halves.security.built).toBe(false);
    expect(halves.security.available).toBe(false);
    expect(halves.security.features).toBeUndefined();
  });

  test("bundleComponentState follows the flags, not the identity", () => {
    const b = FALLBACK_PRICING.bundles[0];
    const halves = Object.fromEntries(b.components.map((c) => [c.id, c]));
    expect(bundleComponentState(halves.dataclean)).toBe("available");
    expect(bundleComponentState(halves.security)).toBe("coming_soon");
    // Built but not yet available still reads as coming soon, and a
    // missing half never reads as available.
    expect(bundleComponentState({ built: true, available: false })).toBe("coming_soon");
    expect(bundleComponentState({ built: false, available: true })).toBe("coming_soon");
    expect(bundleComponentState(null)).toBe("coming_soon");
    expect(bundleComponentState(undefined)).toBe("coming_soon");
  });
});

describe("SAMPLE_CSV", () => {
  test("is a small messy file the real rules can fix", () => {
    const lines = SAMPLE_CSV.split("\n");
    expect(lines.length).toBeGreaterThan(3);
    expect(lines[0]).toContain("Phone");
    expect(SAMPLE_CSV).toContain("(512) 555-0134");
    expect(SAMPLE_CSV).toContain("OPS@ACME.EXAMPLE");
  });
});
