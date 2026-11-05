/**
 * PipDoor — the Pip File Door in the Bookkeeping authenticated shell
 * (DESIGN §3.1, §5.1). Phase 1: the books.receipts route only.
 *
 * Responsibilities (and nothing more): the shell dropzone, the animated
 * Pip, the multi-file queue, the action/question cards, and "Pip's tray".
 * It contains NO product endpoints and NO business verbs — uploads go
 * through the adapter (receiptsAdapter.js), classification through the
 * vendored core sniffer, and tray state through the vendored tray store.
 * Tray states move ONLY on real events (sniffer verdicts, adapter
 * responses) — never on timers (DESIGN §5.4). The timers that do exist
 * here drive the CHARACTER (walk cycle, celebration display, idle
 * sleep), exactly as on the owner's desktop Pip.
 *
 * Motion (owner scope upgrade 2026-11-05): Pip walks the bottom of the
 * shell with the desktop version's three speeds (stroll/brisk/zoom,
 * persisted per user), pauses when a dialog/card is open, when the tab
 * is hidden, on drag-over (he becomes the drop target), while carrying
 * (a real upload in flight), and after a long idle he sleeps. All motion
 * stops under prefers-reduced-motion or the per-user button mode, with
 * 100% of function kept in both.
 */
import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { useAuth } from "@/context/AuthContext";
import { errMsg } from "@/lib/api";
import { sniffFile, createTrayStore, TRAY_EVENTS } from "./core/src/index.js";
import PipCharacter from "./PipCharacter";
import { createReceiptsAdapter } from "./receiptsAdapter";
import { pipFlag, PIP_SETTING_KEYS, RECEIPTS_ADAPTER_ID } from "./config";
import "./pip.css";

/** The tray store for the shell (one per app session; tenant-namespaced). */
export const pipTrayStore = createTrayStore();

const SPEED_PER_TICK = { stroll: 0.16, brisk: 0.34, zoom: 0.62 }; // % of screen width per 40ms tick — the desktop Pip's rates
const WALK_MIN = 2;
const WALK_MAX = 88;
const SLEEP_AFTER_MS = 45000;
const CELEBRATE_MS = 3200;
const SLUMP_MS = 6000;
const CONFETTI_COLORS = ["#14b8a6", "#f59e0b", "#fbbf24", "#ffffff"];

function loadSetting(key, fallback) {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
function saveSetting(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* private mode etc. — settings simply don't persist */
  }
}

/**
 * Derive Pip's pose from REAL state only. Pure — unit-tested.
 * Priority: drag-over > celebration > carrying > thinking > slumped >
 * sleep > walk/idle.
 */
export function pipPose({ dragOver, celebrating, anyStaging, anyQuestion, lastOutcome, sleeping, walking }) {
  if (dragOver) return "excited";
  if (celebrating) return "celebrate";
  if (anyStaging) return "carrying";
  if (anyQuestion) return "thinking";
  if (lastOutcome === "failed") return "slumped";
  if (sleeping) return "sleep";
  return walking ? "walk" : "idle";
}

function blobToArrayBuffer(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = reject;
    r.readAsArrayBuffer(blob);
  });
}
function blobToText(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = reject;
    r.readAsText(blob);
  });
}

const TEXT_SAMPLE_EXT = /\.(csv|json|txt|md)$/i;

/** Read only what the sniffer needs (§5.3): header slice + a text sample for text-shaped files. */
export async function readSniffInput(file, page) {
  const headerBuf = await blobToArrayBuffer(file.slice(0, 8192));
  let textSample;
  if (TEXT_SAMPLE_EXT.test(file.name) || (file.type || "").startsWith("text/") || file.type === "application/json") {
    try {
      textSample = await blobToText(file.slice(0, 4096));
    } catch {
      textSample = undefined;
    }
  }
  return {
    filename: file.name,
    mimeType: file.type || undefined,
    headerBytes: new Uint8Array(headerBuf),
    textSample,
    sizeBytes: file.size,
    pageContext: { product: "books", page },
  };
}

