/**
 * SyncStatus chip + panel (feat/offline-field-capture): the queue's
 * visibility contract — counts on the chip, every item listed in plain
 * words, problems impossible to miss. Rendered with react-dom + act
 * (this repo has no @testing-library install).
 */
import { act } from "react";
import { createRoot } from "react-dom/client";
import SyncStatus from "@/components/SyncStatus";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mockQ = {
  items: [],
  counts: { waiting: 0, syncing: 0, attention: 0, failed: 0, total: 0 },
  syncing: false,
  online: true,
  describeItem: (i) => (i.kind === "time_entry" ? "Time entry — 2h for Acme (2026-11-14)" : i.kind),
  syncNow: jest.fn(),
  retry: jest.fn(),
  discard: jest.fn(),
  captureExpenseNote: jest.fn(),
  pendingClockIn: null,
};

jest.mock("@/context/OfflineQueueContext", () => ({
  useOfflineQueue: () => mockQ,
}));

let container, root;
beforeEach(() => {
  mockQ.items = [];
  mockQ.counts = { waiting: 0, syncing: 0, attention: 0, failed: 0, total: 0 };
  mockQ.online = true;
  jest.clearAllMocks();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const renderChip = () => act(() => { root.render(<SyncStatus />); });
const chip = () => container.querySelector('[data-testid="sync-status-chip"]');
const clickChip = () => act(() => {
  chip().dispatchEvent(new MouseEvent("click", { bubbles: true }));
});

test("chip says Synced when the queue is empty and we're online", () => {
  renderChip();
  expect(chip().textContent).toContain("Synced");
});

test("chip counts what is waiting while offline", () => {
  mockQ.online = false;
  mockQ.counts = { waiting: 2, syncing: 0, attention: 0, failed: 0, total: 2 };
  renderChip();
  expect(chip().textContent).toContain("Offline · 2 waiting");
});

test("panel lists every item in plain words with its state and reason", async () => {
  mockQ.counts = { waiting: 0, syncing: 0, attention: 1, failed: 0, total: 1 };
  mockQ.items = [{
    id: "fc_1", kind: "time_entry", state: "attention", created_at: "2026-11-14T14:02:00.000Z",
    payload: {}, error: "You're already clocked in on the server, so this phone clock-in wasn't sent.",
  }];
  renderChip();
  clickChip();
  await act(async () => {}); // let the dialog portal mount
  const text = document.body.textContent;
  expect(text).toContain("Time entry — 2h for Acme (2026-11-14)");
  expect(text).toContain("Needs attention");
  expect(text).toMatch(/already clocked in on the server/i);
  expect(document.querySelector('[data-testid="sync-counts"]').textContent).toContain("Needs attention: 1");
  const buttons = [...document.querySelectorAll("button")].map((b) => b.textContent);
  expect(buttons.some((t) => /retry/i.test(t))).toBe(true);
  expect(buttons.some((t) => /discard/i.test(t))).toBe(true);
});

test("empty panel says plainly that everything is on the books", async () => {
  renderChip();
  clickChip();
  await act(async () => {});
  expect(document.querySelector('[data-testid="sync-empty"]').textContent).toMatch(/on your books/i);
});
