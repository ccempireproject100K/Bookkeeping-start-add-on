import {
  filterHistoryByDate, sortHistory, shouldMask, faceMoney,
} from "./historyCards";

const INVS = [
  { id: "a", created_at: "2026-11-10T09:00:00+00:00", total_cents: 5000 },
  { id: "b", created_at: "2026-11-12T09:00:00+00:00", total_cents: 7500 },
  { id: "c", created_at: "2026-11-12T18:30:00+00:00", total_cents: 2500 },
];

test("pick by date keeps only that day's invoices", () => {
  expect(filterHistoryByDate(INVS, "2026-11-12").map((i) => i.id))
    .toEqual(["b", "c"]);
  expect(filterHistoryByDate(INVS, "2026-11-11")).toEqual([]);
  expect(filterHistoryByDate(INVS, "")).toHaveLength(3);
});

test("sort by date both ways, input not mutated", () => {
  expect(sortHistory(INVS, "desc").map((i) => i.id)).toEqual(["c", "b", "a"]);
  expect(sortHistory(INVS, "asc").map((i) => i.id)).toEqual(["a", "b", "c"]);
  expect(INVS.map((i) => i.id)).toEqual(["a", "b", "c"]);
});

test("hide mode masks until that one card is revealed", () => {
  expect(shouldMask(false, [], "a")).toBe(false);      // hide off: visible
  expect(shouldMask(true, [], "a")).toBe(true);        // hide on: masked
  expect(shouldMask(true, ["a"], "a")).toBe(false);    // tapped: revealed
  expect(shouldMask(true, ["a"], "b")).toBe(true);     // others stay masked
});

test("masked faces show bullets, never the amount", () => {
  expect(faceMoney(12345, true)).toBe("••••");
  expect(faceMoney(12345, true)).not.toContain("123");
  expect(faceMoney(12345, false)).toBe("$123.45");
});
