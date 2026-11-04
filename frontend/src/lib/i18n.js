import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import en from "@/locales/en/translation.json";
import es from "@/locales/es/translation.json";
import fr from "@/locales/fr/translation.json";
import zhCN from "@/locales/zh-CN/translation.json";
import ht from "@/locales/ht/translation.json";
import ar from "@/locales/ar/translation.json";
import vi from "@/locales/vi/translation.json";
import tl from "@/locales/tl/translation.json";
import hi from "@/locales/hi/translation.json";
import ko from "@/locales/ko/translation.json";
import pt from "@/locales/pt/translation.json";
import so from "@/locales/so/translation.json";
import ur from "@/locales/ur/translation.json";
import bn from "@/locales/bn/translation.json";
import pa from "@/locales/pa/translation.json";
import fa from "@/locales/fa/translation.json";
import am from "@/locales/am/translation.json";
import my from "@/locales/my/translation.json";
import ne from "@/locales/ne/translation.json";
import sw from "@/locales/sw/translation.json";
import loc_te from "@/locales/te/translation.json";

export const SUPPORTED_LANGUAGES = [
  { code: "en", label: "English", flag: "🇺🇸" },
  { code: "es", label: "Español", flag: "🇪🇸" },
  { code: "fr", label: "Français", flag: "🇫🇷" },
  { code: "zh-CN", label: "简体中文", flag: "🇨🇳" },
  { code: "ht", label: "Kreyòl Ayisyen", flag: "🇭🇹" },
  { code: "ar", label: "العربية", flag: "🇸🇦" },
  { code: "vi", label: "Tiếng Việt", flag: "🇻🇳" },
  { code: "tl", label: "Tagalog", flag: "🇵🇭" },
  { code: "hi", label: "हिन्दी", flag: "🇮🇳" },
  { code: "ko", label: "한국어", flag: "🇰🇷" },
  { code: "pt", label: "Português", flag: "🇧🇷" },
  { code: "so", label: "Soomaali", flag: "🇸🇴" },
  { code: "ur", label: "اردو", flag: "🇵🇰" },
  { code: "bn", label: "বাংলা", flag: "🇧🇩" },
  { code: "pa", label: "ਪੰਜਾਬੀ", flag: "🇮🇳" },
  { code: "fa", label: "فارسی", flag: "🇮🇷" },
  { code: "am", label: "አማርኛ", flag: "🇪🇹" },
  { code: "my", label: "မြန်မာ", flag: "🇲🇲" },
  { code: "ne", label: "नेपाली", flag: "🇳🇵" },
  { code: "sw", label: "Kiswahili", flag: "🇰🇪" },
  { code: "te", label: "తెలుగు", flag: "🌐" },
];

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      es: { translation: es },
      fr: { translation: fr },
      "zh-CN": { translation: zhCN },
      ht: { translation: ht },
      ar: { translation: ar },
      vi: { translation: vi },
      tl: { translation: tl },
      hi: { translation: hi },
      ko: { translation: ko },
      pt: { translation: pt },
      so: { translation: so },
      ur: { translation: ur },
      bn: { translation: bn },
      pa: { translation: pa },
      fa: { translation: fa },
      am: { translation: am },
      my: { translation: my },
      ne: { translation: ne },
      sw: { translation: sw },
      "te": { translation: loc_te },
    },
    fallbackLng: "en",
    defaultNS: "translation",
    interpolation: { escapeValue: false },
    detection: {
      order: ["localStorage", "navigator"],
      caches: ["localStorage"],
      lookupLocalStorage: "app-language",
    },
    react: { useSuspense: false },
  });

export const RTL_LANGUAGES = ["ar", "ur", "fa"];

export const isRtl = (lng) =>
  RTL_LANGUAGES.includes(String(lng || "").split("-")[0]);

const applyDirection = (lng) => {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("dir", isRtl(lng) ? "rtl" : "ltr");
  document.documentElement.setAttribute("lang", lng || "en");
};

applyDirection(i18n.language);
i18n.on("languageChanged", applyDirection);

export const setLanguage = (code) => {
  i18n.changeLanguage(code);
  try {
    localStorage.setItem("app-language", code);
  } catch {
    /* storage unavailable */
  }
};

export const getActiveLocale = () => {
  const lng = i18n.language || "en";
  // Map i18next codes to BCP 47 locales for Intl
  if (lng.startsWith("zh")) return "zh-CN";
  if (lng.startsWith("es")) return "es-ES";
  if (lng.startsWith("fr")) return "fr-FR";
  const extra = {ht: "ht-HT", ar: "ar", vi: "vi-VN", tl: "fil-PH", hi: "hi-IN", ko: "ko-KR", pt: "pt-BR", so: "so-SO", ur: "ur-PK", bn: "bn-BD", pa: "pa-IN", fa: "fa-IR", am: "am-ET", my: "my-MM", ne: "ne-NP", sw: "sw-KE"};
  const base = lng.split("-")[0];
  if (extra[base]) return extra[base];
  return "en-US";
};

export default i18n;
