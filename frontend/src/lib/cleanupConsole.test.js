import {
  gradeTone, messHealth, confidenceLabelKey, visibleSuggestions,
  recordFieldRows, previewChangeRows, countChangesByRule,
  ruleLabelKey, ruleStateRows, heldChangeRows, hasWeeklyTrend, sparkPoints,
} from "./cleanupConsole";

describe("gradeTone bands match the books health score", () => {
  test("emerald amber rose", () => {
    expect(gradeTone(80)).toBe("emerald");
    expect(gradeTone(75)).toBe("emerald");
    expect(gradeTone(60)).toBe("amber");
    expect(gradeTone(55)).toBe("amber");
    expect(gradeTone(20)).toBe("rose");
    expect(gradeTone(null)).toBe("gray");
  });
});

describe("messHealth", () => {
  const preview = (cols, extra = {}) => ({
    rows: 100, can_import: true,
    mess_report: { columns: cols },
    schema: { blocking: [], row_error_count: 0 }, ...extra,
  });
  test("clean file scores high", () => {
    expect(messHealth(preview([{ column: "email", empty: 0, issues: {} }])));
    expect(messHealth(preview([{ column: "email", empty: 0, issues: {} }])))
      .toBeGreaterThan(90);
  });
  test("invalids, empties and blocking checks drag it down", () => {
    const dirty = messHealth(preview(
      [{ column: "email", empty: 10, issues: { invalid_emails: 20 } },
       { column: "phone", empty: 0, issues: { invalid_phones: 5, mixed_date_formats: ["A"] } }],
      { can_import: false, schema: { blocking: ["email is unusable"], row_error_count: 3 } }));
    expect(dirty).toBeLessThan(60);
  });
  test("no preview, no score (never faked)", () => {
    expect(messHealth(null)).toBeNull();
    expect(messHealth({ rows: 0, mess_report: { columns: [] } })).toBeNull();
  });
});

describe("confidenceLabelKey", () => {
  test("thresholds", () => {
    expect(confidenceLabelKey(0.99)).toBe("cleanupConsole.confidenceVeryHigh");
    expect(confidenceLabelKey(0.92)).toBe("cleanupConsole.confidenceHigh");
    expect(confidenceLabelKey(0.85)).toBe("cleanupConsole.confidenceLikely");
    expect(confidenceLabelKey(0.6)).toBe("cleanupConsole.confidencePossible");
  });
});

describe("visibleSuggestions", () => {
  const S = [
    { id: "1", entity: "customers", score: 0.99, a_name: "Zeta", created_at: "2026-10-01T00:00:00" },
    { id: "2", entity: "vendors", score: 0.7, a_name: "Alpha", created_at: "2026-10-03T00:00:00" },
    { id: "3", entity: "customers", score: 0.85, a_name: "Mid", created_at: "2026-10-02T00:00:00" },
  ];
  test("entity filter + threshold + score sort", () => {
    expect(visibleSuggestions(S, { entity: "customers", minScore: 0.8, sort: "score" })
      .map((x) => x.id)).toEqual(["1", "3"]);
  });
  test("name and recent sorts", () => {
    expect(visibleSuggestions(S, { entity: "all", minScore: 0, sort: "name" })
      .map((x) => x.id)).toEqual(["2", "3", "1"]);
    expect(visibleSuggestions(S, { entity: "all", minScore: 0, sort: "recent" })
      .map((x) => x.id)).toEqual(["2", "3", "1"]);
  });
});

describe("recordFieldRows", () => {
  test("flags differing fields and reads locations[0].address", () => {
    const rows = recordFieldRows(
      { name: "Acme", email: "a@x.co", phone: "5125550134", locations: [{ address: "1 Main St" }] },
      { name: "Acme Inc", email: "a@x.co", phone: "", address: "1 Main St" });
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));
    expect(byKey.name.differs).toBe(true);
    expect(byKey.email.differs).toBe(false);
    expect(byKey.phone.differs).toBe(true);
    expect(byKey.address.a).toBe("1 Main St");
    expect(byKey.address.differs).toBe(false);
  });
});

describe("previewChangeRows + countChangesByRule", () => {
  const p = { normalization_preview: {
    phone: [
      { row: 2, field: "phone", before: "(512) 555-0134", after: "+15125550134", rule: "phone:x", confidence: 0.99 },
      { row: 3, field: "phone", before: "512.555.9999", after: "+15125559999", rule: "phone:x", confidence: 0.99 },
    ],
    email: [
      { row: 4, field: "email", before: "A@X.CO", after: "a@x.co", rule: "email:y", confidence: 0.95 },
    ],
  } };
  test("rows and rule counts", () => {
    expect(previewChangeRows(p)).toHaveLength(3);
    expect(countChangesByRule(p)).toEqual({ "phone:x": 2, "email:y": 1 });
    expect(previewChangeRows(null)).toEqual([]);
  });
});

describe("Phase 2: rule states, held changes, weekly trend", () => {
  test("ruleLabelKey maps known rules, unknown falls back to null", () => {
    expect(ruleLabelKey("phone:libphonenumber-e164"))
      .toBe("cleanupConsole.rulePhoneFormat");
    expect(ruleLabelKey("some:future-rule")).toBeNull();
  });

  test("ruleStateRows orders by engine firing order and keeps source", () => {
    const preview = { rule_states: {
      "address:usaddress-canonical": { enabled: true, source: "default" },
      "phone:libphonenumber-e164": { enabled: false, source: "profile" },
      "name:whitespace": { enabled: true, source: "run" },
    } };
    const rows = ruleStateRows(preview);
    expect(rows.map((r) => r.rule)).toEqual([
      "name:whitespace", "phone:libphonenumber-e164",
      "address:usaddress-canonical"]);
    expect(rows[1]).toEqual({
      rule: "phone:libphonenumber-e164", enabled: false, source: "profile" });
    expect(ruleStateRows(null)).toEqual([]);
  });

  test("heldChangeRows flattens held buckets like applied ones", () => {
    const preview = { held_changes: { phone: [
      { row: 2, field: "phone", before: "(512) 555-0134",
        after: "+15125550134", rule: "phone:libphonenumber-e164",
        confidence: 1 },
    ] } };
    const rows = heldChangeRows(preview);
    expect(rows).toHaveLength(1);
    expect(rows[0].field).toBe("phone");
    expect(heldChangeRows(null)).toEqual([]);
  });

  test("trend needs real runs in at least two weeks", () => {
    const wk = (files) => ({ week: "2026-W40", week_start: "2026-10-05", files });
    expect(hasWeeklyTrend(null)).toBe(false);
    expect(hasWeeklyTrend([wk(0), wk(0)])).toBe(false);
    expect(hasWeeklyTrend([wk(0), wk(3)])).toBe(false);
    expect(hasWeeklyTrend([wk(1), wk(0), wk(2)])).toBe(true);
  });

  test("sparkPoints scales to the max and refuses a fake trend", () => {
    const wk = (files) => ({ week: "2026-W40", week_start: "2026-10-05", files });
    expect(sparkPoints([wk(0), wk(2)])).toBeNull();
    const pts = sparkPoints([wk(1), wk(3), wk(2)], 100, 20, 2);
    const pairs = pts.split(" ").map((p) => p.split(",").map(Number));
    expect(pairs).toHaveLength(3);
    expect(pairs[1][1]).toBe(2);            // max value touches the top pad
    expect(pairs[0][1]).toBeGreaterThan(pairs[1][1]);
  });
});
