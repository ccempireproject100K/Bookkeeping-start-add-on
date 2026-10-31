import {
  KIND_FIELDS, STUDIO_CAPS, STUDIO_KINDS,
  cleanSummary, dedupeBatch, keyLooksValid, recordsToCsv, resolvePair,
  splitValues,
} from "./datacleanStudio";

describe("kind/field table mirrors the API's validation table", () => {
  test("kinds are customers, vendors, items with the backend's fields", () => {
    expect(STUDIO_KINDS).toEqual(["customers", "vendors", "items"]);
    expect(KIND_FIELDS.customers).toEqual(["name", "email", "phone", "address"]);
    expect(KIND_FIELDS.vendors).toEqual(["name", "email", "phone", "address"]);
    expect(KIND_FIELDS.items).toEqual(["name", "price", "sku", "kind"]);
  });
  test("caps match the API's per-call ceilings", () => {
    expect(STUDIO_CAPS).toEqual({
      maxRows: 5000, maxDedupeRecords: 500, maxNormalizeValues: 1000,
    });
  });
});

describe("keyLooksValid", () => {
  test("accepts the mdck_ shape, rejects everything else", () => {
    expect(keyLooksValid("mdck_AbC123-_xYz9")).toBe(true);
    expect(keyLooksValid("  mdck_AbC123-_xYz9  ")).toBe(true);
    expect(keyLooksValid("maek_AbC123-_xYz9")).toBe(false); // books key ≠ DataClean key
    expect(keyLooksValid("mdck_short")).toBe(false);
    expect(keyLooksValid("")).toBe(false);
    expect(keyLooksValid(null)).toBe(false);
  });
});

describe("recordsToCsv", () => {
  test("header is the kind's fields in order; missing values are empty", () => {
    const csv = recordsToCsv(
      [{ name: "Acme Supply", email: "ops@acme.example" }], "customers");
    expect(csv).toBe("name,email,phone,address\nAcme Supply,ops@acme.example,,\n");
  });
  test("quotes commas, quotes and newlines per RFC 4180", () => {
    const csv = recordsToCsv([{
      name: 'Say "Hi" Co',
      address: "9 Oak Ave, Dallas, TX 75001",
      phone: "+15125550134",
      email: "a@b.example",
    }], "customers");
    expect(csv).toBe(
      "name,email,phone,address\n"
      + '"Say ""Hi"" Co",a@b.example,+15125550134,"9 Oak Ave, Dallas, TX 75001"\n');
  });
  test("items kind uses its own field order; no records is header only", () => {
    expect(recordsToCsv([], "items")).toBe("name,price,sku,kind\n");
    expect(recordsToCsv([{ kind: "part", sku: "X1", name: "Bolt", price: "3" }], "items"))
      .toBe("name,price,sku,kind\nBolt,3,X1,part\n");
  });
});

describe("cleanSummary", () => {
  test("totals come from the payload; rules sort busiest first", () => {
    const s = cleanSummary({
      rows: 3,
      changes: [{}, {}, {}, {}, {}],
      changes_by_rule: { "email:trim-and-lowercase": 2, "phone:libphonenumber-e164": 3 },
    });
    expect(s.rows).toBe(3);
    expect(s.totalChanges).toBe(5);
    expect(s.byRule).toEqual([
      { rule: "phone:libphonenumber-e164", count: 3 },
      { rule: "email:trim-and-lowercase", count: 2 },
    ]);
  });
  test("empty/absent payload is zeros, not invented numbers", () => {
    expect(cleanSummary(null)).toEqual({ rows: 0, totalChanges: 0, byRule: [] });
    expect(cleanSummary({})).toEqual({ rows: 0, totalChanges: 0, byRule: [] });
  });
});

describe("resolvePair", () => {
  const records = [
    { name: "Acme Supply Inc", email: "a@acme.example" },
    { name: "ACME SUPPLY, INC.", email: "a@acme.example" },
    { id: "c9", name: "Named Co" },
  ];
  test("row-N ids resolve to submission order (1-based)", () => {
    const p = resolvePair({ a_id: "row-1", b_id: "row-2", score: 0.93, evidence: { why: "same email" } }, records);
    expect(p.aName).toBe("Acme Supply Inc");
    expect(p.bName).toBe("ACME SUPPLY, INC.");
    expect(p.score).toBe(0.93);
    expect(p.evidence.why).toBe("same email");
  });
  test("a record's own id wins over its row position", () => {
    const p = resolvePair({ a_id: "c9", b_id: "row-1", score: 0.5 }, records);
    expect(p.aName).toBe("Named Co");
    expect(p.bName).toBe("Acme Supply Inc");
  });
  test("unknown ids fall back to the raw id with null records", () => {
    const p = resolvePair({ a_id: "zzz", b_id: "row-9" }, records);
    expect(p.a).toBeNull();
    expect(p.aName).toBe("zzz");
    expect(p.bName).toBe("row-9");
  });
});

describe("dedupeBatch", () => {
  test("projects onto the kind's flat fields — engine helper keys never ship", () => {
    const recs = [{
      name: "Acme Supply", email: "ops@acme.example", phone: "+15125550134",
      address: "9 Oak Ave", name_key: "acme supply",
      address_parts: { zip: "75001" },
    }];
    expect(dedupeBatch(recs, "customers")).toEqual({
      records: [{ name: "Acme Supply", email: "ops@acme.example", phone: "+15125550134", address: "9 Oak Ave" }],
      truncated: false,
    });
  });
  test("drops empty fields and caps at the API's 500, flagging truncation", () => {
    const recs = Array.from({ length: 501 }, (_, i) => ({ name: `Co ${i}`, email: "" }));
    const out = dedupeBatch(recs, "customers");
    expect(out.records).toHaveLength(500);
    expect(out.records[0]).toEqual({ name: "Co 0" });
    expect(out.truncated).toBe(true);
  });
});

describe("splitValues", () => {
  test("one value per line, blanks dropped, trimmed", () => {
    expect(splitValues(" (512) 555-0134 \n\n512.555.0142\n"))
      .toEqual(["(512) 555-0134", "512.555.0142"]);
    expect(splitValues("")).toEqual([]);
    expect(splitValues(null)).toEqual([]);
  });
});
