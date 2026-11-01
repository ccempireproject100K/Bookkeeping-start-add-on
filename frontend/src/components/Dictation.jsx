// Tap-and-talk voice input for the public forms, built on the browser's
// built-in speech recognition (the pattern Trade Circle's Dictation.jsx
// proved in production). Nothing is recorded by us and no audio leaves
// the browser's own speech service: the recognizer turns speech into
// text locally in the page, and only text the visitor keeps in a field
// is ever submitted — by the visitor, with the form's own button.
//
// Two deliberate rules from the owner's voice standard:
//   * where the browser has no speech recognition, these controls
//     render NOTHING at all (no dead buttons, no apology text);
//   * the recognition language follows the page's selected language,
//     never the browser's own locale guess.
// Password fields never get a microphone: speaking a password aloud
// defeats it.
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Mic, Square } from "lucide-react";

const Recognition =
  typeof window !== "undefined"
    ? window.SpeechRecognition || window.webkitSpeechRecognition
    : undefined;

// Page language (i18next code) -> BCP 47 code the recognizer and the
// speech synthesizer both speak. Mirrors getActiveLocale() in lib/i18n.
export const VOICE_LANGS = {
  en: "en-US",
  es: "es-ES",
  fr: "fr-FR",
  "zh-CN": "zh-CN",
  hi: "hi-IN",
  ar: "ar-SA",
};

export function voiceLangFor(lng) {
  if (!lng) return VOICE_LANGS.en;
  const code = String(lng).toLowerCase();
  if (code.startsWith("zh")) return VOICE_LANGS["zh-CN"];
  if (code.startsWith("es")) return VOICE_LANGS.es;
  if (code.startsWith("fr")) return VOICE_LANGS.fr;
  if (code.startsWith("hi")) return VOICE_LANGS.hi;
  if (code.startsWith("ar")) return VOICE_LANGS.ar;
  return VOICE_LANGS.en;
}

// Spoken commands for the guided walk, one set per page language. A
// transcript that is exactly one of these phrases (after trimming
// punctuation) steers the walk instead of filling the field.
export const VOICE_COMMANDS = {
  "en-US": {
    next: ["next", "skip"],
    back: ["go back", "back"],
    stop: ["stop", "done", "finished"],
  },
  "es-ES": {
    next: ["siguiente", "saltar"],
    back: ["atrás", "volver"],
    stop: ["parar", "detener", "terminado", "listo"],
  },
  "fr-FR": {
    next: ["suivant", "passer"],
    back: ["retour", "revenir"],
    stop: ["stop", "arrêter", "terminé", "fini"],
  },
  "zh-CN": {
    next: ["下一个", "跳过"],
    back: ["上一个", "返回"],
    stop: ["停止", "完成", "好了"],
  },
  "hi-IN": {
    next: ["अगला", "छोड़ दो"],
    back: ["पीछे", "वापस"],
    stop: ["रोको", "हो गया"],
  },
  "ar-SA": {
    next: ["التالي", "تخطي"],
    back: ["السابق", "ارجع"],
    stop: ["توقف", "انتهيت"],
  },
};

export function matchVoiceCommand(transcript, voiceLang) {
  const table = VOICE_COMMANDS[voiceLang] || VOICE_COMMANDS["en-US"];
  const norm = String(transcript || "")
    .trim()
    .toLowerCase()
    .replace(/[\s.!?。！？,،、]+$/u, "")
    .trim();
  if (!norm) return null;
  for (const kind of ["next", "back", "stop"]) {
    if (table[kind].includes(norm)) return kind;
  }
  return null;
}

// A dictated email ("ada at example dot com") becomes the address
// itself. Applied to email fields only, in the walk and the correction
// button alike; without the spoken separators it simply strips spaces.
export function normalizeEmailDictation(text) {
  const tokens = String(text || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
  return tokens.map((tok) => (tok === "at" ? "@" : tok === "dot" ? "." : tok)).join("");
}

// Shared recognizer driver. Handlers are read through a ref so callers
// can pass fresh closures every render without restarting the session.
//   onFinal(text)          — a finished transcript chunk
//   onError(kind)          — "denied" | "error" | "langError"
//   onEnd({ gotFinal })    — the session ended (stop, pause, or error)
export function useDictation({ onFinal, onEnd, onError } = {}) {
  const [state, setState] = useState(Recognition ? "idle" : "unsupported");
  const [interim, setInterim] = useState("");
  const recRef = useRef(null);
  const handlers = useRef({ onFinal, onEnd, onError });
  handlers.current = { onFinal, onEnd, onError };
  const gotFinalRef = useRef(false);

  const start = useCallback((lang, { continuous = true } = {}) => {
    if (!Recognition || recRef.current) return false;
    const r = new Recognition();
    r.lang = lang || "en-US";
    r.continuous = continuous;
    r.interimResults = true;
    gotFinalRef.current = false;
    r.onresult = (event) => {
      let finished = "";
      let pending = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const text = event.results[i][0].transcript;
        if (event.results[i].isFinal) finished += text;
        else pending += text;
      }
      if (finished.trim()) {
        gotFinalRef.current = true;
        if (handlers.current.onFinal) handlers.current.onFinal(finished.trim());
      }
      setInterim(pending);
    };
    r.onerror = (event) => {
      const kind =
        event.error === "not-allowed" || event.error === "service-not-allowed"
          ? "denied"
          : event.error === "language-not-supported"
            ? "langError"
            : "error";
      setState(kind);
      if (handlers.current.onError) handlers.current.onError(kind);
    };
    r.onend = () => {
      recRef.current = null;
      setInterim("");
      setState((s) => (s === "listening" ? "idle" : s));
      if (handlers.current.onEnd) handlers.current.onEnd({ gotFinal: gotFinalRef.current });
    };
    recRef.current = r;
    setState("listening");
    try {
      r.start();
    } catch {
      recRef.current = null;
      setState("error");
      if (handlers.current.onError) handlers.current.onError("error");
      return false;
    }
    return true;
  }, []);

  const stop = useCallback(() => {
    try {
      if (recRef.current) recRef.current.stop();
    } catch {
      /* the session already ended */
    }
  }, []);

  // Leaving the page always stops the microphone.
  useEffect(
    () => () => {
      try {
        if (recRef.current) recRef.current.abort();
      } catch {
        /* nothing to stop */
      }
      recRef.current = null;
    },
    []
  );

  return { supported: !!Recognition, state, interim, start, stop };
}

