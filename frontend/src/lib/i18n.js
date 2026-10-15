import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import en from "@/locales/en/translation.json";
import es from "@/locales/es/translation.json";
import fr from "@/locales/fr/translation.json";
import zhCN from "@/locales/zh-CN/translation.json";

export const SUPPORTED_LANGUAGES = [
  { code: "en", label: "English", flag: "🇺🇸" },
  { code: "es", label: "Español", flag: "🇪🇸" },
  { code: "fr", label: "Français", flag: "🇫🇷" },
  { code: "zh-CN", label: "简体中文", flag: "🇨🇳" },
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
  return "en-US";
};

export default i18n;
