/* Mind at Ease Solutions — Books service worker.
 *
 * Deliberately conservative:
 *  - API requests (any URL whose path contains /api/) are NETWORK-FIRST and
 *    never cached — books data must be fresh or fail honestly, never stale.
 *  - The static app shell (HTML/JS/CSS/icons) is cache-first so the app
 *    installs and opens fast; navigations fall back to the cached shell
 *    when the network is gone, and to /offline.html if the shell itself
 *    was never cached.
 *  - Non-GET requests and cross-origin non-API requests pass through.
 *  - Writes are never queued BY THE WORKER. Field capture (time, receipt
 *    photos, expense notes) queues in the app itself — visibly, in the
 *    sync panel, item by item (feat/offline-field-capture). Everything
 *    else, money movement included, fails honestly offline with the 503
 *    below, exactly like it would without a service worker.
 *
 * Restored from the PWA that shipped with the October Capacitor branch
 * (feature/capacitor-mobile-apps) and extended with the offline fallback
 * page. Bump CACHE_VERSION whenever the shell strategy changes; old
 * caches are deleted on activate.
 */
const CACHE_VERSION = "mae-books-v3";
const APP_SHELL = [
  "/",
  "/index.html",
  "/offline.html",
  "/manifest.json",
  "/icons/icon.svg",
  "/icons/maskable.svg",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_VERSION)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

function isApiRequest(url) {
  return url.pathname === "/api" || url.pathname.includes("/api/");
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  if (isApiRequest(url)) {
    // Network-first for API traffic, on any origin (the API may live on a
    // different host than the web shell). Responses are never written to a
    // cache; a network failure surfaces as a plain 503 to the app.
    event.respondWith(
      fetch(request).catch(
        () =>
          new Response(
            JSON.stringify({ detail: "Offline — the API is unreachable." }),
            { status: 503, headers: { "Content-Type": "application/json" } }
          )
      )
    );
    return;
  }

  if (url.origin !== self.location.origin) return; // fonts/CDN: network's job

  if (request.mode === "navigate") {
    // Fresh shell when online; cached shell when not; the offline page if
    // the shell was never cached (first visit with no connection). A
    // non-OK navigation response (e.g. a static host without an SPA
    // rewrite answering 404 for a client-side route) is treated as a
    // failure and falls back the same way.
    event.respondWith(
      fetch(request)
        .then((res) => {
          if (!res.ok) throw new Error(`Navigation failed: ${res.status}`);
          return res;
        })
        .catch(() =>
          caches
            .match("/index.html")
            .then((hit) => hit || caches.match("/offline.html"))
        )
    );
    return;
  }

  // Same-origin static assets: cache-first, then populate on miss.
  event.respondWith(
    caches.match(request).then(
      (hit) =>
        hit ||
        fetch(request).then((res) => {
          if (res && res.status === 200 && res.type === "basic") {
            const copy = res.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
          }
          return res;
        })
    )
  );
});
