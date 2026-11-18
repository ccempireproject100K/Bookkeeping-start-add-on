/**
 * Offline field-capture queue (feat/offline-field-capture).
 *
 * Engine-level tests against a fake server that mirrors the real
 * backend's client_key contract: the server stores one record per
 * client_key and returns the existing record on a repeat send. That is
 * what makes "sync the same item twice → one server record" provable
 * here the same way the backend tests prove it on the server side.
 */
import { createQueue, createMemoryStore, describeItem, QUEUE_KINDS } from "@/lib/offlineQueue";

function netErr() {
  const e = new Error("Network Error");
  e.network = true;
  return e;
}
function httpErr(status, detail) {
  const e = new Error(detail || `HTTP ${status}`);
  e.response = { status, data: { detail } };
  return e;
}

/** A fake Books server with the backend's dedupe-by-client_key behavior. */
function makeServer() {
  const state = {
    entries: new Map(), clockIns: new Map(), receipts: new Map(), expenses: new Map(),
    openShift: null, dropNextResponse: false, failNext: null, calls: [],
  };
  const maybeDrop = () => {
    if (state.dropNextResponse) { state.dropNextResponse = false; throw netErr(); }
    if (state.failNext) { const f = state.failNext; state.failNext = null; throw f; }
  };
  const transport = {
    async postTimeEntry(p) {
      state.calls.push(["time_entry", p.client_key]);
      if (state.entries.has(p.client_key)) return state.entries.get(p.client_key);
      const rec = { id: `srv-${state.entries.size + 1}`, ...p };
      state.entries.set(p.client_key, rec);
      maybeDrop(); // record landed; the response may still be lost
      return rec;
    },
    async postClockIn(p) {
      state.calls.push(["clock_in", p.client_key]);
      if (state.clockIns.has(p.client_key)) return state.clockIns.get(p.client_key);
      if (state.openShift) throw httpErr(409, "Already clocked in");
      const rec = { id: `shift-${state.clockIns.size + 1}`, ...p, status: "open" };
      state.clockIns.set(p.client_key, rec);
      state.openShift = rec;
      maybeDrop();
      return rec;
    },
    async postClockOut(p) {
      state.calls.push(["clock_out", p.client_key]);
      if (!state.openShift) throw httpErr(404, "Not clocked in");
      const rec = { ...state.openShift, status: "logged" };
      state.openShift = null;
      maybeDrop();
      return rec;
    },
    async uploadReceipt(blob, filename, clientKey) {
      state.calls.push(["receipt", clientKey]);
      if (state.receipts.has(clientKey)) return state.receipts.get(clientKey);
      const rec = { id: `rcpt-${state.receipts.size + 1}`, filename, client_key: clientKey };
      state.receipts.set(clientKey, rec);
      maybeDrop();
      return rec;
    },
    async postExpense(p) {
      state.calls.push(["expense", p.client_key]);
      if (state.expenses.has(p.client_key)) return state.expenses.get(p.client_key);
      if (!(p.amount > 0)) throw httpErr(422, "Amount must be positive");
      const rec = { id: `exp-${state.expenses.size + 1}`, ...p };
      state.expenses.set(p.client_key, rec);
      maybeDrop();
      return rec;
    },
  };
  return { state, transport };
}

const deadTransport = {
  postTimeEntry: async () => { throw netErr(); },
  postClockIn: async () => { throw netErr(); },
  postClockOut: async () => { throw netErr(); },
  uploadReceipt: async () => { throw netErr(); },
  postExpense: async () => { throw netErr(); },
};

describe("queue scope", () => {
  test("only field capture kinds exist — no money movement can queue", () => {
    expect(QUEUE_KINDS).toEqual(["time_entry", "time_clock_in", "time_clock_out", "receipt", "expense_note"]);
  });
  test("enqueue rejects an invoice send (online-only action)", async () => {
    const q = createQueue({ store: createMemoryStore(), transport: deadTransport });
    await expect(q.enqueue("invoice_send", {})).rejects.toThrow(/Unknown queue kind/);
    await expect(q.enqueue("payment", {})).rejects.toThrow(/Unknown queue kind/);
  });
});

