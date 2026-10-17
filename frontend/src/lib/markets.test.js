import { asMarketList, findMarket, asSkin } from "./markets";

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
