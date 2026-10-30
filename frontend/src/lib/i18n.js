import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import en from "@/locales/en/translation.json";
import es from "@/locales/es/translation.json";
import fr from "@/locales/fr/translation.json";
import zhCN from "@/locales/zh-CN/translation.json";
import hi from "@/locales/hi/translation.json";
import ar from "@/locales/ar/translation.json";

// THE language registry. Adding a language = one entry here, one import
// + RESOURCES line below, and a locales/<code>/translation.json with every
// en key (tests/i18n-parity.py and lib/i18n.test.js enforce both).
//   code:  i18next code, also the locales/ folder name
//   label: the language's own name, shown in the switcher
//   intl:  BCP 47 locale for Intl number/date/money formatting
// Text direction comes from i18next (i18n.dir), which already knows the
// right-to-left languages (ar, he, fa, ur, ...).
export const SUPPORTED_LANGUAGES = [
  { code: "en", label: "English", flag: "🇺🇸", intl: "en-US" },
  { code: "es", label: "Español", flag: "🇪🇸", intl: "es-ES" },
  { code: "fr", label: "Français", flag: "🇫🇷", intl: "fr-FR" },
  { code: "zh-CN", label: "简体中文", flag: "🇨🇳", intl: "zh-CN" },
  { code: "hi", label: "हिन्दी", flag: "🇮🇳", intl: "hi-IN" },
  // Arabic text, Western digits: amounts stay in the 0-9 the books use.
  { code: "ar", label: "العربية", flag: "🌐", intl: "ar-u-nu-latn" },
];

const RESOURCES = {
  en,
  es,
  fr,
  "zh-CN": zhCN,
  hi,
  ar,
};

// The one localStorage key the language choice lives under. The detector
// reads it first on every load; setLanguage writes it on every explicit
// pick. Nothing else may write a language key (an earlier switcher wrote
// "i18nextLng", which nothing read — the pick only survived because the
// detector happened to cache changeLanguage under this key too).
export const LANGUAGE_STORAGE_KEY = "app-language";

// Map any detected/stored code (e.g. a browser's "es-MX", "fr-CA",
// "zh-TW", "hi-IN", "ar-SA") onto one of the supported codes, so i18n.language is
// always an exact toggle position and never a regional variant the
// switcher would mis-display as English. Genuinely unsupported codes
// pass through untouched: supportedLngs matching then skips them (a
// de-first/es-second browser still lands on Spanish) and the English
// fallback applies when nothing supported remains.
export const normalizeLanguage = (lng) => {
  if (!lng) return "en";
  const code = String(lng).toLowerCase();
  const exact = SUPPORTED_LANGUAGES.find((l) => l.code.toLowerCase() === code);
  if (exact) return exact.code;
  // Same base language (es-MX -> es, zh-TW -> zh-CN, ar-SA -> ar).
  const base = code.split("-")[0];
  const byBase = SUPPORTED_LANGUAGES.find((l) => l.code.toLowerCase().split("-")[0] === base);
  return byBase ? byBase.code : lng;
};

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: Object.fromEntries(
      SUPPORTED_LANGUAGES.map(({ code }) => [code, { translation: RESOURCES[code] }])
    ),
    supportedLngs: SUPPORTED_LANGUAGES.map((l) => l.code),
    fallbackLng: "en",
    defaultNS: "translation",
    interpolation: { escapeValue: false },
    detection: {
      // Precedence: the visitor's stored explicit choice first, then
      // their browser language, then the English fallback above.
      order: ["localStorage", "navigator"],
      caches: ["localStorage"],
      lookupLocalStorage: LANGUAGE_STORAGE_KEY,
      convertDetectedLanguage: normalizeLanguage,
    },
    react: { useSuspense: false },
  });

// The single way anything changes the language: switch i18next now and
// persist the pick under LANGUAGE_STORAGE_KEY so it sticks across pages
// and return visits until the visitor picks a different one.
export const setLanguage = (code) => {
  const normalized = normalizeLanguage(code);
  i18n.changeLanguage(normalized);
  try {
    localStorage.setItem(LANGUAGE_STORAGE_KEY, normalized);
  } catch {
    /* storage unavailable */
  }
};

export const getActiveLocale = () => {
  const code = normalizeLanguage(i18n.language || "en");
  // Map i18next codes to BCP 47 locales for Intl (from the registry).
  return (SUPPORTED_LANGUAGES.find((l) => l.code === code) || SUPPORTED_LANGUAGES[0]).intl;
};

// Arabic reads right to left: the page direction (and lang) follow the
// language so the whole layout mirrors, not just the text.
const syncDocumentLanguage = (lng) => {
  try {
    const code = normalizeLanguage(lng || i18n.language);
    document.documentElement.lang = code;
    document.documentElement.dir = i18n.dir(code);
  } catch {
    /* no document (tests, native shell startup) */
  }
};
i18n.on("languageChanged", syncDocumentLanguage);
syncDocumentLanguage(i18n.language);

export default i18n;
