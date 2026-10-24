import { shouldOpenExternally } from "./nativeShell";

const ORIGIN = "https://localhost";

describe("shouldOpenExternally (native link routing)", () => {
  test("same-origin app links stay inside the app", () => {
    expect(shouldOpenExternally("/invoices", ORIGIN)).toBe(false);
    expect(shouldOpenExternally("https://localhost/dashboard", ORIGIN)).toBe(false);
    expect(shouldOpenExternally("#section", ORIGIN)).toBe(false);
  });

  test("other origins open in the system browser", () => {
    expect(shouldOpenExternally("https://example.com/help", ORIGIN)).toBe(true);
    expect(shouldOpenExternally("http://intuit.com", ORIGIN)).toBe(true);
    expect(shouldOpenExternally("//example.com/x", ORIGIN)).toBe(true);
  });

  test("mailto / tel / sms hand off to the OS", () => {
    expect(shouldOpenExternally("mailto:support@example.com", ORIGIN)).toBe(true);
    expect(shouldOpenExternally("tel:+12125551234", ORIGIN)).toBe(true);
    expect(shouldOpenExternally("sms:+12125551234", ORIGIN)).toBe(true);
  });

  test("empty and non-web hrefs do not hijack navigation", () => {
    expect(shouldOpenExternally("", ORIGIN)).toBe(false);
    expect(shouldOpenExternally(null, ORIGIN)).toBe(false);
    expect(shouldOpenExternally("javascript:void(0)", ORIGIN)).toBe(false);
  });
});
