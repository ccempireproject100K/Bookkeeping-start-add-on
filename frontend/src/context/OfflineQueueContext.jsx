import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { createIdbStore, createQueue, describeItem } from "@/lib/offlineQueue";
import { apiTransport, isOnline } from "@/lib/fieldCapture";

/**
 * App-wide offline field-capture queue.
 *
 * Captures ALWAYS go through the queue (one code path, strict order);
 * when the phone is online the sync pass runs immediately, so an online
 * clock-in still lands at once — it just also survives a dead zone.
 * Auto-sync fires on the browser's `online` event and on a slow interval
 * while the app is open; a flush that sends items ends with a visible
 * "back online — N items sent" toast. Nothing here is silent.
 */
const Ctx = createContext(null);

export function OfflineQueueProvider({ children }) {
  const queueRef = useRef(null);
  if (!queueRef.current) {
    queueRef.current = createQueue({ store: createIdbStore(), transport: apiTransport });
  }
  const queue = queueRef.current;
  const [snap, setSnap] = useState({ items: [], counts: { waiting: 0, syncing: 0, attention: 0, failed: 0, total: 0 }, syncing: false });
  const [online, setOnline] = useState(isOnline());
  const wasOffline = useRef(!isOnline());

  useEffect(() => {
    const unsub = queue.subscribe(setSnap);
    queue.load();
    return unsub;
  }, [queue]);

  const runSync = async (announce) => {
    const summary = await queue.syncNow();
    if (summary.sent > 0 && announce) {
      toast.success(
        wasOffline.current
          ? `Back online — ${summary.sent} item${summary.sent === 1 ? "" : "s"} sent`
          : `${summary.sent} item${summary.sent === 1 ? "" : "s"} sent`,
      );
    }
    if (summary.attention > 0) {
      toast.warning(`${summary.attention} item${summary.attention === 1 ? "" : "s"} need${summary.attention === 1 ? "s" : ""} your attention — open the sync panel`);
    }
    wasOffline.current = summary.offline;
    return summary;
  };

  useEffect(() => {
    const goOnline = () => { setOnline(true); runSync(true); };
    const goOffline = () => { setOnline(false); wasOffline.current = true; };
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    const t = setInterval(() => { if (isOnline()) runSync(false); }, 45000);
    runSync(false); // catch up on anything captured in a previous session
    return () => {
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queue]);

  const value = useMemo(() => {
    const capture = async (kind, payload) => {
      const item = await queue.enqueue(kind, payload);
      if (isOnline()) runSync(false);
      return item;
    };
    return {
      items: snap.items,
      counts: snap.counts,
      syncing: snap.syncing,
      online,
      describeItem,
      syncNow: () => runSync(true),
      retry: (id) => queue.retry(id),
      discard: (id) => queue.discard(id),
      /** The one pending (unsent) clock-in item, if the tech clocked in offline. */
      pendingClockIn: snap.items.find((i) => i.kind === "time_clock_in" && i.state !== "attention") || null,
      captureTimeEntry: (payload) => capture("time_entry", payload),
      captureClockIn: (payload) => capture("time_clock_in", payload),
      captureClockOut: () => capture("time_clock_out", {}),
      captureReceipt: (payload) => capture("receipt", payload),
      captureExpenseNote: (payload) => capture("expense_note", payload),
      captureStockScan: (payload) => capture("stock_scan", payload),
      /**
       * Clock-out pressed while the matching clock-in is still queued:
       * the pair becomes ONE manual time entry built from the phone's own
       * in/out timestamps, so the hours are the hours actually worked —
       * the server's clock-in would stamp sync time and undercount.
       */
      async convertPendingClockInToEntry(clockOutAtIso) {
        const pending = snap.items.find((i) => i.kind === "time_clock_in");
        if (!pending) return null;
        const inAt = pending.payload.client_in_at || pending.created_at;
        const hours = Math.max(0.25, Math.round(((new Date(clockOutAtIso) - new Date(inAt)) / 3600000) * 4) / 4);
        await queue.discard(pending.id);
        return capture("time_entry", {
          date: inAt.slice(0, 10),
          hours,
          notes: `${pending.payload.notes || ""} (clocked in/out in the field; captured offline)`.trim(),
          client_name: pending.payload.client_name || "",
          billable: true,
          job_id: pending.payload.job_id || null,
        });
      },
    };
  }, [snap, online, queue]); // eslint-disable-line react-hooks/exhaustive-deps

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useOfflineQueue() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useOfflineQueue must be used inside OfflineQueueProvider");
  return ctx;
}
