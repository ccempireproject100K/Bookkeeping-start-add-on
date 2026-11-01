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
    const raw = window.localStorage.getItem(MARKET_STORAGE_KEY) || "";
    // The explicit "no market" pick reads as no choice everywhere
    // else (signup stores no market for it, exactly as before).
    return raw === NO_MARKET_CHOICE ? "" : raw;
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

// --- Market-face default ruling (owner, 2026-11-01) -----------------
// No page ever silently wears a non-US market face. Precedence:
// (1) the visitor's own stored choice; (2) the detected country
// (GET /market-packs/detect echoes the edge's CF-IPCountry) when it
// has a pack; (3) the US market. An explicit "no market" pick is a
// stored choice too — it is kept as a sentinel so it sticks instead
// of being re-detected on every visit.

export const DEFAULT_MARKET_CODE = "US";
export const NO_MARKET_CHOICE = "__none__";

function readRawMarketChoice() {
  try {
    return window.localStorage.getItem(MARKET_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function writeNoMarketChoice() {
  try {
    window.localStorage.setItem(MARKET_STORAGE_KEY, NO_MARKET_CHOICE);
  } catch {
    /* storage unavailable — the pick just won't persist */
  }
}

/** Resolve the market face a public page should wear, following the
 *  ruling's precedence. Returns { code, source } — code "" means the
 *  trade-only face (only ever from the visitor's own explicit pick);
 *  source is "choice" | "none" | "detected" | "default" and is shown
 *  to the visitor so a face is never worn without a stated reason. */
export async function resolveVisitorMarket(api) {
  const raw = readRawMarketChoice();
  if (raw === NO_MARKET_CHOICE) return { code: "", source: "none" };
  if (raw) return { code: raw, source: "choice" };
  try {
    const data = await api.get("/market-packs/detect");
    if (data && typeof data.market_code === "string" && data.market_code) {
      return {
        code: data.market_code,
        source: data.source === "detected" ? "detected" : "default",
      };
    }
  } catch {
    /* detection unreachable — fall through to the US default */
  }
  return { code: DEFAULT_MARKET_CODE, source: "default" };
}
