// ledger values are integer cents; dashboard tiles are dollars
//
// Multi-currency (#16): formatting goes through Intl.NumberFormat in the
// document's or workspace's currency — USD/en-US is no longer hardcoded.
// `homeCurrency` defaults to USD until a page learns the workspace currency
// (from /fx/rates) and calls setHomeCurrency(); an explicit per-call
// currency always wins.
let homeCurrency = "USD";
export const setHomeCurrency = (c) => { if (c) homeCurrency = String(c).toUpperCase(); };
export const getHomeCurrency = () => homeCurrency;

const fmtAmount = (value, currency) => {
  try {
    return Number(value || 0).toLocaleString("en-US", {
      style: "currency", currency: currency || homeCurrency,
    });
  } catch {
    return Number(value || 0).toLocaleString("en-US", { style: "currency", currency: "USD" });
  }
};

export const money = (cents, currency) => fmtAmount((cents || 0) / 100, currency);

export const dollars = (d, currency) => fmtAmount(d, currency);

export const fmtDate = (s) => {
  if (!s) return "—";
  try {
    return new Date(s + (s.length === 10 ? "T00:00:00" : "")).toLocaleDateString("en-US", {
      month: "short", day: "numeric", year: "numeric",
    });
  } catch { return s; }
};

export const today = () => new Date().toISOString().slice(0, 10);
export const monthStart = () => new Date().toISOString().slice(0, 7) + "-01";
