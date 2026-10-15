import React from "react";
import { useTranslation } from "react-i18next";

const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "es", label: "Español" },
  { code: "fr", label: "Français" },
  { code: "zh-CN", label: "简体中文" },
];

export default function LanguageSwitcher({ compact = false }) {
  const { i18n } = useTranslation();
  const current = i18n.language || "en";

  const change = (e) => {
    const lng = e.target.value;
    i18n.changeLanguage(lng);
    try {
      localStorage.setItem("i18nextLng", lng);
    } catch {}
  };

  if (compact) {
    return (
      <select
        value={LANGUAGES.some((l) => l.code === current) ? current : "en"}
        onChange={change}
        aria-label="Language"
        style={{ padding: "4px 8px", borderRadius: 6 }}
      >
        {LANGUAGES.map((l) => (
          <option key={l.code} value={l.code}>
            {l.label}
          </option>
        ))}
      </select>
    );
  }

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <label htmlFor="language-select" style={{ fontWeight: 600 }}>
        Language / Idioma / Langue / 语言
      </label>
      <select
        id="language-select"
        value={LANGUAGES.some((l) => l.code === current) ? current : "en"}
        onChange={change}
        style={{ padding: "6px 10px", borderRadius: 6 }}
      >
        {LANGUAGES.map((l) => (
          <option key={l.code} value={l.code}>
            {l.label}
          </option>
        ))}
      </select>
    </div>
  );
}
