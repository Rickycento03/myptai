/* =============================================================================
   Service Worker - MyPTAI PWA
   - PRECACHE degli asset statici (cache-first)
   - API /api/*: network-first con fallback su cache
   Requisito: HTTPS o localhost (secure context) per la registrazione.
   ============================================================================= */
const CACHE = "garmin-coach-v92";
const PRECACHE = [
  "/",
  "/index.html",
  "/manifest.json",
  "/css/style.css?v=92",
  "/js/app.js?v=92",
  "/img/logo.svg?v=92",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
      .then(() => self.clients.matchAll({ type: "window" }))
      .then((clients) => clients.forEach((c) => c.postMessage({ type: "MYPTAI_NEW_VERSION" })))
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || req.url.includes("/api/")) {
    // API: network-first con fallback su cache (per usabilità offline parziale)
    if (req.method === "GET") {
      event.respondWith(
        fetch(req)
          .then((res) => {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
            return res;
          })
          .catch(() => caches.match(req))
      );
    }
    return;
  }
  // Statici: cache-first
  event.respondWith(
    caches.match(req).then(
      (hit) =>
        hit ||
        fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
    )
  );
});
