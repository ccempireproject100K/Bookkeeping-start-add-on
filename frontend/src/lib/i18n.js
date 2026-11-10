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
import az from "@/locales/az/translation.json";
import bg from "@/locales/bg/translation.json";
import bho from "@/locales/bho/translation.json";
import bs from "@/locales/bs/translation.json";
import ca from "@/locales/ca/translation.json";
import ceb from "@/locales/ceb/translation.json";
import cs from "@/locales/cs/translation.json";
import da from "@/locales/da/translation.json";
import de from "@/locales/de/translation.json";
import el from "@/locales/el/translation.json";
import et from "@/locales/et/translation.json";
import ff from "@/locales/ff/translation.json";
import fi from "@/locales/fi/translation.json";
import gu from "@/locales/gu/translation.json";
import ha from "@/locales/ha/translation.json";
import he from "@/locales/he/translation.json";
import hr from "@/locales/hr/translation.json";
import hu from "@/locales/hu/translation.json";
import hy from "@/locales/hy/translation.json";
import id from "@/locales/id/translation.json";
import ig from "@/locales/ig/translation.json";
import ilo from "@/locales/ilo/translation.json";
import it from "@/locales/it/translation.json";
import ja from "@/locales/ja/translation.json";
import jv from "@/locales/jv/translation.json";
import ka from "@/locales/ka/translation.json";
import kk from "@/locales/kk/translation.json";
import km from "@/locales/km/translation.json";
import kn from "@/locales/kn/translation.json";
import ku from "@/locales/ku/translation.json";
import ln from "@/locales/ln/translation.json";
import lo from "@/locales/lo/translation.json";
import lt from "@/locales/lt/translation.json";
import lv from "@/locales/lv/translation.json";
import mg from "@/locales/mg/translation.json";
import mk from "@/locales/mk/translation.json";
import ml from "@/locales/ml/translation.json";
import mr from "@/locales/mr/translation.json";
import ms from "@/locales/ms/translation.json";
import nl from "@/locales/nl/translation.json";
import no from "@/locales/no/translation.json";
import om from "@/locales/om/translation.json";
import pl from "@/locales/pl/translation.json";
import prs from "@/locales/prs/translation.json";
import ps from "@/locales/ps/translation.json";
import qu from "@/locales/qu/translation.json";
import rn from "@/locales/rn/translation.json";
import ro from "@/locales/ro/translation.json";
import ru from "@/locales/ru/translation.json";
import rw from "@/locales/rw/translation.json";
import si from "@/locales/si/translation.json";
import sk from "@/locales/sk/translation.json";
import sl from "@/locales/sl/translation.json";
import sm from "@/locales/sm/translation.json";
import sq from "@/locales/sq/translation.json";
import sr from "@/locales/sr/translation.json";
import sv from "@/locales/sv/translation.json";
import ta from "@/locales/ta/translation.json";
import th from "@/locales/th/translation.json";
import ti from "@/locales/ti/translation.json";
import tr from "@/locales/tr/translation.json";
import tw from "@/locales/tw/translation.json";
import uk from "@/locales/uk/translation.json";
import uz from "@/locales/uz/translation.json";
import wo from "@/locales/wo/translation.json";
import yi from "@/locales/yi/translation.json";
import yo from "@/locales/yo/translation.json";
import yue from "@/locales/yue/translation.json";
import zh from "@/locales/zh/translation.json";
import zu from "@/locales/zu/translation.json";

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
  { code: "az", label: "Azərbaycanca", flag: "🇦🇿" },
  { code: "bg", label: "Български", flag: "🇧🇬" },
  { code: "bho", label: "भोजपुरी", flag: "🇮🇳" },
  { code: "bs", label: "Bosanski", flag: "🇧🇦" },
  { code: "ca", label: "Català", flag: "🌐" },
  { code: "ceb", label: "Cebuano", flag: "🇵🇭" },
  { code: "cs", label: "Čeština", flag: "🇨🇿" },
  { code: "da", label: "Dansk", flag: "🇩🇰" },
  { code: "de", label: "Deutsch", flag: "🇩🇪" },
  { code: "el", label: "Ελληνικά", flag: "🇬🇷" },
  { code: "et", label: "Eesti", flag: "🇪🇪" },
  { code: "ff", label: "Fulfulde", flag: "🌐" },
  { code: "fi", label: "Suomi", flag: "🇫🇮" },
  { code: "gu", label: "ગુજરાતી", flag: "🇮🇳" },
  { code: "ha", label: "Hausa", flag: "🇳🇬" },
  { code: "he", label: "עברית", flag: "🇮🇱" },
  { code: "hr", label: "Hrvatski", flag: "🇭🇷" },
  { code: "hu", label: "Magyar", flag: "🇭🇺" },
  { code: "hy", label: "Հայերեն", flag: "🇦🇲" },
  { code: "id", label: "Bahasa Indonesia", flag: "🇮🇩" },
  { code: "ig", label: "Igbo", flag: "🇳🇬" },
  { code: "ilo", label: "Ilokano", flag: "🇵🇭" },
  { code: "it", label: "Italiano", flag: "🇮🇹" },
  { code: "ja", label: "日本語", flag: "🇯🇵" },
  { code: "jv", label: "Basa Jawa", flag: "🇮🇩" },
  { code: "ka", label: "ქართული", flag: "🇬🇪" },
  { code: "kk", label: "Қазақ", flag: "🇰🇿" },
  { code: "km", label: "ខ្មែរ", flag: "🇰🇭" },
  { code: "kn", label: "ಕನ್ನಡ", flag: "🇮🇳" },
  { code: "ku", label: "Kurdî", flag: "🌐" },
  { code: "ln", label: "Lingála", flag: "🇨🇩" },
  { code: "lo", label: "ລາວ", flag: "🇱🇦" },
  { code: "lt", label: "Lietuvių", flag: "🇱🇹" },
  { code: "lv", label: "Latviešu", flag: "🇱🇻" },
  { code: "mg", label: "Malagasy", flag: "🇲🇬" },
  { code: "mk", label: "Македонски", flag: "🇲🇰" },
  { code: "ml", label: "മലയാളം", flag: "🇮🇳" },
  { code: "mr", label: "मराठी", flag: "🇮🇳" },
  { code: "ms", label: "Bahasa Melayu", flag: "🇲🇾" },
  { code: "nl", label: "Nederlands", flag: "🇳🇱" },
  { code: "no", label: "Norsk", flag: "🇳🇴" },
  { code: "om", label: "Afaan Oromoo", flag: "🇪🇹" },
  { code: "pl", label: "Polski", flag: "🇵🇱" },
  { code: "prs", label: "دری", flag: "🇦🇫" },
  { code: "ps", label: "پښتو", flag: "🇦🇫" },
  { code: "qu", label: "Runasimi", flag: "🌐" },
  { code: "rn", label: "Ikirundi", flag: "🇧🇮" },
  { code: "ro", label: "Română", flag: "🇷🇴" },
  { code: "ru", label: "Русский", flag: "🇷🇺" },
  { code: "rw", label: "Kinyarwanda", flag: "🇷🇼" },
  { code: "si", label: "සිංහල", flag: "🇱🇰" },
  { code: "sk", label: "Slovenčina", flag: "🇸🇰" },
  { code: "sl", label: "Slovenščina", flag: "🇸🇮" },
  { code: "sm", label: "Gagana Sāmoa", flag: "🇼🇸" },
  { code: "sq", label: "Shqip", flag: "🇦🇱" },
  { code: "sr", label: "Српски", flag: "🇷🇸" },
  { code: "sv", label: "Svenska", flag: "🇸🇪" },
  { code: "ta", label: "தமிழ்", flag: "🇮🇳" },
  { code: "th", label: "ไทย", flag: "🇹🇭" },
  { code: "ti", label: "ትግርኛ", flag: "🇪🇷" },
  { code: "tr", label: "Türkçe", flag: "🇹🇷" },
  { code: "tw", label: "Twi", flag: "🇬🇭" },
  { code: "uk", label: "Українська", flag: "🇺🇦" },
  { code: "uz", label: "Oʻzbek", flag: "🇺🇿" },
  { code: "wo", label: "Wolof", flag: "🇸🇳" },
  { code: "yi", label: "ייִדיש", flag: "🌐" },
  { code: "yo", label: "Yorùbá", flag: "🇳🇬" },
  { code: "yue", label: "粵語", flag: "🌐" },
  { code: "zh", label: "简体中文", flag: "🇨🇳" },
  { code: "zu", label: "isiZulu", flag: "🇿🇦" },
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
      az: { translation: az },
      bg: { translation: bg },
      bho: { translation: bho },
      bs: { translation: bs },
      ca: { translation: ca },
      ceb: { translation: ceb },
      cs: { translation: cs },
      da: { translation: da },
      de: { translation: de },
      el: { translation: el },
      et: { translation: et },
      ff: { translation: ff },
      fi: { translation: fi },
      gu: { translation: gu },
      ha: { translation: ha },
      he: { translation: he },
      hr: { translation: hr },
      hu: { translation: hu },
      hy: { translation: hy },
      id: { translation: id },
      ig: { translation: ig },
      ilo: { translation: ilo },
      it: { translation: it },
      ja: { translation: ja },
      jv: { translation: jv },
      ka: { translation: ka },
      kk: { translation: kk },
      km: { translation: km },
      kn: { translation: kn },
      ku: { translation: ku },
      ln: { translation: ln },
      lo: { translation: lo },
      lt: { translation: lt },
      lv: { translation: lv },
      mg: { translation: mg },
      mk: { translation: mk },
      ml: { translation: ml },
      mr: { translation: mr },
      ms: { translation: ms },
      nl: { translation: nl },
      no: { translation: no },
      om: { translation: om },
      pl: { translation: pl },
      prs: { translation: prs },
      ps: { translation: ps },
      qu: { translation: qu },
      rn: { translation: rn },
      ro: { translation: ro },
      ru: { translation: ru },
      rw: { translation: rw },
      si: { translation: si },
      sk: { translation: sk },
      sl: { translation: sl },
      sm: { translation: sm },
      sq: { translation: sq },
      sr: { translation: sr },
      sv: { translation: sv },
      ta: { translation: ta },
      th: { translation: th },
      ti: { translation: ti },
      tr: { translation: tr },
      tw: { translation: tw },
      uk: { translation: uk },
      uz: { translation: uz },
      wo: { translation: wo },
      yi: { translation: yi },
      yo: { translation: yo },
      yue: { translation: yue },
      zh: { translation: zh },
      zu: { translation: zu },
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

