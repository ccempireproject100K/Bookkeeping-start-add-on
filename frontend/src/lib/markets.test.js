import {
  asMarketList, findMarket, asSkin,
  readMarketChoice, writeMarketChoice, writeNoMarketChoice,
  resolveVisitorMarket, MARKET_STORAGE_KEY, NO_MARKET_CHOICE,
  DEFAULT_MARKET_CODE,
} from "./markets";

const MX = { code: "MX", name: "Mexico", flag: "🇲🇽", colors: { accent: "#006847" }, languages: ["es"], currency: "MXN" };

describe("asMarketList", () => {
  it("accepts a real array", () => {
    expect(asMarketList([MX])).toEqual([MX]);
  });
  it("rejects non-arrays (e.g. an HTML page accepted as data)", () => {
    expect(asMarketList("<html>…</html>")).toBeNull();
    expect(asMarketList({})).toBeNull();
    expect(asMarketList(null)).toBeNull();
  });
});

describe("findMarket", () => {
  it("finds by exact code", () => {
    expect(findMarket([MX], "MX")).toEqual(MX);
  });
  it("returns null for unknown codes and bad payloads", () => {
    expect(findMarket([MX], "XX")).toBeNull();
    expect(findMarket("nope", "MX")).toBeNull();
    expect(findMarket([MX], "")).toBeNull();
  });
});

describe("asSkin", () => {
  it("accepts a composed skin (trade + market objects)", () => {
    const skin = { trade_id: "plumbing", market_code: "MX", trade: { id: "plumbing" }, market: MX, accent: "#006847" };
    expect(asSkin(skin)).toEqual(skin);
  });
  it("rejects partial or wrong-shaped payloads", () => {
    expect(asSkin({ trade: { id: "plumbing" } })).toBeNull();
    expect(asSkin([1, 2])).toBeNull();
    expect(asSkin(null)).toBeNull();
  });
});

// Market-face default ruling (owner, 2026-11-01): stored choice >
// detected country > US default; a non-US face is never worn without
// the visitor's choice or a detection behind it.
describe("resolveVisitorMarket (precedence)", () => {
  beforeEach(() => window.localStorage.clear());

  const detectApi = (payload) => ({ get: jest.fn(() => Promise.resolve(payload)) });

  it("a stored choice wins and detection is never consulted", async () => {
    writeMarketChoice("GB");
    const api = detectApi({ market_code: "DE", source: "detected" });
    await expect(resolveVisitorMarket(api)).resolves.toEqual({ code: "GB", source: "choice" });
    expect(api.get).not.toHaveBeenCalled();
  });

  it("an explicit no-market pick sticks (sentinel, source none)", async () => {
    writeNoMarketChoice();
    expect(window.localStorage.getItem(MARKET_STORAGE_KEY)).toBe(NO_MARKET_CHOICE);
    const api = detectApi({ market_code: "GB", source: "detected" });
    await expect(resolveVisitorMarket(api)).resolves.toEqual({ code: "", source: "none" });
    expect(api.get).not.toHaveBeenCalled();
    // …and reads as "no choice" for signup/login, as before.
    expect(readMarketChoice()).toBe("");
  });

  it("no stored choice: a detected packed country wins over the default", async () => {
    const api = detectApi({ market_code: "GB", source: "detected" });
    await expect(resolveVisitorMarket(api)).resolves.toEqual({ code: "GB", source: "detected" });
    expect(api.get).toHaveBeenCalledWith("/market-packs/detect");
  });

  it("no stored choice and no detection: the US market, source default", async () => {
    const api = detectApi({ market_code: "US", source: "default" });
    await expect(resolveVisitorMarket(api)).resolves.toEqual({ code: "US", source: "default" });
    expect(DEFAULT_MARKET_CODE).toBe("US");
  });

  it("detection failures and garbage payloads still land on US default", async () => {
    const down = { get: jest.fn(() => Promise.reject(new Error("down"))) };
    await expect(resolveVisitorMarket(down)).resolves.toEqual({ code: "US", source: "default" });
    const garbage = detectApi("<html>nope</html>");
    await expect(resolveVisitorMarket(garbage)).resolves.toEqual({ code: "US", source: "default" });
  });
});
