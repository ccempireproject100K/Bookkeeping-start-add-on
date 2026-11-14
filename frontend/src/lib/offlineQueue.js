/**
 * Offline field-capture queue (feat/offline-field-capture).
 *
 * The PWA's standing rule was "nothing is queued silently." This queue is
 * the opposite of silent: it exists ONLY for field capture (time entries,
 * receipt photos, quick expense notes), every item in it is listed in the
 * sync-status panel with plain-words labels and per-item retry/discard,
 * and each item carries a stable id that doubles as the server-side
 * idempotency key (client_key) — a retried sync returns the record the
 * server already has instead of creating a duplicate.
 *
 * Money movement never queues: invoice sends, payments and everything
 * else stay online-only and keep the service worker's honest 503.
 *
 * Storage is IndexedDB in the browser (survives reload/restart); the
 * store is pluggable so tests can drive the same engine in memory.
 */

export const QUEUE_KINDS = ["time_entry", "time_clock_in", "time_clock_out", "receipt", "expense_note"];

let counter = 0;
export function newItemId() {
  counter += 1;
  const rand =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Date.now().toString(36);
  return `fc_${rand}_${counter}`;
}

/* ---------------- Stores ---------------- */

export function createMemoryStore() {
  const map = new Map();
  return {
    async getAll() {
      return [...map.values()].map((r) => ({ ...r }));
    },
    async put(record) {
      map.set(record.id, { ...record });
    },
    async remove(id) {
      map.delete(id);
    },
  };
}

export function createIdbStore(dbName = "mae-books-offline", storeName = "field_capture") {
  let dbPromise = null;
  const open = () => {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        const req = indexedDB.open(dbName, 1);
        req.onupgradeneeded = () => {
          if (!req.result.objectStoreNames.contains(storeName)) {
            req.result.createObjectStore(storeName, { keyPath: "id" });
          }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
    }
    return dbPromise;
  };
  const tx = async (mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(storeName, mode);
      const store = t.objectStore(storeName);
      let result;
      t.oncomplete = () => resolve(result);
      t.onerror = () => reject(t.error);
      result = fn(store);
      // getAll-style requests resolve through their own onsuccess below.
      if (result && typeof result === "object" && "onsuccess" in result) {
        const req = result;
        req.onsuccess = () => resolve(req.result);
      }
    });
  };
  return {
    async getAll() {
      const rows = await tx("readonly", (s) => s.getAll());
      return rows || [];
    },
    async put(record) {
      await tx("readwrite", (s) => s.put(record));
    },
    async remove(id) {
      await tx("readwrite", (s) => s.delete(id));
    },
  };
}

/* ---------------- Plain-words labels ---------------- */

export function describeItem(item) {
  const p = item.payload || {};
  switch (item.kind) {
    case "time_entry": {
      const hrs = p.hours != null ? `${p.hours}h` : "time";
      const who = p.client_name ? ` for ${p.client_name}` : "";
      return `Time entry — ${hrs}${who} (${p.date || ""})`;
    }
    case "time_clock_in":
      return `Clock-in${p.client_name ? ` — ${p.client_name}` : ""}`;
    case "time_clock_out":
      return "Clock-out";
    case "receipt": {
      const bits = ["Receipt photo"];
      if (p.vendor) bits.push(p.vendor);
      if (p.amount) bits.push(`$${p.amount}`);
      return bits.join(" — ");
    }
    case "expense_note": {
      const bits = ["Expense note"];
      if (p.amount) bits.push(`$${p.amount}`);
      if (p.note) bits.push(p.note);
      return bits.join(" — ");
    }
    default:
      return item.kind;
  }
}

/* ---------------- The queue engine ---------------- */

function isNetworkError(err) {
  if (!err) return true;
  if (err.network) return true;
  const status = err.status ?? err.response?.status;
  if (status == null) return true; // axios network failure has no response
  return status === 503 || status === 0;
}

function errDetail(err) {
  const d = err?.response?.data?.detail ?? err?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join(" ");
  return err?.message || "Send failed";
}

function errStatus(err) {
  return err?.status ?? err?.response?.status ?? null;
}

/**
 * transport: {
 *   postTimeEntry(payload) -> Promise<record>,
 *   postClockIn(payload)   -> Promise<record>,
 *   postClockOut(payload)  -> Promise<record>,
 *   uploadReceipt(blob, filename, clientKey, notes) -> Promise<record>,
 *   postExpense(payload)   -> Promise<record>,
 * }
 * Payloads receive `client_key: item.id` merged in by the engine.
 */
