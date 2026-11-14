import {
  HELP_LINKS, HELP_TOPICS, HELP_LABEL, groupHelpLinks,
  HELP_VIDEOS, HELP_VIDEO_TOPICS, groupHelpVideos, isYouTubeUrl,
} from "./helpLinks";

test("shipped config: the seven starter resources, all https", () => {
  expect(HELP_LINKS).toHaveLength(7);
  const names = HELP_LINKS.map((l) => l.name).join(" | ");
  for (const org of ["SBA", "SCORE", "SBDC", "FDIC", "IRS", "CFPB", "MyMoney"]) {
    expect(names).toContain(org);
  }
  for (const l of HELP_LINKS) {
    expect(l.url.startsWith("https://")).toBe(true);
    expect(l.blurb.length).toBeGreaterThan(20);
    expect(HELP_TOPICS.map((t) => t.id)).toContain(l.topic);
  }
});

test("the four owner topics exist", () => {
  const labels = HELP_TOPICS.map((t) => t.label);
  expect(labels).toEqual(expect.arrayContaining([
    "Cash flow & getting paid", "Taxes", "Credit & borrowing",
    "Starting & running a business",
  ]));
});

test("grouping renders every link under its topic, in topic order", () => {
  const groups = groupHelpLinks();
  expect(groups.map((g) => g.id)).toEqual(HELP_TOPICS.map((t) => t.id));
  const total = groups.reduce((n, g) => n + g.links.length, 0);
  expect(total).toBe(HELP_LINKS.length);
  for (const g of groups) {
    for (const l of g.links) expect(l.topic).toBe(g.id);
  }
});

test("honest label is the shipped wording", () => {
  expect(HELP_LABEL).toContain("Free public resources");
  expect(HELP_LABEL).toContain("isn't financial advice");
});

test("videos: core set of at least 6, every topic covered (6–9 is the core, not a cap — owner 2026-11-14)", () => {
  expect(HELP_VIDEOS.length).toBeGreaterThanOrEqual(6);
  const covered = new Set(HELP_VIDEOS.map((v) => v.topic));
  for (const t of HELP_VIDEO_TOPICS) expect(covered.has(t.id)).toBe(true);
  expect(HELP_VIDEO_TOPICS.map((t) => t.id)).toEqual(expect.arrayContaining([
    "first-invoice", "payment-terms", "late-payment", "deposits",
    "records-cashflow",
  ]));
});

test("videos: every configured URL is an https YouTube link", () => {
  for (const v of HELP_VIDEOS) {
    expect(isYouTubeUrl(v.url)).toBe(true);
    expect(v.title.length).toBeGreaterThan(5);
    expect(v.source.length).toBeGreaterThan(3);
    expect(v.blurb.length).toBeGreaterThan(20);
  }
  expect(isYouTubeUrl("http://www.youtube.com/watch?v=x")).toBe(false);
  expect(isYouTubeUrl("https://vimeo.com/123")).toBe(false);
});

test("videos: search fallbacks are honestly labeled as searches", () => {
  for (const v of HELP_VIDEOS.filter((x) => x.kind === "search")) {
    expect(v.url).toContain("youtube.com/results?search_query=");
    expect(v.title.toLowerCase()).toContain("more videos on");
  }
  const specifics = HELP_VIDEOS.filter((x) => x.kind === "video");
  expect(specifics.length).toBeGreaterThanOrEqual(5);
  for (const v of specifics) expect(v.url).toContain("youtube.com/watch?v=");
});

test("videos: grouping renders every entry under its topic", () => {
  const groups = groupHelpVideos();
  const total = groups.reduce((n, g) => n + g.videos.length, 0);
  expect(total).toBe(HELP_VIDEOS.length);
  for (const g of groups) {
    for (const v of g.videos) expect(v.topic).toBe(g.id);
  }
});
