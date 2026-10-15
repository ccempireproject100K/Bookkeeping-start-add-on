// ledger values are integer cents; dashboard tiles are dollars
export const money = (cents) =>
  Number((cents || 0) / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });

export const dollars = (d) =>
  Number(d || 0).toLocaleString("en-US", { style: "currency", currency: "USD" });

// Multi-currency Phase 4: format a x100-stored amount in any supported
// currency. Intl handles per-currency decimals automatically (JPY: no cents).
export const moneyFor = (cents, currency = "USD") => {
  const code = String(currency || "USD").toUpperCase();
  try {
    return Number((cents || 0) / 100).toLocaleString("en-US", { style: "currency", currency: code });
  } catch {
    return money(cents); // unknown code -> fall back to USD formatting
  }
};

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