export function createQueue({ store, transport, now = () => new Date().toISOString() }) {
  let items = [];
  let syncing = false;
  const listeners = new Set();

  const emit = () => {
    const snapshot = queue.snapshot();
    listeners.forEach((fn) => {
      try { fn(snapshot); } catch { /* a broken listener must not break sync */ }
    });
  };

  // Records are stored as structured clones (IndexedDB keeps Blobs in
  // receipt payloads as-is); we copy the plain shell so callers can't
  // mutate stored state by reference.
  const save = async (item) => {
    const copy = { ...item, payload: { ...item.payload }, progress: { ...item.progress } };
    await store.put(copy);
    const idx = items.findIndex((i) => i.id === item.id);
    if (idx >= 0) items[idx] = copy; else items.push(copy);
    emit();
  };

  const queue = {
    async load() {
      items = (await store.getAll()).sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
      emit();
      return items;
    },

    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },

    snapshot() {
      const counts = { waiting: 0, syncing: 0, attention: 0, failed: 0, total: items.length };
      for (const i of items) {
        if (i.state === "waiting") counts.waiting += 1;
        else if (i.state === "syncing") counts.syncing += 1;
        else if (i.state === "attention") counts.attention += 1;
        else if (i.state === "failed") counts.failed += 1;
      }
      return { items: [...items], counts, syncing };
    },

    async enqueue(kind, payload) {
      if (!QUEUE_KINDS.includes(kind)) throw new Error(`Unknown queue kind: ${kind}`);
      const item = {
        id: newItemId(),
        kind,
        payload,
        state: "waiting",
        created_at: now(),
        attempts: 0,
        error: null,
        note: null,
        progress: {},
      };
      await save(item);
      return item;
    },

    async discard(id) {
      items = items.filter((i) => i.id !== id);
      await store.remove(id);
      emit();
    },

    async retry(id) {
      const item = items.find((i) => i.id === id);
      if (!item) return;
      item.state = "waiting";
      item.error = null;
      item.note = null;
      await save(item);
      await queue.syncNow();
    },

    /** One sync pass over waiting items, oldest first. Returns a summary. */
    async syncNow() {
      if (syncing) return { sent: 0, attention: 0, failed: 0, blocked: true };
      syncing = true;
      emit();
      const summary = { sent: 0, attention: 0, failed: 0, offline: false };
      try {
        const due = items
          .filter((i) => i.state === "waiting")
          .sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
        for (const item of due) {
          item.state = "syncing";
          item.attempts += 1;
          await save(item);
          try {
            await deliver(item, transport, async () => save(item));
            await queue.discard(item.id); // landed — remove from the queue
            summary.sent += 1;
          } catch (err) {
            if (isNetworkError(err)) {
              item.state = "waiting";
              item.error = null;
              await save(item);
              summary.offline = true;
              break; // still offline — stop the pass, everything stays visible
            }
            const status = errStatus(err);
            if (status === 401) {
              item.state = "waiting";
              item.error = "Waiting for sign-in — this will send once you're signed back in.";
              await save(item);
              summary.offline = true;
              break;
            }
            if (item.kind === "time_clock_in" && status === 409) {
              // SERVER WINS: an open shift already exists server-side
              // (e.g. clocked in from the office). Never overwrite it.
              item.state = "attention";
              item.error =
                "You're already clocked in on the server, so this phone clock-in wasn't sent. " +
                "The server's shift is the one that counts — discard this, or keep it here and sort it out with your lead.";
              summary.attention += 1;
              await save(item);
              continue;
            }
            if (item.kind === "time_clock_out" && status === 404) {
              item.state = "attention";
              item.error =
                "The server shows no open shift for this clock-out — it may already be recorded, " +
                "or the shift was closed another way. Nothing was changed by this item.";
              summary.attention += 1;
              await save(item);
              continue;
            }
            item.state = "failed";
            item.error = errDetail(err);
            summary.failed += 1;
            await save(item);
          }
        }
      } finally {
        syncing = false;
        emit();
      }
      return summary;
    },
  };

  return queue;
}

async function deliver(item, transport, saveProgress) {
  const p = item.payload || {};
  const key = { client_key: item.id };
  switch (item.kind) {
    case "time_entry":
      return transport.postTimeEntry({ ...p, ...key });
    case "time_clock_in":
      return transport.postClockIn({ ...p, ...key });
    case "time_clock_out":
      return transport.postClockOut({ ...key });
    case "receipt":
      return transport.uploadReceipt(p.blob, p.filename, item.id, {
        vendor: p.vendor || "",
        amount: p.amount || "",
        date: p.date || "",
      });
    case "expense_note": {
      // Two steps, both idempotent: the photo (if any) lands in the
      // Receipt Inbox first; progress is remembered so a retry after the
      // photo landed only sends the expense itself.
      let receiptId = item.progress?.receipt_id || null;
      if (p.photoBlob && !receiptId) {
        const rec = await transport.uploadReceipt(p.photoBlob, p.photoName || "expense-photo.jpg", `${item.id}:photo`, {});
        receiptId = rec?.id || "uploaded";
        item.progress = { ...item.progress, receipt_id: receiptId };
        if (saveProgress) await saveProgress(); // a retry now skips the photo step
      }
      return transport.postExpense({
        vendor: p.vendor || "Field expense",
        amount: parseFloat(p.amount),
        description: p.note || "",
        expense_date: p.date || undefined,
        category_code: p.category_code || "5700",
        payment_method: p.payment_method || "bank",
        ...key,
      });
    }
    default:
      throw new Error(`Unknown queue kind: ${item.kind}`);
  }
}
