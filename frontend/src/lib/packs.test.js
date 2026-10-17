import { asPackList, findPack, asFace } from "./packs";

// The payload that broke production: with a misconfigured API base the
// SPA's own index.html came back as "data" (a string). The old code fed
// it straight into .map/.find and React crashed to a black screen.
// These guards must turn that payload into the pages' error/fallback
// states instead.
const HTML_PAGE = "<!doctype html><html><head><title>Mind at Ease Solutions</title></head><body></body></html>";

const PACKS = [
  { id: "plumbing", trade: "Plumbing", product_name: "The Plumber's Ledger" },
  { id: "hvac", trade: "HVAC", product_name: "The HVAC Ledger" },
  { id: "barbershop", trade: "Barbershop", product_name: "The Barber's Ledger" },
  { id: "electrical", trade: "Electrical", product_name: "The Electrician's Ledger" },
];

describe("asPackList", () => {
  test("passes a real array through", () => {
    expect(asPackList(PACKS)).toBe(PACKS);
  });

  test("rejects the HTML-string payload from the incident", () => {
    expect(asPackList(HTML_PAGE)).toBeNull();
  });

  test("rejects objects and nullish values", () => {
    expect(asPackList({ id: "plumbing" })).toBeNull();
    expect(asPackList(null)).toBeNull();
    expect(asPackList(undefined)).toBeNull();
  });
});

describe("findPack", () => {
  test("finds each pack by the slug the SPA routes on", () => {
    for (const p of PACKS) expect(findPack(PACKS, p.id)).toBe(p);
  });

  test("returns null for an unknown slug (drives the fallback page)", () => {
    expect(findPack(PACKS, "not-a-trade")).toBeNull();
  });

  test("returns null instead of throwing on the incident payload", () => {
    expect(() => findPack(HTML_PAGE, "plumbing")).not.toThrow();
    expect(findPack(HTML_PAGE, "plumbing")).toBeNull();
    expect(findPack(null, "plumbing")).toBeNull();
    expect(findPack(undefined, "plumbing")).toBeNull();
  });
});

describe("asFace", () => {
  test("passes a pack object through", () => {
    const face = { niche_id: "hvac", product_name: "The HVAC Ledger" };
    expect(asFace(face)).toBe(face);
  });

  test("rejects strings, arrays and nullish values", () => {
    expect(asFace(HTML_PAGE)).toBeNull();
    expect(asFace([1, 2])).toBeNull();
    expect(asFace(null)).toBeNull();
    expect(asFace(undefined)).toBeNull();
  });
});