export const RTL_LANGUAGES = ["ar", "ur", "fa", "he", "ps", "prs", "yi"];

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
  const extra = {ht: "ht-HT", ar: "ar", vi: "vi-VN", tl: "fil-PH", hi: "hi-IN", ko: "ko-KR", pt: "pt-BR", so: "so-SO", ur: "ur-PK", bn: "bn-BD", pa: "pa-IN", fa: "fa-IR", am: "am-ET", my: "my-MM", ne: "ne-NP", sw: "sw-KE", az: "az-AZ", bg: "bg-BG", bho: "bho-IN", bs: "bs-BA", ca: "ca-ES", ceb: "ceb-PH", cs: "cs-CZ", da: "da-DK", de: "de-DE", el: "el-GR", et: "et-EE", ff: "ff", fi: "fi-FI", gu: "gu-IN", ha: "ha-NG", he: "he-IL", hr: "hr-HR", hu: "hu-HU", hy: "hy-AM", id: "id-ID", ig: "ig-NG", ilo: "ilo-PH", it: "it-IT", ja: "ja-JP", jv: "jv-ID", ka: "ka-GE", kk: "kk-KZ", km: "km-KH", kn: "kn-IN", ku: "ku", ln: "ln-CD", lo: "lo-LA", lt: "lt-LT", lv: "lv-LV", mg: "mg-MG", mk: "mk-MK", ml: "ml-IN", mr: "mr-IN", ms: "ms-MY", nl: "nl-NL", no: "no-NO", om: "om-ET", pl: "pl-PL", prs: "prs-AF", ps: "ps-AF", qu: "qu-PE", rn: "rn-BI", ro: "ro-RO", ru: "ru-RU", rw: "rw-RW", si: "si-LK", sk: "sk-SK", sl: "sl-SI", sm: "sm-WS", sq: "sq-AL", sr: "sr-RS", sv: "sv-SE", ta: "ta-IN", th: "th-TH", ti: "ti-ER", tr: "tr-TR", tw: "tw-GH", uk: "uk-UA", uz: "uz-UZ", wo: "wo-SN", yi: "yi", yo: "yo-NG", yue: "yue-HK", zu: "zu-ZA"};
  const base = lng.split("-")[0];
  if (extra[base]) return extra[base];
  return "en-US";
};

export default i18n;