describe("capture offline, sync on reconnect", () => {
  test("items wait visibly offline, then land exactly once when the transport heals", async () => {
    const server = makeServer();
    let live = false;
    const transport = Object.fromEntries(
      Object.entries(server.transport).map(([k, fn]) => [k, async (...a) => { if (!live) throw netErr(); return fn(...a); }]),
    );
    const q = createQueue({ store: createMemoryStore(), transport });
    await q.enqueue("time_entry", { date: "2026-11-14", hours: 2, client_name: "Acme", notes: "", billable: true });
    await q.enqueue("receipt", { filename: "r.jpg", vendor: "", amount: "", date: "" });

    let snap = q.snapshot();
    expect(snap.counts).toMatchObject({ waiting: 2, total: 2 });

    const offlinePass = await q.syncNow();
    expect(offlinePass.offline).toBe(true);
    snap = q.snapshot();
    expect(snap.counts.waiting).toBe(2); // nothing lost, nothing faked as sent
    expect(server.state.entries.size).toBe(0);

    live = true;
    const pass = await q.syncNow();
    expect(pass.sent).toBe(2);
    expect(q.snapshot().counts.total).toBe(0);
    expect(server.state.entries.size).toBe(1);
    expect(server.state.receipts.size).toBe(1);
  });

  test("status counts are accurate at each step", async () => {
    const server = makeServer();
    const q = createQueue({ store: createMemoryStore(), transport: server.transport });
    await q.enqueue("time_entry", { date: "2026-11-14", hours: 1 });
    await q.enqueue("time_entry", { date: "2026-11-14", hours: 3 });
    expect(q.snapshot().counts).toMatchObject({ waiting: 2, syncing: 0, attention: 0, failed: 0 });
    await q.syncNow();
    expect(q.snapshot().counts).toMatchObject({ waiting: 0, total: 0 });
  });
});

describe("exactly-once under retries", () => {
  test("a response lost after the server recorded the entry does not duplicate it", async () => {
    const server = makeServer();
    const q = createQueue({ store: createMemoryStore(), transport: server.transport });
    await q.enqueue("time_entry", { date: "2026-11-14", hours: 2.5 });
    server.state.dropNextResponse = true; // server records, response never arrives
    const first = await q.syncNow();
    expect(first.offline).toBe(true);
    expect(server.state.entries.size).toBe(1); // it DID land
    expect(q.snapshot().counts.waiting).toBe(1); // queue honestly still shows it waiting

    const second = await q.syncNow(); // the retry
    expect(second.sent).toBe(1);
    expect(server.state.entries.size).toBe(1); // still exactly one server record
    expect(q.snapshot().counts.total).toBe(0);
  });

  test("receipt upload is idempotent the same way", async () => {
    const server = makeServer();
    const q = createQueue({ store: createMemoryStore(), transport: server.transport });
    await q.enqueue("receipt", { filename: "a.jpg" });
    server.state.dropNextResponse = true;
    await q.syncNow();
    await q.syncNow();
    expect(server.state.receipts.size).toBe(1);
    expect(q.snapshot().counts.total).toBe(0);
  });

  test("expense note uploads its photo once even when the expense step needs a retry", async () => {
    const server = makeServer();
    const q = createQueue({ store: createMemoryStore(), transport: server.transport });
    const item = await q.enqueue("expense_note", { amount: "12.50", note: "Tape", photoBlob: { fake: true }, photoName: "p.jpg" });
    // First pass: photo lands, then the expense call drops its response.
    const origExpense = server.transport.postExpense;
    let tripped = false;
    server.transport.postExpense = async (p) => {
      if (!tripped) { tripped = true; const r = await origExpense(p); throw netErr(); }
      return origExpense(p);
    };
    await q.syncNow();
    const receiptCalls = server.state.calls.filter((c) => c[0] === "receipt").length;
    expect(receiptCalls).toBe(1);
    expect(server.state.expenses.size).toBe(1); // expense landed despite the lost response
    await q.retry(item.id);
    expect(server.state.calls.filter((c) => c[0] === "receipt").length).toBe(1); // photo NOT re-uploaded
    expect(server.state.expenses.size).toBe(1);
    expect(q.snapshot().counts.total).toBe(0);
  });
});