const noticeCls = "text-[11px] text-red-400 mt-1";
const hintCls = "text-[11px] text-gray-500 mt-1";

/** Small per-field correction microphone: tap, talk, and the dictated
 *  text is handed to onText (the field's owner decides how it lands —
 *  append, whitespace-stripped, email-normalized). Renders nothing on a
 *  browser without speech recognition. */
export function DictateButton({ onText }) {
  const { t, i18n } = useTranslation();
  const lang = voiceLangFor(i18n.language);
  const { supported, state, interim, start, stop } = useDictation({
    onFinal: (text) => onText(text),
  });
  if (!supported) return null;
  const listening = state === "listening";
  return (
    <div className="shrink-0">
      <button
        type="button"
        data-testid="dictate-button"
        aria-label={listening ? t("dictation.stop") : t("dictation.tapToTalk")}
        title={listening ? t("dictation.stop") : t("dictation.tapToTalk")}
        onClick={listening ? stop : () => start(lang, { continuous: true })}
        className={`inline-flex items-center justify-center w-9 h-9 rounded-md border ${
          listening
            ? "border-red-500 text-red-400 animate-pulse"
            : "border-[#222838] text-gray-300 hover:text-amber-400 hover:border-amber-500"
        } bg-[#14171E]`}
      >
        {listening ? <Square size={14} /> : <Mic size={14} />}
      </button>
      {listening && (
        <p role="status" className="text-[11px] text-gray-400 mt-1">
          {t("dictation.listening")}
        </p>
      )}
      {listening && interim && <p className="text-[11px] text-gray-500 mt-1">{interim}</p>}
      {state === "denied" && <p className={noticeCls}>{t("dictation.denied")}</p>}
      {state === "error" && <p className={noticeCls}>{t("dictation.error")}</p>}
      {state === "langError" && <p className={noticeCls}>{t("dictation.langError")}</p>}
      <p className="text-[11px] text-gray-600 mt-1 max-w-[200px]">{t("dictation.browserNote")}</p>
    </div>
  );
}

/** The ONE primary voice control for a form: a single press walks the
 *  visitor through the fields — highlight + focus the current field,
 *  speak and show its prompt, listen, fill it, move on. Spoken commands
 *  (per language): next/skip, go back, stop/done. A pause — the
 *  recognizer ending with nothing said — also advances gracefully, so
 *  the visitor is never trapped. The walk NEVER submits: it ends by
 *  telling the visitor to review and tap the form's own button.
 *
 *  fields: [{ key, id, label, transform? }] — id is the input's DOM id
 *  (used for highlight/focus), label is the on-screen field label,
 *  transform optionally reshapes the dictated text (emails).
 *  onFill(key, value) replaces the field's value. */
