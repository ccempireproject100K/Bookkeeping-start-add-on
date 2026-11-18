/**
 * Client-page invoice-history tiles — pure helpers (owner design order
 * 2026-11-14): pick-by-date filtering, date sorting, and the privacy
 * (hide) mode masking rule.
 *
 * Privacy rule: default card faces carry NO account or payment details.
 * In hide mode, amounts stay masked until the viewer deliberately taps
 * that one card to reveal it — revealing one card never reveals others.
 */
import { money } from "./format";

/** Keep only invoices issued on the picked date (YYYY-MM-DD); "" = all. */
export function filterHistoryByDate(invs, dateStr) {
  if (!dateStr) return [...invs];
  return invs.filter((i) => (i.created_at || "").slice(0, 10) === dateStr);
}

/** Sort by issue date; dir "desc" = newest first, "asc" = oldest first. */
export function sortHistory(invs, dir = "desc") {
  const out = [...invs];
  out.sort((a, b) => {
    const da = a.created_at || "";
    const db = b.created_at || "";
    return dir === "asc" ? da.localeCompare(db) : db.localeCompare(da);
  });
  return out;
}

/** Should this card's amounts be masked right now? */
export function shouldMask(hideMode, revealedIds, invoiceId) {
  return !!hideMode && !(revealedIds || []).includes(invoiceId);
}

/** Money for a card face — masked faces show bullets, never digits. */
export function faceMoney(cents, masked) {
  return masked ? "••••" : money(cents);
}
