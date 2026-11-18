import React, { useEffect, useReducer, useState } from "react";
import { X, ArrowLeft, ArrowRight } from "lucide-react";
import InvoiceAssistantMascot from "./InvoiceAssistantMascot";
import {
  TOURS, TOUR_IDS, ALL_TOURS, FULL_TOUR_ID,
  initialTourState, tourReducer,
} from "../lib/tours";

/**
 * On-demand monster walkthroughs. NOTHING renders until the user asks —
 * tours never auto-start (owner rule, 2026-11-14).
 *
 * Ask first: every entry point opens the ask prompt — "full walkthrough,
 * or just help with the one thing?" — and delivers exactly what the user
 * picks. Completing the FULL walkthrough fires onFullWalkthroughComplete
 * (the page records it; the assistant then stops proactively offering the
 * full tour for a while — it stays available here forever, and
 * single-topic help is never suppressed).
 *
 * Usage: const tour = useMonsterTour({ onFullWalkthroughComplete });
 *        {tour.element} … tour.ask() / tour.ask("design-shop").
 */
/** Radix tabs activate on mousedown, not click — a synthetic .click()
 * alone never switches the tab, so fire the full sequence. */
export function activateTab(testid) {
  const el = document.querySelector(`[data-testid='${testid}']`);
  if (!el) return;
  el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  el.click();
}

export function useMonsterTour({ onFullWalkthroughComplete } = {}) {
  const [state, dispatch] = useReducer(tourReducer, initialTourState);
  const [askFocus, setAskFocus] = useState(null); // null = prompt closed

  const startTour = (tourId) => {
    setAskFocus(null);
    const t = ALL_TOURS[tourId];
    if (t?.tab === "settings") activateTab("tab-invoice-settings");
    if (t?.tab === "invoices") activateTab("tab-invoices");
    // Let the tab switch render before the first step measures its target.
    setTimeout(() => dispatch({ type: "start", tourId }), 60);
  };

  const handleNext = () => {
    if (state.active && state.tourId === FULL_TOUR_ID
        && state.step >= ALL_TOURS[FULL_TOUR_ID].steps.length - 1) {
      onFullWalkthroughComplete && onFullWalkthroughComplete();
    }
    dispatch({ type: "next" });
  };

  let element = null;
  if (state.active) {
    element = (
      <MonsterTourCard
        tourId={state.tourId}
        step={state.step}
        onNext={handleNext}
        onBack={() => dispatch({ type: "back" })}
        onExit={() => dispatch({ type: "exit" })}
      />
    );
  } else if (askFocus !== null) {
    element = (
      <AskPrompt
        focusTopic={askFocus || undefined}
        onFull={() => startTour(FULL_TOUR_ID)}
        onTopic={(id) => startTour(id)}
        onClose={() => setAskFocus(null)}
      />
    );
  }
  return {
    element,
    active: state.active,
    ask: (focusTopic) => setAskFocus(focusTopic || ""),
    start: startTour,
  };
}

