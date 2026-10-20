// Service worker registration (production builds only).
//
// The worker itself lives in public/service-worker.js: it caches the static
// app shell and is network-first for /api — see that file for the strategy.
// Registration is best-effort: a failure here must never break the app, so
// errors are logged, not thrown.

export function register() {
  if (process.env.NODE_ENV !== "production") return;
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    const swUrl = `${process.env.PUBLIC_URL || ""}/service-worker.js`;
    navigator.serviceWorker.register(swUrl).catch((error) => {
      console.warn("Service worker registration failed:", error);
    });
  });
}

export function unregister() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.ready
      .then((registration) => registration.unregister())
      .catch(() => {});
  }
}
