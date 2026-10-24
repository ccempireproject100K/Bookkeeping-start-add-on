// Smoke coverage for the Capacitor wrap (mobile apps). These tests read
// the committed scaffold from disk so a future edit that breaks the wrap
// (wrong webDir, dropped native project, missing package) fails here
// instead of at store-build time.

const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const config = JSON.parse(fs.readFileSync(path.join(root, "capacitor.config.json"), "utf8"));
const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));

describe("capacitor config (mobile apps smoke)", () => {
  test("app id is a valid reverse-domain id carrying the brand", () => {
    expect(config.appId).toMatch(/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/);
    expect(config.appId).toContain("mindatease");
  });

  test("app name carries the brand short name", () => {
    expect(config.appName).toMatch(/Mind at Ease/);
  });

  test("webDir points at the CRA build output", () => {
    expect(config.webDir).toBe("build");
  });

  test("capacitor packages are declared", () => {
    const deps = { ...(pkg.dependencies || {}), ...(pkg.devDependencies || {}) };
    expect(deps["@capacitor/core"]).toBeTruthy();
    expect(deps["@capacitor/cli"]).toBeTruthy();
    expect(deps["@capacitor/android"]).toBeTruthy();
    expect(deps["@capacitor/ios"]).toBeTruthy();
    expect(deps["@capacitor/app"]).toBeTruthy();
    expect(deps["@capacitor/browser"]).toBeTruthy();
  });

  test("native projects are scaffolded", () => {
    expect(fs.existsSync(path.join(root, "android", "app", "build.gradle"))).toBe(true);
    expect(fs.existsSync(path.join(root, "android", "app", "src", "main", "AndroidManifest.xml"))).toBe(true);
    expect(fs.existsSync(path.join(root, "ios", "App", "App.xcodeproj", "project.pbxproj"))).toBe(true);
    expect(fs.existsSync(path.join(root, "ios", "App", "App", "Info.plist"))).toBe(true);
  });
});
