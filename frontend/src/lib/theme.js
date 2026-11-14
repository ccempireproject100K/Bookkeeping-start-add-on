/**
 * Client-page (Bookkeeping InvoiceEase) light/dark theme preference.
 * Dark is the delivered default; a visitor's explicit choice is persisted
 * in localStorage and wins on later visits.
 */
export const CLIENT_THEME_KEY = "bk-invoiceease-client-theme";

export function getStoredClientTheme(storage) {
  try {
    return storage && storage.getItem(CLIENT_THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function storeClientTheme(storage, theme) {
  try {
    if (storage) storage.setItem(CLIENT_THEME_KEY, theme === "light" ? "light" : "dark");
  } catch {
    /* storage unavailable (private mode etc) — theme just won't persist */
  }
}

export function nextClientTheme(theme) {
  return theme === "light" ? "dark" : "light";
}