/** The ask-first prompt: full walkthrough, or just the one thing? */
function AskPrompt({ focusTopic, onFull, onTopic, onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const topics = focusTopic
    ? [...TOUR_IDS].sort((a, b) => (a === focusTopic ? -1 : b === focusTopic ? 1 : 0))
    : TOUR_IDS;
  return (
    <div data-testid="monster-ask" className="fixed inset-0 z-[90]">
      <div className="absolute inset-0 bg-[#020610]/55" onClick={onClose} />
      <div className="absolute inset-x-3 bottom-3 sm:left-1/2 sm:right-auto sm:bottom-6 sm:w-[430px] sm:-translate-x-1/2 rounded-2xl border border-amber-500/40 bg-[#14171E] p-4 shadow-2xl">
        <div className="flex items-start gap-3">
          <InvoiceAssistantMascot mood="eating" theme="dark" size={56} className="shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <h4 className="font-display font-semibold text-gray-50">How can I help?</h4>
              <button type="button" onClick={onClose} data-testid="ask-close"
                aria-label="Close"
                className="rounded-full p-1 text-gray-400 hover:text-gray-100 hover:bg-white/10">
                <X size={16} />
              </button>
            </div>
            <p className="mt-1 text-sm leading-relaxed text-gray-300" data-testid="ask-question">
              Want the full walkthrough, or just help with the one thing
              you're doing right now?
            </p>
            <button type="button" onClick={onFull} data-testid="ask-full"
              className="mt-3 w-full rounded-md bg-amber-600 px-3 py-2 text-sm font-semibold text-black hover:bg-amber-500">
              The full walkthrough — show me everything
            </button>
            <p className="mt-1 text-[11px] text-gray-500">
              It's here anytime you're ready — no rush, and asking never starts anything on its own.
            </p>
            <div className="mt-3 flex flex-wrap gap-1.5" data-testid="ask-topics">
              {topics.map((id) => (
                <button key={id} type="button" onClick={() => onTopic(id)}
                  data-testid={`ask-topic-${id}`}
                  className={`rounded-full border px-2.5 py-1 text-xs ${id === focusTopic
                    ? "border-amber-500/60 text-amber-200"
                    : "border-[#2A3040] text-gray-300"} hover:border-amber-500/50 hover:text-amber-200`}>
                  Just: {TOURS[id].title}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function MonsterTourCard({ tourId, step, onNext, onBack, onExit }) {
  const tour = ALL_TOURS[tourId];
  const def = tour.steps[step];
  const [rect, setRect] = useState(null);

  useEffect(() => {
    // Full-walkthrough steps span tabs — land on the step's tab first.
    if (def.tab === "settings") activateTab("tab-invoice-settings");
    else if (def.tab === "invoices") activateTab("tab-invoices");
    let raf;
    let tries = 0;
    const measure = () => {
      if (!def.target) { setRect(null); return true; }
      const el = document.querySelector(def.target);
      if (!el) { setRect(null); return false; }
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      const r = el.getBoundingClientRect();
      setRect({ top: r.top, left: r.left, width: r.width, height: r.height });
      return true;
    };
    // Tab-switch steps mount their targets asynchronously (e.g. the
    // Design Shop loads branding first) — retry for ~3s before giving up
    // on the highlight; the card shows regardless.
    const iv = setInterval(() => {
      tries += 1;
      if (measure() || tries > 14) clearInterval(iv);
    }, 220);
    const onMove = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(measure); };
    window.addEventListener("resize", onMove);
    return () => { clearInterval(iv); window.removeEventListener("resize", onMove); cancelAnimationFrame(raf); };
  }, [def]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onExit(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onExit]);

  const last = step === tour.steps.length - 1;
  return (
    <div data-testid="monster-tour" className="fixed inset-0 z-[90] pointer-events-none">
      {rect && (
        <div
          data-testid="tour-highlight"
          className="absolute rounded-lg ring-4 ring-amber-400 ring-offset-2 ring-offset-transparent transition-all duration-300"
          style={{
            top: rect.top - 6, left: rect.left - 6,
            width: rect.width + 12, height: rect.height + 12,
            boxShadow: "0 0 0 9999px rgba(2,6,16,0.55)",
          }}
        />
      )}
      {!rect && <div className="absolute inset-0 bg-[#020610]/55" />}
      <div
        data-testid="tour-card"
        className="pointer-events-auto absolute inset-x-3 bottom-3 sm:left-1/2 sm:right-auto sm:bottom-6 sm:w-[430px] sm:-translate-x-1/2 rounded-2xl border border-amber-500/40 bg-[#14171E] p-4 shadow-2xl"
      >
        <div className="flex items-start gap-3">
          <InvoiceAssistantMascot mood="eating" theme="dark" size={56} className="shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <p className="text-[11px] font-semibold uppercase tracking-wider text-amber-400">
                {def.section ? `${def.section} · ` : ""}Step {step + 1} of {tour.steps.length}
              </p>
              <button type="button" onClick={onExit} data-testid="tour-exit"
                aria-label="End tour"
                className="rounded-full p-1 text-gray-400 hover:text-gray-100 hover:bg-white/10">
                <X size={16} />
              </button>
            </div>
            <h4 className="mt-0.5 font-display font-semibold text-gray-50" data-testid="tour-step-title">{def.title}</h4>
            <p className="mt-1 text-sm leading-relaxed text-gray-300" data-testid="tour-step-text">{def.text}</p>
            <div className="mt-3 flex items-center gap-2">
              <button type="button" onClick={onBack} disabled={step === 0} data-testid="tour-back"
                className="inline-flex items-center gap-1 rounded-md border border-[#2A3040] px-3 py-1.5 text-sm text-gray-300 disabled:opacity-40 hover:bg-white/5">
                <ArrowLeft size={14} /> Back
              </button>
              <button type="button" onClick={onNext} data-testid="tour-next"
                className="inline-flex items-center gap-1 rounded-md bg-amber-600 px-3 py-1.5 text-sm font-semibold text-black hover:bg-amber-500">
                {last ? "Done" : "Next"} {!last && <ArrowRight size={14} />}
              </button>
              <span className="ml-auto flex gap-1" aria-hidden>
                {tour.steps.map((_, i) => (
                  <span key={i} className={`h-1.5 w-4 rounded-full ${i === step ? "bg-amber-400" : "bg-[#2A3040]"}`} />
                ))}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default MonsterTourCard;
