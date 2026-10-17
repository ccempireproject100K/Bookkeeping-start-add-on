// Market-face helpers: response-shape guards + the visitor's market
// choice (localStorage), shared by the public pages and signup.
//
// API contract: GET /market-packs returns an ARRAY of market objects
// ({code, name, flag, colors, languages, currency, extended_network});
// GET /market-packs/resolve returns ONE composed skin object. Same
// rule as lib/packs.js: anything else degrades to "no market", never
// reaches render code as if it were data.

export const MARKET_STORAGE_KEY = "mae_market_code";

export function asMarketList(data) {
  return Array.isArray(data) ? data : null;
}

export function findMarket(data, code) {
  const list = asMarketList(data);
  if (!list || !code) return null;
  return list.find((m) => m && m.code === code) || null;
}

export function asSkin(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  if (!data.trade || !data.market) return null;
  return data;
}

export function readMarketChoice() {
  try {
    return window.localStorage.getItem(MARKET_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function writeMarketChoice(code) {
  try {
    if (code) window.localStorage.setItem(MARKET_STORAGE_KEY, code);
    else window.localStorage.removeItem(MARKET_STORAGE_KEY);
  } catch {
    /* storage unavailable — the choice just won't persist */
  }
}
