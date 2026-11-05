/**
 * books.receipts — the Phase-1 routing adapter (DESIGN §5.2, §4.1).
 *
 * The adapter does EXACTLY what the Receipts page itself does, through the
 * same `lib/api` client (same axios instance, same session credentials,
 * same tenant scoping — Pip introduces no new privileges, DESIGN §5.6):
 *
 *   stage(file)  →  const fd = new FormData(); fd.append("file", file);
 *                   api.post("/receipts/upload", fd)     — one file per
 *                   request, mirroring pages/Receipts.jsx `upload()`.
 *   scan(id)     →  api.post(`/receipts/${id}/scan`)     — the page's
 *                   `scan()`. EXPLICIT user action only: scanning spends
 *                   vision money, so Pip never calls it on his own (§4.1).
 *
 * There is deliberately NO post/expense method here. Posting lives only
 * on the Receipts page, behind its review dialog — Pip cannot reach it
 * by construction, and receiptsAdapter.test.js pins that.
 */
import { api, errMsg } from "@/lib/api";

export const RECEIPTS_ROUTE = "/receipts";

export function createReceiptsAdapter(client = api) {
  return {
    id: "books.receipts",

    /**
     * Perform the destination's own upload call for ONE file.
     * @param {File} file
     * @returns {Promise<{ id: string|null, status: string }>} the created
     *   receipt record's reference (the upload endpoint returns the
     *   created inbox record, as the page's reload list shows).
     */
    async stage(file) {
      const fd = new FormData();
      fd.append("file", file);
      const res = await client.post("/receipts/upload", fd);
      return { id: res?.id ?? res?.receipt?.id ?? null, status: res?.status || "uploaded" };
    },

    /**
     * Explicit-scan action — mirrors the page's scan call exactly.
     * @param {string} receiptId
     */
    async scan(receiptId) {
      return client.post(`/receipts/${receiptId}/scan`);
    },

    /** Deep link to the canonical record: the Receipts page, row highlighted. */
    deepLink(ref) {
      return ref?.id ? `${RECEIPTS_ROUTE}?receipt=${encodeURIComponent(ref.id)}` : RECEIPTS_ROUTE;
    },
  };
}

/** The one adapter instance the shell uses. */
export const receiptsAdapter = createReceiptsAdapter();

export { errMsg };
