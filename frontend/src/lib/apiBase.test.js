import { resolveApiBase, backendUrlProblem } from "./apiBase";

// Regression coverage for the production incident where the bundle was
// built without REACT_APP_BACKEND_URL and baked "undefined/api" as its
// API base, breaking every page that talks to the backend.

describe("resolveApiBase", () => {
  test("uses REACT_APP_BACKEND_URL when set", () => {
    expect(resolveApiBase({ REACT_APP_BACKEND_URL: "https://api.example.com" }))
      .toBe("https://api.example.com/api");
  });

  test("trims a trailing slash on the backend URL", () => {
    expect(resolveApiBase({ REACT_APP_BACKEND_URL: "https://api.example.com/" }))
      .toBe("https://api.example.com/api");
  });

  test("never produces the string 'undefined' when the variable is missing", () => {
    const base = resolveApiBase({});
    expect(base).toBe("/api");
    expect(base).not.toContain("undefined");
  });

  test("never produces the string 'undefined' when the variable is empty", () => {
    expect(resolveApiBase({ REACT_APP_BACKEND_URL: "" })).toBe("/api");
    expect(resolveApiBase({ REACT_APP_BACKEND_URL: "   " })).toBe("/api");
  });
});

describe("backendUrlProblem", () => {
  test("accepts a valid https URL", () => {
    expect(backendUrlProblem({ REACT_APP_BACKEND_URL: "https://bookkeeping-api-ymh6.onrender.com" })).toBeNull();
  });

  test("flags a missing variable", () => {
    expect(backendUrlProblem({})).toMatch(/not set/);
  });

  test("flags the literal string 'undefined'", () => {
    expect(backendUrlProblem({ REACT_APP_BACKEND_URL: "undefined" })).toMatch(/literal string/);
  });

  test("flags a non-URL value", () => {
    expect(backendUrlProblem({ REACT_APP_BACKEND_URL: "not a url" })).toMatch(/not a valid URL/);
  });

  test("flags a non-http(s) scheme", () => {
    expect(backendUrlProblem({ REACT_APP_BACKEND_URL: "ftp://example.com" })).toMatch(/http/);
  });
});