describe("conflicts resolve as server-wins, in the open", () => {
  test("clock-in against an existing server shift goes to needs-attention with the reason", async () => {
    const server = makeServer();
    server.state.openShift = { id: "office-shift", status: "open" }; // clocked in from the office
    const q = createQueue({ store: createMemoryStore(), transport: server.transport });
    await q.enqueue("time_clock_in", { notes: "", client_in_at: "2026-11-14T13:00:00.000Z" });
    const pass = await q.syncNow();
    expect(pass.attention).toBe(1);
    const item = q.snapshot().items[0];
    expect(item.state).toBe("attention");
    expect(item.error).toMatch(/already clocked in on the server/i);
    expect(server.state.clockIns.size).toBe(0); // server shift untouched, nothing overwritten
  });

  test("clock-out with no open shift goes to needs-attention, not silence", async () => {
    const server = makeServer();
    const q = createQueue({ store: createMemoryStore(), transport: server.transport });
    await q.enqueue("time_clock_out", {});
    const pass = await q.syncNow();
    expect(pass.attention).toBe(1);
    expect(q.snapshot().items[0].error).toMatch(/no open shift/i);
  });
});

describe("failures stay visible and retryable", () => {
  test("a 422 marks the item failed with the server's reason; retry after a fix sends it", async () => {
    const server = makeServer();
    const q = createQueue({ store: createMemoryStore(), transport: server.transport });
    const item = await q.enqueue("expense_note", { amount: "0", note: "bad" });
    const pass = await q.syncNow();
    expect(pass.failed).toBe(1);
    expect(q.snapshot().items[0].state).toBe("failed");
    expect(q.snapshot().items[0].error).toMatch(/positive/i);
    q.snapshot().items[0].payload.amount = "9.99"; // corrected before retrying
    await q.retry(item.id);
    expect(server.state.expenses.size).toBe(1);
    expect(q.snapshot().counts.total).toBe(0);
  });

  test("discard removes the item from the queue and the store", async () => {
    const store = createMemoryStore();
    const q = createQueue({ store, transport: deadTransport });
    const item = await q.enqueue("time_entry", { date: "2026-11-14", hours: 1 });
    await q.discard(item.id);
    expect(q.snapshot().counts.total).toBe(0);
    expect(await store.getAll()).toHaveLength(0);
  });
});

describe("persistence across reload", () => {
  test("queued items survive a store round-trip (the IndexedDB reload shape)", async () => {
    // A store whose records cross a JSON boundary on every write/read —
    // the same durability contract IndexedDB gives the real app.
    const backing = new Map();
    const durableStore = {
      async getAll() { return [...backing.values()].map((r) => JSON.parse(JSON.stringify(r))); },
      async put(r) { backing.set(r.id, JSON.parse(JSON.stringify(r))); },
      async remove(id) { backing.delete(id); },
    };
    const server = makeServer();
    const q1 = createQueue({ store: durableStore, transport: deadTransport });
    await q1.enqueue("time_entry", { date: "2026-11-14", hours: 4, notes: "rough-in" });
    await q1.enqueue("expense_note", { amount: "20", note: "gas" });

    const q2 = createQueue({ store: durableStore, transport: server.transport }); // "reopened app"
    const loaded = await q2.load();
    expect(loaded).toHaveLength(2);
    expect(q2.snapshot().counts.waiting).toBe(2);
    const pass = await q2.syncNow();
    expect(pass.sent).toBe(2);
    expect(server.state.entries.size).toBe(1);
    expect(server.state.expenses.size).toBe(1);
  });
});

describe("plain-words labels", () => {
  test("describeItem reads like a person wrote it", () => {
    expect(describeItem({ kind: "time_entry", payload: { hours: 2, client_name: "Acme", date: "2026-11-14" } })).toContain("Time entry — 2h for Acme");
    expect(describeItem({ kind: "receipt", payload: { vendor: "Home Depot", amount: "86.40" } })).toContain("Receipt photo — Home Depot — $86.40");
    expect(describeItem({ kind: "time_clock_in", payload: {} })).toBe("Clock-in");
  });
});
