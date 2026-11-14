/**
 * Field-capture transport: how queued items reach the real Books API.
 *
 * Every call carries the queue item's id as `client_key`; the backend
 * (time entries, clock in/out, receipt upload, vendor expenses) dedupes
 * on it, so re-sending an item after a dropped connection returns the
 * record that already landed instead of creating a second one.
 */
import { api } from "@/lib/api";

export const apiTransport = {
  postTimeEntry: (payload) => api.post("/time/entries", payload),
  postClockIn: (payload) => api.post("/time/clock-in", payload),
  postClockOut: (payload) => api.post("/time/clock-out", payload),
  postExpense: (payload) => api.post("/vendors/expenses", payload),
  postStockScan: (payload) => api.post("/inventory/scan", payload),

  async uploadReceipt(blob, filename, clientKey /* , notes */) {
    const fd = new FormData();
    fd.append("file", blob, filename || "receipt.jpg");
    if (clientKey) fd.append("client_key", clientKey);
    return api.post("/receipts/upload", fd);
  },
};

/** True when the browser believes it has a connection. */
export function isOnline() {
  return typeof navigator === "undefined" ? true : navigator.onLine !== false;
}