export function PipDoor({
  tenantId,
  canStage,
  navigate,
  adapter = undefined,
  store = pipTrayStore,
  page = undefined,
}) {
  const { t } = useTranslation();
  const receipts = useMemo(() => adapter || createReceiptsAdapter(), [adapter]);

  const [entries, setEntries] = useState(() => store.list(tenantId));
  const [trayOpen, setTrayOpen] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [receiptMeta, setReceiptMeta] = useState({}); // entryId -> { receiptId, scanned }
  const [scanningId, setScanningId] = useState(null);
  const [celebrating, setCelebrating] = useState(false);
  const [cssConfetti, setCssConfetti] = useState(null);
  const [lastOutcome, setLastOutcome] = useState(null);
  const [sleeping, setSleeping] = useState(false);
  const [docHidden, setDocHidden] = useState(false);
  const [dismissed, setDismissed] = useState(() => new Set());

  // Per-user settings (persisted; the desktop Pip's pattern, host keys).
  const [speed, setSpeed] = useState(() => {
    const s = loadSetting(PIP_SETTING_KEYS.speed, "brisk");
    return SPEED_PER_TICK[s] ? s : "brisk";
  });
  const [paused, setPaused] = useState(() => loadSetting(PIP_SETTING_KEYS.paused, "no") === "yes");
  const [buttonMode, setButtonMode] = useState(() => loadSetting(PIP_SETTING_KEYS.buttonMode, "no") === "yes");
  const [reducedMotion, setReducedMotion] = useState(() => {
    try {
      return !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    } catch {
      return false;
    }
  });

  // Walk state (refs for the tick loop, state for render — as the desktop Pip).
  const [x, setX] = useState(14);
  const [facing, setFacing] = useState(1);
  const xRef = useRef(14);
  const dirRef = useRef(1);

  const filesRef = useRef(new Map()); // entryId -> File (memory only; blobs never persist, §5.3)
  const sniffRef = useRef(new Map()); // entryId -> SniffResult
  const fileInputRef = useRef(null);
  const trayRef = useRef(null);
  const dragCountRef = useRef(0);
  const lastActivityRef = useRef(Date.now());
  const timersRef = useRef([]);
  const prevTenantRef = useRef(tenantId);

  const later = useCallback((fn, ms) => {
    const id = window.setTimeout(fn, ms);
    timersRef.current.push(id);
    return id;
  }, []);

  useEffect(() => () => timersRef.current.forEach((id) => window.clearTimeout(id)), []);

  const markActivity = useCallback(() => {
    lastActivityRef.current = Date.now();
    setSleeping(false);
  }, []);

  // ---- tenant switch: wipe the previous tenant's tray (§5.6) ----
  useEffect(() => {
    if (prevTenantRef.current !== tenantId) {
      store.wipe(prevTenantRef.current);
      filesRef.current.clear();
      sniffRef.current.clear();
      setReceiptMeta({});
      setDismissed(new Set());
      prevTenantRef.current = tenantId;
    }
    setEntries(store.list(tenantId));
  }, [tenantId, store]);

  // ---- hydrate persisted tray history for this tenant (§5.3) ----
  useEffect(() => {
    let raw = null;
    try {
      raw = window.localStorage.getItem(`pip-tray:${tenantId}`);
    } catch {
      raw = null;
    }
    if (!raw) return;
    try {
      const saved = JSON.parse(raw);
      for (const e of saved) {
        if (store.get(tenantId, e.id)) continue;
        const entry = store.add({
          id: e.id, tenantId, name: e.name, kind: e.kind, route: e.route,
          link: e.link, evidence: e.evidence, error: e.error,
        });
        // Replay only honest restorations: delivered/failed/unsupported
        // restore as-is; anything mid-flight has lost its file, so it
        // fails honestly instead of pretending to be queued (§6.2).
        if (e.state === "delivered") {
          store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.SNIFF_START });
          store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.SNIFF_READY, patch: { route: e.route } });
          store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.STAGE_START });
          store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.DELIVER, patch: { link: e.link } });
          const rid = /[?&]receipt=([^&]+)/.exec(e.link || "");
          if (rid) setReceiptMeta((m) => ({ ...m, [entry.id]: { receiptId: decodeURIComponent(rid[1]), scanned: false } }));
        } else if (e.state === "unsupported") {
          store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.MARK_UNSUPPORTED, patch: { kind: e.kind, evidence: e.evidence } });
        } else if (e.state === "failed") {
          store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.FAIL, patch: { error: e.error } });
        } else {
          store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.FAIL, patch: { error: "pip.fileGone" } });
        }
      }
      setEntries(store.list(tenantId));
    } catch {
      /* a corrupt saved tray is dropped, never fatal */
    }
    // hydrate once per tenant mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  // ---- subscribe: render + persist + announce + celebrate/slump ----
  useEffect(() => {
    const unsub = store.subscribe((note) => {
      if (note.tenantId !== tenantId) return;
      markActivity();
      const list = store.list(tenantId);
      setEntries(list);
      try {
        window.localStorage.setItem(`pip-tray:${tenantId}`, JSON.stringify(list));
      } catch {
        /* storage full/blocked — the tray still works in memory */
      }
      if (note.entry) {
        setAnnouncement(`${note.entry.name}: ${t(`pip.state.${note.entry.state}`)}`);
        if (note.action === "transition" && note.entry.state === "delivered") {
          setLastOutcome("delivered");
          setCelebrating(true);
          fireConfetti();
          later(() => setCelebrating(false), CELEBRATE_MS);
        }
        if (note.action === "transition" && note.entry.state === "failed") {
          setLastOutcome("failed");
          later(() => setLastOutcome((o) => (o === "failed" ? null : o)), SLUMP_MS);
        }
      }
    });
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, tenantId]);

  const fireConfetti = useCallback(() => {
    const cssFallback = () => {
      // CSS fallback burst (same moment, no library): deterministic pieces.
      const pieces = Array.from({ length: 22 }, (_, i) => ({
        left: 6 + ((i * 41) % 80),
        delay: (i % 7) * 0.07,
        dur: 1.4 + (i % 5) * 0.22,
        color: CONFETTI_COLORS[i % CONFETTI_COLORS.length],
        w: 6 + (i % 3) * 2,
      }));
      setCssConfetti(pieces);
      later(() => setCssConfetti(null), 2600);
    };
    // canvas-confetti needs a real 2d context; where there is none
    // (jsdom, some embedded webviews) it fails inside its own rAF loop,
    // which no .catch can reach — so check first, fall back cleanly.
    let ctx = null;
    try {
      ctx = document.createElement("canvas").getContext("2d");
    } catch {
      ctx = null;
    }
    if (!ctx) {
      cssFallback();
      return;
    }
    import("canvas-confetti")
      .then((mod) => {
        const fire = mod.default || mod;
        fire({ particleCount: 70, spread: 75, origin: { y: 0.92 }, colors: CONFETTI_COLORS });
        fire({ particleCount: 40, angle: 60, spread: 60, origin: { x: 0, y: 0.95 }, colors: CONFETTI_COLORS });
      })
      .catch(cssFallback);
  }, [later]);

  // ---- visibility + reduced-motion listeners ----
  useEffect(() => {
    const onVis = () => setDocHidden(!!document.hidden);
    document.addEventListener("visibilitychange", onVis);
    onVis();
    let mq;
    try {
      mq = window.matchMedia("(prefers-reduced-motion: reduce)");
      const onMq = () => setReducedMotion(!!mq.matches);
      mq.addEventListener?.("change", onMq);
      return () => {
        document.removeEventListener("visibilitychange", onVis);
        mq.removeEventListener?.("change", onMq);
      };
    } catch {
      return () => document.removeEventListener("visibilitychange", onVis);
    }
  }, []);

  // ---- idle sleep check (character stillness only — never tray state) ----
  useEffect(() => {
    const id = window.setInterval(() => {
      if (Date.now() - lastActivityRef.current > SLEEP_AFTER_MS) setSleeping(true);
    }, 5000);
    return () => window.clearInterval(id);
  }, []);

  const visibleEntries = useMemo(() => entries.filter((e) => !dismissed.has(e.id)), [entries, dismissed]);
  const anyStaging = visibleEntries.some((e) => e.state === "staging");
  const anyQuestion = visibleEntries.some((e) => e.state === "needs-answer");
  const readyEntries = visibleEntries.filter((e) => e.state === "ready" && e.route === RECEIPTS_ADAPTER_ID);

  const walkingActive =
    !buttonMode && !reducedMotion && !paused && !docHidden && !dragOver && !trayOpen &&
    !celebrating && !sleeping && !anyStaging && !anyQuestion;

  // ---- the walk loop (desktop Pip's per-tick system, transform-only) ----
  useEffect(() => {
    if (!walkingActive) return undefined;
    const perTick = SPEED_PER_TICK[speed] || SPEED_PER_TICK.brisk;
    const id = window.setInterval(() => {
      let next = xRef.current + dirRef.current * perTick;
      if (next >= WALK_MAX) {
        next = WALK_MAX;
        dirRef.current = -1;
        setFacing(-1);
      } else if (next <= WALK_MIN) {
        next = WALK_MIN;
        dirRef.current = 1;
        setFacing(1);
      }
      xRef.current = next;
      setX(next);
    }, 40);
    return () => window.clearInterval(id);
  }, [walkingActive, speed]);

  const pose = pipPose({
    dragOver, celebrating, anyStaging, anyQuestion, lastOutcome, sleeping,
    walking: walkingActive,
  });

  // ---- intake ----
  const processFile = useCallback(
    async (entryId, file) => {
      store.dispatch(tenantId, entryId, { type: TRAY_EVENTS.SNIFF_START });
      try {
        const input = await readSniffInput(file, page);
        const result = sniffFile(input);
        sniffRef.current.set(entryId, result);
        if (result.terminal || result.danger) {
          store.dispatch(tenantId, entryId, {
            type: TRAY_EVENTS.MARK_UNSUPPORTED,
            patch: { kind: result.kind, evidence: result.evidence },
          });
          return;
        }
        if (result.needsQuestion) {
          const hasReceiptOption = result.needsQuestion.options.some((o) => o.adapterId === RECEIPTS_ADAPTER_ID);
          if (hasReceiptOption) {
            store.dispatch(tenantId, entryId, {
              type: TRAY_EVENTS.SNIFF_NEEDS_ANSWER,
              patch: { kind: result.kind, questionId: result.needsQuestion.id, evidence: result.evidence },
            });
          } else {
            // A question Pip cannot act on in Phase 1 (the CSV fork,
            // office-container): the honesty/signpost card, never a
            // question with nowhere to go (§4.7).
            store.dispatch(tenantId, entryId, {
              type: TRAY_EVENTS.MARK_UNSUPPORTED,
              patch: { kind: result.kind, evidence: result.evidence },
            });
          }
          return;
        }
        const top = result.candidates?.[0];
        if (top?.adapterId === RECEIPTS_ADAPTER_ID && top.confidence >= 0.8) {
          store.dispatch(tenantId, entryId, {
            type: TRAY_EVENTS.SNIFF_READY,
            patch: { kind: result.kind, route: top.adapterId, evidence: top.evidence },
          });
        } else {
          store.dispatch(tenantId, entryId, {
            type: TRAY_EVENTS.MARK_UNSUPPORTED,
            patch: { kind: result.kind, evidence: result.evidence },
          });
        }
      } catch (e) {
        store.dispatch(tenantId, entryId, { type: TRAY_EVENTS.FAIL, patch: { error: errMsg(e) } });
      }
    },
    [store, tenantId, page],
  );

  const addFiles = useCallback(
    (files) => {
      const list = [...(files || [])];
      if (!list.length) return;
      markActivity();
      setLastOutcome(null);
      setTrayOpen(true);
      for (const file of list) {
        const entry = store.add({ tenantId, name: file.name });
        filesRef.current.set(entry.id, file);
        void processFile(entry.id, file);
      }
    },
    [store, tenantId, processFile, markActivity],
  );

  // ---- shell-wide drag & drop ----
  useEffect(() => {
    const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    const onDragEnter = (e) => {
      if (!hasFiles(e)) return;
      dragCountRef.current += 1;
      markActivity();
      setDragOver(true);
    };
    const onDragOver = (e) => {
      if (hasFiles(e)) e.preventDefault();
    };
    const onDragLeave = () => {
      dragCountRef.current = Math.max(0, dragCountRef.current - 1);
      if (dragCountRef.current === 0) setDragOver(false);
    };
    const onDrop = (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragCountRef.current = 0;
      setDragOver(false);
      addFiles(e.dataTransfer.files);
    };
    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, [addFiles, markActivity]);

  // ---- actions ----
  const answerQuestion = useCallback(
    (entry, option) => {
      markActivity();
      if (!option.adapterId) {
        // “Neither” — the tray machine has no needs-answer → unsupported
        // edge by design; the entry closes as a failed route and renders
        // as the honesty card (no Retry) via the sentinel error key.
        store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.FAIL, patch: { error: "pip.honestyNeither" } });
        return;
      }
      const result = sniffRef.current.get(entry.id);
      store.dispatch(tenantId, entry.id, {
        type: TRAY_EVENTS.SNIFF_READY,
        patch: { route: option.adapterId, evidence: result?.evidence || entry.evidence },
      });
    },
    [store, tenantId, markActivity],
  );

  const stageEntry = useCallback(
    async (entry) => {
      const file = filesRef.current.get(entry.id);
      markActivity();
      if (!file) {
        store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.STAGE_START });
        store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.FAIL, patch: { error: "pip.fileGone" } });
        return;
      }
      store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.STAGE_START });
      try {
        const ref = await receipts.stage(file);
        if (ref?.id) setReceiptMeta((m) => ({ ...m, [entry.id]: { receiptId: ref.id, scanned: false } }));
        store.dispatch(tenantId, entry.id, {
          type: TRAY_EVENTS.DELIVER,
          patch: { route: RECEIPTS_ADAPTER_ID, link: receipts.deepLink(ref) },
        });
      } catch (e) {
        store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.FAIL, patch: { error: errMsg(e) } });
      }
    },
    [store, tenantId, receipts, markActivity],
  );

  const stageAll = useCallback(async () => {
    for (const entry of readyEntries) {
      // sequential: one request per file, failures isolated (§4.1)
      // eslint-disable-next-line no-await-in-loop
      await stageEntry(entry);
    }
  }, [readyEntries, stageEntry]);

  const retryEntry = useCallback(
    (entry) => {
      store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.RETRY });
      store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.SNIFF_START });
      store.dispatch(tenantId, entry.id, { type: TRAY_EVENTS.SNIFF_READY, patch: { route: entry.route || RECEIPTS_ADAPTER_ID } });
      void stageEntry({ ...entry, state: "ready" });
    },
    [store, tenantId, stageEntry],
  );

  const scanEntry = useCallback(
    async (entry) => {
      const rid = receiptMeta[entry.id]?.receiptId;
      if (!rid || scanningId) return;
      markActivity();
      setScanningId(entry.id);
      try {
        await receipts.scan(rid);
        setReceiptMeta((m) => ({ ...m, [entry.id]: { receiptId: rid, scanned: true } }));
        toast.success(t("pip.scanDone"));
      } catch (e) {
        toast.error(errMsg(e));
      } finally {
        setScanningId(null);
      }
    },
    [receiptMeta, scanningId, receipts, t, markActivity],
  );

  const removeEntry = useCallback((entry) => {
    filesRef.current.delete(entry.id);
    sniffRef.current.delete(entry.id);
    setDismissed((prev) => new Set(prev).add(entry.id));
  }, []);

  const openPicker = useCallback(() => {
    markActivity();
    fileInputRef.current?.click();
  }, [markActivity]);

  useEffect(() => {
    if (trayOpen) trayRef.current?.focus?.();
  }, [trayOpen]);

  // ---- render helpers ----
  const renderEvidence = (evidence) =>
    evidence?.length ? (
      <ul className="mt-1 space-y-0.5">
        {evidence.map((line, i) => (
          <li key={i} className="text-[11px] leading-snug text-gray-500">{line}</li>
        ))}
      </ul>
    ) : null;

  const renderEntry = (entry) => {
    const result = sniffRef.current.get(entry.id);
    const meta = receiptMeta[entry.id];
    const isHonestyClose = entry.state === "failed" && entry.error === "pip.honestyNeither";
    return (
      <div key={entry.id} data-testid={`pip-entry-${entry.id}`} data-state={entry.state}
        className="rounded-xl border border-[#222838] bg-[#10131a] p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-gray-100" title={entry.name}>{entry.name}</div>
            <div className="text-[11px] uppercase tracking-wide text-gray-500">
              {isHonestyClose ? t("pip.state.unsupported") : t(`pip.state.${entry.state}`)}
            </div>
          </div>
          {(entry.state === "unsupported" || entry.state === "failed" || entry.state === "delivered") && (
            <button type="button" onClick={() => removeEntry(entry)} data-testid={`pip-remove-${entry.id}`}
              className="shrink-0 rounded-md px-2 py-1 text-xs text-gray-400 hover:bg-[#1a2030] hover:text-gray-100">
              {t("pip.remove")}
            </button>
          )}
        </div>

        {entry.state === "needs-answer" && result?.needsQuestion && (
          <div className="mt-2">
            <p className="text-sm text-gray-200">{result.needsQuestion.prompt}</p>
            <div className="mt-2 flex flex-wrap gap-2">
              {result.needsQuestion.options
                .filter((o) => !o.adapterId || o.adapterId === RECEIPTS_ADAPTER_ID)
                .map((o) => (
                  <button key={o.id} type="button" data-testid={`pip-answer-${entry.id}-${o.id}`}
                    onClick={() => answerQuestion(entry, o)}
                    className="rounded-full border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-300 hover:bg-amber-500/20">
                    {o.label}
                  </button>
                ))}
            </div>
            {renderEvidence(entry.evidence)}
          </div>
        )}

        {entry.state === "ready" && (
          <div className="mt-2">
            <p className="text-sm text-gray-300">{t("pip.receiptCard")}</p>
            {renderEvidence(entry.evidence)}
            {canStage ? (
              <button type="button" data-testid={`pip-send-${entry.id}`} onClick={() => void stageEntry(entry)}
                className="mt-2 rounded-lg bg-amber-500 px-3 py-2 text-sm font-semibold text-black hover:bg-amber-400">
                {t("pip.sendToReceipts")}
              </button>
            ) : (
              <p className="mt-2 text-xs text-amber-300/90">{t("pip.ownerOnly")}</p>
            )}
          </div>
        )}

        {entry.state === "delivered" && (
          <div className="mt-2 space-y-2">
            <p className="text-sm text-emerald-300">✓ {t("pip.deliveredNote")}</p>
            <div className="flex flex-wrap gap-2">
              <button type="button" data-testid={`pip-open-${entry.id}`}
                onClick={() => navigate(entry.link || "/receipts")}
                className="rounded-lg border border-[#2a3247] px-3 py-1.5 text-xs text-gray-100 hover:bg-[#1a2030]">
                {t("pip.openReceipt")}
              </button>
              {meta?.receiptId && !meta.scanned && canStage && (
                <button type="button" data-testid={`pip-scan-${entry.id}`} disabled={scanningId === entry.id}
                  onClick={() => void scanEntry(entry)}
                  className="rounded-lg bg-[#14b8a6] px-3 py-1.5 text-xs font-semibold text-black hover:bg-teal-400 disabled:opacity-50">
                  {scanningId === entry.id ? t("pip.scanning") : t("pip.scan")}
                </button>
              )}
              {meta?.scanned && <span className="px-1 py-1.5 text-xs text-teal-300">{t("pip.scannedNote")}</span>}
            </div>
          </div>
        )}

        {entry.state === "failed" && !isHonestyClose && (
          <div className="mt-2">
            <p className="text-sm text-rose-300">{entry.error?.startsWith("pip.") ? t(entry.error) : entry.error || t("pip.failedNote")}</p>
            <div className="mt-2 flex gap-2">
              {entry.route && canStage && (
                <button type="button" data-testid={`pip-retry-${entry.id}`} onClick={() => retryEntry(entry)}
                  className="rounded-lg border border-[#2a3247] px-3 py-1.5 text-xs text-gray-100 hover:bg-[#1a2030]">
                  {t("pip.retry")}
                </button>
              )}
            </div>
          </div>
        )}

        {(entry.state === "unsupported" || isHonestyClose) && (
          <div className="mt-2">
            {entry.kind === "backup" ? (
              <p className="text-sm font-medium text-rose-300">{t("pip.backupDanger")}</p>
            ) : (
              <p className="text-sm text-gray-300">{t("pip.unsupportedNote")}</p>
            )}
            {renderEvidence(entry.evidence)}
            {entry.kind === "csv" && (
              <button type="button" data-testid={`pip-signpost-${entry.id}`} onClick={() => navigate("/cleanup-console")}
                className="mt-2 rounded-lg border border-[#2a3247] px-3 py-1.5 text-xs text-gray-100 hover:bg-[#1a2030]">
                {t("pip.openCleanup")}
              </button>
            )}
          </div>
        )}
      </div>
    );
  };

  const pendingCount = visibleEntries.filter((e) => !["delivered", "unsupported"].includes(e.state)).length;

  return (
    <>
      {/* drop overlay — Pip lights up as the target */}
      {dragOver && (
        <div data-testid="pip-drop-overlay"
          className="pointer-events-none fixed inset-0 z-[70] flex items-end justify-center border-4 border-dashed border-teal-400/70 bg-[#0b0e13]/70 pb-32">
          <div className="rounded-2xl bg-[#0D0F14] px-5 py-3 text-base font-semibold text-teal-200 shadow-2xl">
            {t("pip.dropHere")}
          </div>
        </div>
      )}

      {/* CSS confetti fallback layer */}
      {cssConfetti && (
        <div className="pointer-events-none fixed inset-x-0 bottom-0 z-[65] h-[180px] overflow-hidden" aria-hidden="true">
          {cssConfetti.map((p, i) => (
            <span key={i} style={{
              position: "absolute", left: `${p.left}%`, top: -8, width: p.w, height: p.w * 0.5,
              background: p.color, animation: `pip-confettiFall ${p.dur}s ease-in ${p.delay}s both`,
            }} />
          ))}
        </div>
      )}

      <input ref={fileInputRef} type="file" multiple className="hidden" data-testid="pip-file-input"
        accept="image/jpeg,image/png,image/webp,application/pdf,.csv,.json,.txt,.md,.xlsx,.docx"
        onChange={(e) => {
          addFiles(e.target.files);
          e.target.value = "";
        }} />

      {/* the walking strip */}
      {!buttonMode ? (
        <div className="pointer-events-none fixed inset-x-0 bottom-0 z-[60] h-[104px]" data-testid="pip-strip">
          <div className="absolute bottom-1 will-change-transform"
            style={{ transform: `translateX(min(${x}vw, calc(100vw - 92px)))` }}>
            <button type="button" data-testid="pip-character-btn" aria-label={t("pip.openTray")}
              onClick={() => { markActivity(); setTrayOpen((o) => !o); }}
              className="pointer-events-auto block rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400">
              <PipCharacter state={pose} facing={facing} />
            </button>
          </div>
          {/* corner controls: tray toggle */}
          <div className="pointer-events-auto absolute bottom-2 right-3 flex items-center gap-2">
            {pendingCount > 0 && (
              <span className="rounded-full bg-amber-500 px-2 py-0.5 text-[11px] font-bold text-black" aria-hidden="true">{pendingCount}</span>
            )}
            <button type="button" data-testid="pip-tray-toggle" onClick={() => { markActivity(); setTrayOpen((o) => !o); }}
              className="rounded-lg border border-[#2a3247] bg-[#0D0F14]/90 px-3 py-2 text-xs text-gray-100 hover:bg-[#1a2030]">
              {t("pip.trayTitle")}
            </button>
          </div>
        </div>
      ) : (
        <div className="fixed bottom-3 right-3 z-[60] flex items-center gap-2" data-testid="pip-button-mode">
          <button type="button" data-testid="pip-add-files-btn" onClick={openPicker}
            className="rounded-lg bg-amber-500 px-4 py-2.5 text-sm font-semibold text-black shadow-lg hover:bg-amber-400">
            {t("pip.addFiles")}
          </button>
          <button type="button" data-testid="pip-tray-toggle" onClick={() => { markActivity(); setTrayOpen((o) => !o); }}
            className="rounded-lg border border-[#2a3247] bg-[#0D0F14]/90 px-3 py-2.5 text-xs text-gray-100 hover:bg-[#1a2030]">
            {t("pip.trayTitle")}{pendingCount > 0 ? ` (${pendingCount})` : ""}
          </button>
        </div>
      )}

      {/* the tray */}
      {trayOpen && (
        <div ref={trayRef} tabIndex={-1} role="dialog" aria-label={t("pip.trayTitle")} data-testid="pip-tray"
          className="pip-panel-in fixed bottom-[112px] right-3 z-[60] flex max-h-[62vh] w-[min(400px,calc(100vw-24px))] flex-col rounded-2xl border border-[#222838] bg-[#0D0F14] shadow-2xl focus:outline-none">
          <div className="flex items-center justify-between border-b border-[#1b2230] px-4 py-3">
            <div className="font-display text-sm font-semibold text-gray-50">{t("pip.trayTitle")}</div>
            <button type="button" data-testid="pip-tray-close" onClick={() => setTrayOpen(false)}
              className="rounded-md px-2 py-1 text-xs text-gray-400 hover:bg-[#1a2030] hover:text-gray-100">
              {t("pip.close")}
            </button>
          </div>

          {/* character controls: speed, pause, button mode */}
          <div className="flex flex-wrap items-center gap-2 border-b border-[#1b2230] px-4 py-2.5">
            <div className="flex overflow-hidden rounded-lg border border-[#2a3247]" role="group" aria-label={t("pip.speedLabel")}>
              {["stroll", "brisk", "zoom"].map((s) => (
                <button key={s} type="button" data-testid={`pip-speed-${s}`} aria-pressed={speed === s}
                  onClick={() => { setSpeed(s); saveSetting(PIP_SETTING_KEYS.speed, s); markActivity(); }}
                  className={`px-2.5 py-1.5 text-[11px] ${speed === s ? "bg-amber-500 font-bold text-black" : "bg-[#10131a] text-gray-300 hover:bg-[#1a2030]"}`}>
                  {t(`pip.speed.${s}`)}
                </button>
              ))}
            </div>
            <button type="button" data-testid="pip-pause-toggle" aria-pressed={paused}
              onClick={() => {
                const v = !paused;
                setPaused(v);
                saveSetting(PIP_SETTING_KEYS.paused, v ? "yes" : "no");
                markActivity();
              }}
              className="rounded-lg border border-[#2a3247] px-2.5 py-1.5 text-[11px] text-gray-200 hover:bg-[#1a2030]">
              {paused ? t("pip.resumeWalk") : t("pip.pauseWalk")}
            </button>
            <button type="button" data-testid="pip-button-mode-toggle" aria-pressed={buttonMode}
              onClick={() => {
                const v = !buttonMode;
                setButtonMode(v);
                saveSetting(PIP_SETTING_KEYS.buttonMode, v ? "yes" : "no");
              }}
              className="rounded-lg border border-[#2a3247] px-2.5 py-1.5 text-[11px] text-gray-200 hover:bg-[#1a2030]">
              {t("pip.buttonMode")}
            </button>
            <button type="button" data-testid="pip-add-files" onClick={openPicker}
              className="rounded-lg bg-amber-500 px-3 py-1.5 text-[11px] font-semibold text-black hover:bg-amber-400">
              {t("pip.addFiles")}
            </button>
          </div>

          {readyEntries.length >= 2 && canStage && (
            <div className="border-b border-[#1b2230] px-4 py-2.5">
              <button type="button" data-testid="pip-send-all" onClick={() => void stageAll()}
                className="w-full rounded-lg bg-amber-500 px-3 py-2 text-sm font-semibold text-black hover:bg-amber-400">
                {t("pip.sendAll", { count: readyEntries.length })}
              </button>
            </div>
          )}

          <div className="flex-1 space-y-2 overflow-y-auto px-3 py-3">
            {visibleEntries.length === 0 ? (
              <p className="px-1 py-4 text-center text-sm text-gray-500">{t("pip.trayEmpty")}</p>
            ) : (
              visibleEntries.map(renderEntry)
            )}
          </div>
        </div>
      )}

      {/* screen-reader announcements for every state change (§5.6) */}
      <div role="status" aria-live="polite" className="sr-only" data-testid="pip-announcer">{announcement}</div>
    </>
  );
}

/**
 * Shell wrapper: mounts the door only for a signed-in user, behind the
 * feature flag (kill switch in config.js). Layout lazy-loads this.
 */
export function PipDoorShell() {
  const { user } = useAuth();
  const navigate = useNavigate();
  if (!pipFlag("pip_door.enabled")) return null;
  if (!user || typeof user !== "object") return null;
  const tenantId = String(user.tenant_id || user.tenant?.id || user.id || "unknown");
  return (
    <Suspense fallback={null}>
      <PipDoor tenantId={tenantId} canStage={user.role === "owner"} navigate={navigate} />
    </Suspense>
  );
}

export default PipDoorShell;
