/* =============================================================================
   Service Worker - MyPTAI PWA
   - PRECACHE degli asset statici (cache-first)
   - Tutte le API personali: solo rete, senza cache né fallback
   Requisito: HTTPS o localhost (secure context) per la registrazione.
   ============================================================================= */
const CACHE = "garmin-coach-v101";
const PRECACHE = [
  "/",
  "/index.html",
  "/manifest.json",
  "/css/style.css?v=95",
  "/js/app.js?v=101",
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
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("garmin-coach-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(async () => {
        const cache = await caches.open(CACHE);
        const keys = await cache.keys();
        await Promise.all(keys.filter((req) => new URL(req.url).pathname.startsWith("/api/"))
          .map((req) => cache.delete(req)));
      })
      .then(() => self.clients.claim())
      .then(() => self.clients.matchAll({ type: "window" }))
      .then((clients) => clients.forEach((c) => c.postMessage({ type: "MYPTAI_NEW_VERSION" })))
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || req.headers.has("Authorization")) {
    event.respondWith(fetch(req, { cache: "no-store" }));
    return;
  }
  const staticPaths = PRECACHE.map((path) => new URL(path, self.location.origin).pathname);
  if (req.method !== "GET" || !staticPaths.includes(url.pathname)) return;
  // Statici: cache-first
  event.respondWith(
    caches.open(CACHE).then((cache) => cache.match(req)).then(
      (hit) =>
        hit ||
        fetch(req).then((res) => {
          const copy = res.clone();
          if (res.ok && res.type === "basic") caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
    )
  );
});
