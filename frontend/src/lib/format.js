// ledger values are integer cents; dashboard tiles are dollars
// All formatters follow the app's active language (default en-US).
import { getActiveLocale } from "@/lib/i18n";

export const money = (cents) =>
  Number((cents || 0) / 100).toLocaleString(getActiveLocale(), { style: "currency", currency: "USD" });

export const dollars = (d) =>
  Number(d || 0).toLocaleString(getActiveLocale(), { style: "currency", currency: "USD" });

// Multi-currency Phase 4: format a x100-stored amount in any supported
// currency. Intl handles per-currency decimals automatically (JPY: no cents).
export const moneyFor = (cents, currency = "USD") => {
  const code = String(currency || "USD").toUpperCase();
  try {
    return Number((cents || 0) / 100).toLocaleString(getActiveLocale(), { style: "currency", currency: code });
  } catch {
    return money(cents); // unknown code -> fall back to USD formatting
  }
};

export const num = (n, opts = {}) =>
  Number(n || 0).toLocaleString(getActiveLocale(), opts);

export const pct = (n, digits = 1) =>
  Number(n || 0).toLocaleString(getActiveLocale(), {
    style: "percent",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });

export const fmtDate = (s) => {
  if (!s) return "—";
  try {
    return new Date(s + (s.length === 10 ? "T00:00:00" : "")).toLocaleDateString(getActiveLocale(), {
      month: "short", day: "numeric", year: "numeric",
    });
  } catch { return s; }
};

export const fmtDateTime = (s) => {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString(getActiveLocale(), {
      month: "short", day: "numeric", year: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  } catch { return s; }
};

export const today = () => new Date().toISOString().slice(0, 10);
export const monthStart = () => new Date().toISOString().slice(0, 7) + "-01";
