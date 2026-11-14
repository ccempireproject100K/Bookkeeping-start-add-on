import {
  TOURS, TOUR_IDS, ALL_TOURS, FULL_TOUR_ID,
  initialTourState, tourReducer, toursAutoStart,
} from "./tours";

test("every topic tour exists with real steps", () => {
  expect(TOUR_IDS).toEqual(expect.arrayContaining([
    "create-send", "read-rundown", "autopay",
    "design-shop", "rundown-frequency",
  ]));
  for (const id of TOUR_IDS) {
    const t = TOURS[id];
    expect(t.title).toBeTruthy();
    expect(t.steps.length).toBeGreaterThanOrEqual(3);
    for (const s of t.steps) {
      expect(typeof s.title).toBe("string");
      expect(s.text.length).toBeGreaterThan(10);
      // target is either null (centered card) or a CSS selector string
      expect(s.target === null || typeof s.target === "string").toBe(true);
    }
  }
});

test("full walkthrough concatenates every topic with sections", () => {
  const full = ALL_TOURS[FULL_TOUR_ID];
  const expected = TOUR_IDS.reduce((n, id) => n + TOURS[id].steps.length, 0);
  expect(full.steps.length).toBe(expected);
  const sections = new Set(full.steps.map((s) => s.section));
  for (const id of TOUR_IDS) expect(sections.has(TOURS[id].title)).toBe(true);
});

test("tours never auto-start", () => {
  expect(toursAutoStart()).toBe(false);
  expect(initialTourState.active).toBe(false);
});

test("reducer: start / next / back / exit, finish closes the tour", () => {
  let s = tourReducer(initialTourState, { type: "start", tourId: "autopay" });
  expect(s).toEqual({ active: true, tourId: "autopay", step: 0 });
  s = tourReducer(s, { type: "next" });
  expect(s.step).toBe(1);
  s = tourReducer(s, { type: "back" });
  expect(s.step).toBe(0);
  // walk to the end: next on the last step closes the tour
  const last = TOURS.autopay.steps.length - 1;
  s = { active: true, tourId: "autopay", step: last };
  expect(tourReducer(s, { type: "next" })).toEqual(initialTourState);
  // unknown tour never starts; exit always closes
  expect(tourReducer(initialTourState, { type: "start", tourId: "nope" }))
    .toEqual(initialTourState);
  expect(tourReducer({ active: true, tourId: "autopay", step: 1 },
    { type: "exit" })).toEqual(initialTourState);
});
