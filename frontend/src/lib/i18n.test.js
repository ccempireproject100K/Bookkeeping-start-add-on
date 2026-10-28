// Sticky language toggle — behavior pins for lib/i18n + LanguageSwitcher.
//
// The owner's ruling (2026-10-26): the same four-language toggle on every
// page, and once a visitor picks a language it STAYS picked — across
// pages and return visits — until they pick a different one. These tests
// pin the mechanism behind that ruling:
//
//   precedence: stored explicit choice > browser language > English
//   persistence: one localStorage key ("app-language"), written by
//                setLanguage — the only path the switcher uses
//   normalization: regional codes (es-MX, fr-CA, zh-TW) resolve to the
//                exact supported code so the toggle never mis-displays

const KEY = "app-language";

const setNavigator = (language, languages) => {
  Object.defineProperty(window, "navigator", {
    value: {
      language,
      languages: languages || [language],
      userAgent: "jest",
    },
    configurable: true,
    writable: true,
  });
};

const loadI18nModule = () => {
  jest.resetModules();
  return require("./i18n");
};

describe("language detection precedence (first visit vs return visit)", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("uses the browser language on a first visit, normalized to a supported code", () => {
    setNavigator("es-MX", ["es-MX", "en-US"]);
    const { default: i18n } = loadI18nModule();
    expect(i18n.language).toBe("es");
  });

  it("normalizes zh-TW to zh-CN and fr-CA to fr", () => {
    setNavigator("zh-TW", ["zh-TW"]);
    expect(loadI18nModule().default.language).toBe("zh-CN");
    // The detector caches the first detection under the same key, so a
    // genuinely fresh visit starts from an empty store.
    window.localStorage.clear();
    setNavigator("fr-CA", ["fr-CA"]);
    expect(loadI18nModule().default.language).toBe("fr");
  });

  it("falls back to English for an unsupported browser language", () => {
    setNavigator("de-DE", ["de-DE"]);
    expect(loadI18nModule().default.language).toBe("en");
  });

  it("skips an unsupported first browser language for a supported second one", () => {
    setNavigator("de-DE", ["de-DE", "es-ES"]);
    expect(loadI18nModule().default.language).toBe("es");
  });

  it("a stored explicit choice beats the browser language on a return visit", () => {
    window.localStorage.setItem(KEY, "fr");
    setNavigator("es-ES", ["es-ES"]);
    expect(loadI18nModule().default.language).toBe("fr");
  });
});

describe("setLanguage — the pick sticks", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setNavigator("en-US", ["en-US"]);
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("switches immediately and persists under the one storage key", () => {
    const mod = loadI18nModule();
    mod.setLanguage("es");
    expect(mod.default.language).toBe("es");
    expect(window.localStorage.getItem(KEY)).toBe("es");
    // The orphaned key an earlier switcher wrote must stay untouched:
    // nothing reads it, and writing it again would resurrect the split.
    expect(window.localStorage.getItem("i18nextLng")).toBeNull();
  });

  it("the pick survives a fresh visit (module reload)", () => {
    const first = loadI18nModule();
    first.setLanguage("zh-CN");
    const second = loadI18nModule();
    expect(second.default.language).toBe("zh-CN");
  });

  it("picking a different language replaces the stored pick", () => {
    const mod = loadI18nModule();
    mod.setLanguage("es");
    mod.setLanguage("fr");
    expect(window.localStorage.getItem(KEY)).toBe("fr");
    expect(loadI18nModule().default.language).toBe("fr");
  });
});

describe("LanguageSwitcher reflects and drives the same mechanism", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setNavigator("en-US", ["en-US"]);
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("renders the active language as the selected option", () => {
    // Require everything AFTER loadI18nModule's resetModules so the
    // component, React and the renderer all come from one registry
    // generation (mixing generations breaks the hooks dispatcher).
    const mod = loadI18nModule();
    mod.setLanguage("fr");
    const React = require("react");
    const { renderToString } = require("react-dom/server");
    const LanguageSwitcher = require("../components/LanguageSwitcher").default;
    const html = renderToString(
      React.createElement(LanguageSwitcher, { compact: true }),
    );
    // React SSR marks the option matching the select's value as selected.
    expect(html).toMatch(/<option[^>]*selected[^>]*>Français<\/option>/);
    expect(html).toContain('data-testid="language-switcher"');
  });
});

describe("Hindi and Arabic", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("a Hindi or Arabic browser opens in that language", () => {
    setNavigator("hi-IN", ["hi-IN", "en-US"]);
    expect(loadI18nModule().default.language).toBe("hi");
    window.localStorage.clear();
    setNavigator("ar-SA", ["ar-SA"]);
    expect(loadI18nModule().default.language).toBe("ar");
  });

  it("Arabic turns the page right to left; switching back restores left to right", () => {
    setNavigator("en-US", ["en-US"]);
    const mod = loadI18nModule();
    mod.setLanguage("ar");
    expect(document.documentElement.dir).toBe("rtl");
    expect(document.documentElement.lang).toBe("ar");
    mod.setLanguage("hi");
    expect(document.documentElement.dir).toBe("ltr");
    expect(document.documentElement.lang).toBe("hi");
    mod.setLanguage("en");
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("money keeps Western digits in Arabic and uses the Indian grouping in Hindi", () => {
    setNavigator("en-US", ["en-US"]);
    const mod = loadI18nModule();
    mod.setLanguage("ar");
    const ar = (123456.78).toLocaleString(mod.getActiveLocale(), { style: "currency", currency: "USD" });
    expect(ar).toMatch(/123,456\.78/);
    mod.setLanguage("hi");
    const hi = (123456.78).toLocaleString(mod.getActiveLocale(), { style: "currency", currency: "USD" });
    expect(hi).toMatch(/1,23,456\.78/);
  });

  it("both appear in the switcher, named in their own language", () => {
    const mod = loadI18nModule();
    expect(mod.SUPPORTED_LANGUAGES.map((l) => l.code)).toEqual(["en", "es", "fr", "zh-CN", "hi", "ar"]);
    expect(mod.SUPPORTED_LANGUAGES.find((l) => l.code === "hi").label).toBe("हिन्दी");
    expect(mod.SUPPORTED_LANGUAGES.find((l) => l.code === "ar").label).toBe("العربية");
  });
});
