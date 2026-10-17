// The axios client must never end up with "undefined" in its baseURL,
// whatever the build environment looked like. (Regression test for the
// incident where the production bundle baked baseURL "undefined/api".)

describe("api client baseURL", () => {
  const ORIGINAL = process.env.REACT_APP_BACKEND_URL;

  afterEach(() => {
    if (ORIGINAL === undefined) delete process.env.REACT_APP_BACKEND_URL;
    else process.env.REACT_APP_BACKEND_URL = ORIGINAL;
    jest.resetModules();
  });

  test("falls back to a relative /api — never 'undefined/api' — when unset", () => {
    delete process.env.REACT_APP_BACKEND_URL;
    jest.resetModules();
    const client = require("./api").default;
    expect(client.defaults.baseURL).toBe("/api");
    expect(client.defaults.baseURL).not.toContain("undefined");
  });

  test("uses REACT_APP_BACKEND_URL when set at build time", () => {
    process.env.REACT_APP_BACKEND_URL = "https://bookkeeping-api-ymh6.onrender.com";
    jest.resetModules();
    const client = require("./api").default;
    expect(client.defaults.baseURL).toBe("https://bookkeeping-api-ymh6.onrender.com/api");
  });
});
