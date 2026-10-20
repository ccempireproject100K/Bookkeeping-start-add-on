/* Mind at Ease Solutions — Books service worker.
 *
 * Deliberately conservative:
 *  - API requests (any URL whose path contains /api/) are NETWORK-FIRST and
 *    never cached — books data must be fresh or fail honestly, never stale.
 *  - The static app shell (HTML/JS/CSS/icons) is cache-first so the app
 *    installs and opens fast; navigations fall back to the cached shell
 *    when the network is gone.
 *  - Non-GET requests and cross-origin non-API requests pass through.
 *
 * Bump CACHE_VERSION whenever the shell strategy changes; old caches are
 * deleted on activate.
 */
const CACHE_VERSION = "mae-books-v1";
const APP_SHELL = [
  "/",
  "/index.html",
  "/manifest.json",
  "/icons/icon.svg",
  "/icons/maskable.svg",
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
    // Fresh shell when online, cached shell when not.
    event.respondWith(fetch(request).catch(() => caches.match("/index.html")));
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
