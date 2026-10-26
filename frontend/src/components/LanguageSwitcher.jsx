import React from "react";
import { useTranslation } from "react-i18next";
import { SUPPORTED_LANGUAGES, normalizeLanguage, setLanguage } from "@/lib/i18n";

// The empire language toggle: the same four languages everywhere it
// appears. Picking one switches the UI immediately and persists the
// choice (via lib/i18n's setLanguage) so it sticks across pages and
// return visits until a different language is picked.
export default function LanguageSwitcher({ compact = false }) {
  const { i18n } = useTranslation();
  const resolved = normalizeLanguage(i18n.language || "en");
  const current = SUPPORTED_LANGUAGES.some((l) => l.code === resolved)
    ? resolved
    : "en";

  const change = (e) => {
    setLanguage(e.target.value);
  };

  if (compact) {
    return (
      <select
        value={current}
        onChange={change}
        aria-label="Language"
        data-testid="language-switcher"
        style={{ padding: "4px 8px", borderRadius: 6 }}
      >
        {SUPPORTED_LANGUAGES.map((l) => (
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
        value={current}
        onChange={change}
        data-testid="language-switcher"
        style={{ padding: "6px 10px", borderRadius: 6 }}
      >
        {SUPPORTED_LANGUAGES.map((l) => (
          <option key={l.code} value={l.code}>
            {l.label}
          </option>
        ))}
      </select>
    </div>
  );
}