export function GuidedVoiceFill({ fields, onFill }) {
  const { t, i18n } = useTranslation();
  const lang = voiceLangFor(i18n.language);
  const [walking, setWalking] = useState(false);
  const [step, setStep] = useState(0);
  const [notice, setNotice] = useState(null);
  const fieldsRef = useRef(fields);
  fieldsRef.current = fields;
  const onFillRef = useRef(onFill);
  onFillRef.current = onFill;
  const session = useRef({ active: false, stopRequested: false, errorKind: null, result: null });

  const speak = useCallback(
    (text) => {
      try {
        if (typeof window !== "undefined" && window.speechSynthesis) {
          window.speechSynthesis.cancel();
          const utterance = new window.SpeechSynthesisUtterance(text);
          utterance.lang = lang;
          window.speechSynthesis.speak(utterance);
        }
      } catch {
        /* synthesis is a bonus; the on-screen prompt carries the walk */
      }
    },
    [lang]
  );

  const setHighlight = useCallback((idx) => {
    fieldsRef.current.forEach((f, i) => {
      const el = document.getElementById(f.id);
      if (el) el.classList.toggle("voice-walk-highlight", idx !== null && i === idx);
    });
    if (idx !== null) {
      const el = document.getElementById(fieldsRef.current[idx].id);
      if (el) el.focus();
    }
  }, []);

  const endWalk = useCallback(
    (kind) => {
      session.current.active = false;
      setWalking(false);
      setHighlight(null);
      try {
        if (typeof window !== "undefined" && window.speechSynthesis) {
          window.speechSynthesis.cancel();
        }
      } catch {
        /* nothing to cancel */
      }
      setNotice(kind);
    },
    [setHighlight]
  );

  const beginStep = useCallback(
    (idx) => {
      const f = fieldsRef.current[idx];
      setHighlight(idx);
      speak(t("dictation.walkPrompt", { label: f.label }));
      start(lang, { continuous: false });
    },
    // `start` is stable (useCallback in useDictation); t/lang/speak/setHighlight too.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [lang, speak, setHighlight, t]
  );

  const { supported, interim, start, stop } = useDictation({
    onFinal: (text) => {
      session.current.result = text;
    },
    onError: (kind) => {
      session.current.errorKind = kind;
    },
    onEnd: () => {
      const s = session.current;
      if (!s.active) return;
      if (s.errorKind) {
        endWalk(s.errorKind);
        return;
      }
      if (s.stopRequested) {
        endWalk("stopped");
        return;
      }
      const flds = fieldsRef.current;
      const current = flds[step];
      const text = s.result;
      s.result = null;
      let nextStep = step + 1;
      if (text && current) {
        const cmd = matchVoiceCommand(text, lang);
        if (cmd === "stop") {
          endWalk("stopped");
          return;
        }
        if (cmd === "back") nextStep = Math.max(0, step - 1);
        else if (cmd !== "next") {
          onFillRef.current(current.key, current.transform ? current.transform(text) : text);
        }
      }
      // No text at all (a pause) advances too — never trap the visitor.
      if (nextStep >= flds.length) {
        endWalk("done");
        return;
      }
      setStep(nextStep);
      beginStep(nextStep);
    },
  });

  const begin = () => {
    setNotice(null);
    session.current = { active: true, stopRequested: false, errorKind: null, result: null };
    setStep(0);
    setWalking(true);
    beginStep(0);
  };

  const requestStop = () => {
    session.current.stopRequested = true;
    stop();
    // If the recognizer had already ended on its own, onEnd may never
    // fire again — close the walk out from here instead of hanging.
    window.setTimeout(() => {
      if (session.current.active && session.current.stopRequested) endWalk("stopped");
    }, 350);
  };

  // Unmount: stop highlighting and silence any spoken prompt.
  useEffect(
    () => () => {
      session.current.active = false;
      fieldsRef.current.forEach((f) => {
        const el = document.getElementById(f.id);
        if (el) el.classList.remove("voice-walk-highlight");
      });
      try {
        if (typeof window !== "undefined" && window.speechSynthesis) {
          window.speechSynthesis.cancel();
        }
      } catch {
        /* nothing to cancel */
      }
    },
    []
  );

  if (!supported) return null;

  const currentField = fields[step];
  return (
    <div data-testid="guided-voice-fill">
      {!walking ? (
        <button
          type="button"
          onClick={begin}
          className="inline-flex items-center gap-2 rounded-md border border-amber-500 px-3 py-2 text-sm font-semibold text-amber-400 hover:bg-[#171a21]"
        >
          <Mic size={16} /> {t("dictation.fillByTalking")}
        </button>
      ) : (
        <div className="rounded-md border border-[#222838] bg-[#14171E] p-3">
          <p role="status" aria-live="polite" className="text-sm text-gray-100">
            {currentField ? t("dictation.walkPrompt", { label: currentField.label }) : ""}
          </p>
          {interim && <p className="text-xs text-gray-500 mt-1">{interim}</p>}
          <p className={hintCls}>{t("dictation.commandsHint")}</p>
          <button
            type="button"
            onClick={requestStop}
            className="mt-2 inline-flex items-center gap-2 rounded-md border border-red-500 px-3 py-1.5 text-xs font-semibold text-red-400"
          >
            <Square size={12} /> {t("dictation.stop")}
          </button>
        </div>
      )}
      {notice === "done" && (
        <p role="status" className="text-[11px] text-emerald-400 mt-1">
          {t("dictation.walkDone")}
        </p>
      )}
      {notice === "stopped" && (
        <p role="status" className={hintCls}>
          {t("dictation.walkStopped")}
        </p>
      )}
      {notice === "denied" && <p className={noticeCls}>{t("dictation.denied")}</p>}
      {notice === "error" && <p className={noticeCls}>{t("dictation.error")}</p>}
      {notice === "langError" && <p className={noticeCls}>{t("dictation.langError")}</p>}
      <p className={hintCls}>{t("dictation.privacy")}</p>
      <p className={hintCls}>{t("dictation.browserNote")}</p>
    </div>
  );
}
