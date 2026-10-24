// Native shell glue for the Capacitor wrap (Android / iOS apps).
//
// The mobile apps are this same web app running inside a Capacitor
// webview: the bundle ships inside the app and talks to the production
// API (REACT_APP_BACKEND_URL is baked at build time, exactly like the
// web build — see scripts/check-build-env.js). This module is the only
// native-aware code in the web bundle:
//
//   * hardware/gesture back button walks the SPA history, exiting at
//     the root instead of killing the app mid-flow;
//   * links that leave the app (other origins, mailto:/tel:/sms:) open
//     in the system browser / handler instead of navigating the webview
//     away from the app;
//   * everything here is a no-op in a regular browser, so the web app
//     and the PWA behave exactly as before.
//
// Safe-area insets are handled in CSS (src/index.css, #root padding with
// env(safe-area-inset-*)) together with viewport-fit=cover in
// public/index.html.

import { Capacitor } from "@capacitor/core";
import { App as CapacitorApp } from "@capacitor/app";
import { Browser } from "@capacitor/browser";

export function isNativePlatform() {
  return Capacitor.isNativePlatform();
}

// Pure helper (unit-tested): should this anchor href leave the app and
// open externally when running natively?
export function shouldOpenExternally(href, currentOrigin) {
  if (!href) return false;
  let url;
  try {
    url = new URL(href, currentOrigin);
  } catch {
    return false;
  }
  if (url.protocol === "mailto:" || url.protocol === "tel:" || url.protocol === "sms:") {
    return true;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  return url.origin !== currentOrigin;
}

let initialised = false;

export function initNativeShell() {
  if (initialised || !isNativePlatform()) return;
  initialised = true;

  // Back button: go back through the SPA history; at the root, exit.
  CapacitorApp.addListener("backButton", ({ canGoBack }) => {
    if (canGoBack || window.history.length > 1) {
      window.history.back();
    } else {
      CapacitorApp.exitApp();
    }
  });

  // External links: system browser (http/https) or the OS handler
  // (mailto:/tel:/sms:), never an in-webview navigation away from the app.
  document.addEventListener("click", (event) => {
    const anchor = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!anchor) return;
    const href = anchor.getAttribute("href");
    if (!shouldOpenExternally(href, window.location.origin)) return;
    event.preventDefault();
    const absolute = new URL(href, window.location.origin).toString();
    if (/^(mailto:|tel:|sms:)/.test(absolute)) {
      window.location.href = absolute;
    } else {
      Browser.open({ url: absolute });
    }
  });
}
