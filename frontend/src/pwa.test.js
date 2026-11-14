/**
 * PWA shell validation (feat/pwa-mobile).
 *
 * These tests pin the installability contract of the Books web app:
 * a complete manifest with real PNG icons at the declared sizes, the
 * app-shell service worker with its offline fallback, the manifest link
 * in index.html, registration in the app entry, and the Digital Asset
 * Links file the Android (TWA) build depends on.
 */
const fs = require("fs");
const path = require("path");

const PUBLIC_DIR = path.join(__dirname, "..", "public");
const readPublic = (rel) => fs.readFileSync(path.join(PUBLIC_DIR, rel), "utf8");

function pngSize(file) {
  const buf = fs.readFileSync(file);
  // PNG signature (8 bytes) + IHDR: width at offset 16, height at 20.
  expect(buf.subarray(0, 4).toString("hex")).toBe("89504e47");
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

describe("web app manifest", () => {
  const manifest = JSON.parse(readPublic("manifest.json"));

  test("carries the identity and display fields installers require", () => {
    expect(manifest.name).toContain("Mind at Ease");
    expect(manifest.short_name).toBe("Mind at Ease");
    expect(manifest.start_url).toBeTruthy();
    expect(manifest.scope).toBeTruthy();
    expect(manifest.display).toBe("standalone");
    expect(manifest.theme_color).toBe("#0B0D10");
    expect(manifest.background_color).toBe("#0B0D10");
  });

  test("declares PNG icons at 192 and 512 plus a 512 maskable", () => {
    const pngs = manifest.icons.filter((i) => i.type === "image/png");
    expect(pngs.some((i) => i.sizes === "192x192" && i.purpose === "any")).toBe(true);
    expect(pngs.some((i) => i.sizes === "512x512" && i.purpose === "any")).toBe(true);
    expect(pngs.some((i) => i.sizes === "512x512" && i.purpose === "maskable")).toBe(true);
  });

  test("every declared icon exists on disk at its declared size", () => {
    for (const icon of manifest.icons) {
      const file = path.join(PUBLIC_DIR, icon.src);
      expect(fs.existsSync(file)).toBe(true);
      if (icon.type === "image/png") {
        const [w, h] = icon.sizes.split("x").map(Number);
        expect(pngSize(file)).toEqual({ width: w, height: h });
      }
    }
    // iOS home-screen icon referenced from index.html.
    expect(
      pngSize(path.join(PUBLIC_DIR, "icons", "apple-touch-icon.png")),
    ).toEqual({ width: 180, height: 180 });
  });
});

describe("service worker and offline fallback", () => {
  const sw = readPublic("service-worker.js");

  test("caches the app shell and never caches API traffic", () => {
    expect(sw).toContain("CACHE_VERSION");
    expect(sw).toContain("/index.html");
    expect(sw).toContain("/manifest.json");
    expect(sw).toContain('"/api/"');
  });

  test("falls back to the offline page", () => {
    expect(sw).toContain("/offline.html");
    const offline = readPublic("offline.html");
    expect(offline.toLowerCase()).toContain("offline");
    // Honest posture (feat/offline-field-capture): field capture may be
    // saved on the phone, but only as a listed, visible queue — the page
    // must say items are NOT on the books until the panel says they sent.
    expect(offline).toContain("listed in the sync panel");
    expect(offline).toContain("not on your books");
  });
});

describe("entry points wire the PWA up", () => {
  test("index.html links the manifest with the brand theme color", () => {
    const html = readPublic("index.html");
    expect(html).toContain('rel="manifest"');
    expect(html).toContain('content="#0B0D10"');
    expect(html).toContain("apple-touch-icon");
  });

  test("the app entry registers the service worker", () => {
    const entry = fs.readFileSync(path.join(__dirname, "index.js"), "utf8");
    expect(entry).toContain("serviceWorkerRegistration.register()");
  });
});

describe("digital asset links (Android TWA)", () => {
  test("assetlinks.json targets the Books package with a real fingerprint", () => {
    const links = JSON.parse(readPublic(".well-known/assetlinks.json"));
    const entry = links.find((l) =>
      (l.relation || []).includes("delegate_permission/common.handle_all_urls"),
    );
    expect(entry).toBeTruthy();
    expect(entry.target.package_name).toBe("com.mindatease.bookkeeping");
    const prints = entry.target.sha256_cert_fingerprints;
    expect(Array.isArray(prints) && prints.length).toBeTruthy();
    for (const fp of prints) {
      expect(fp).toMatch(/^([0-9A-F]{2}:){31}[0-9A-F]{2}$/);
    }
  });
});
