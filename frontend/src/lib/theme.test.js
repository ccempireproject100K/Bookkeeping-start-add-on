import {
  CLIENT_THEME_KEY,
  getStoredClientTheme,
  storeClientTheme,
  nextClientTheme,
} from "./theme";

function fakeStorage(initial = {}) {
  const m = { ...initial };
  return {
    getItem: (k) => (k in m ? m[k] : null),
    setItem: (k, v) => { m[k] = String(v); },
    _map: m,
  };
}

describe("client page theme preference", () => {
  test("defaults to dark when nothing is stored", () => {
    expect(getStoredClientTheme(fakeStorage())).toBe("dark");
    expect(getStoredClientTheme(null)).toBe("dark");
    expect(getStoredClientTheme(undefined)).toBe("dark");
  });

  test("stored light choice wins; unknown values fall back to dark", () => {
    expect(getStoredClientTheme(fakeStorage({ [CLIENT_THEME_KEY]: "light" }))).toBe("light");
    expect(getStoredClientTheme(fakeStorage({ [CLIENT_THEME_KEY]: "dark" }))).toBe("dark");
    expect(getStoredClientTheme(fakeStorage({ [CLIENT_THEME_KEY]: "sepia" }))).toBe("dark");
  });

  test("toggle flips and the choice round-trips through storage", () => {
    const s = fakeStorage();
    expect(nextClientTheme("dark")).toBe("light");
    expect(nextClientTheme("light")).toBe("dark");
    storeClientTheme(s, nextClientTheme(getStoredClientTheme(s)));
    expect(s._map[CLIENT_THEME_KEY]).toBe("light");
    expect(getStoredClientTheme(s)).toBe("light");
    storeClientTheme(s, nextClientTheme(getStoredClientTheme(s)));
    expect(getStoredClientTheme(s)).toBe("dark");
  });

  test("broken storage never throws", () => {
    const broken = {
      getItem: () => { throw new Error("denied"); },
      setItem: () => { throw new Error("denied"); },
    };
    expect(getStoredClientTheme(broken)).toBe("dark");
    expect(() => storeClientTheme(broken, "light")).not.toThrow();
  });
});
