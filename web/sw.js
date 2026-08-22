// Minimal service worker: only exists so the page is installable as a PWA
// and the app shell still loads (from cache) if the network briefly drops.
// It deliberately does NOT try to cache the WebSocket stream or make the
// live console/sensor data work offline -- that needs a real connection to
// the iLO2 host on the local network no matter what.
//
// Network-first, not cache-first: this page is under active development,
// so always prefer a fresh copy when the network is up, and only fall back
// to the cache if the fetch actually fails. A cache-first strategy here
// would silently keep serving whatever HTML/JS got cached on first visit
// forever, no matter how many times index.html changes on the server --
// exactly the bug this replaces (a UI fix that tested correctly in
// isolation kept "not applying" because the browser never re-fetched it).
const CACHE = "ilo2-shell-v2";
const SHELL = ["./", "./index.html", "./manifest.json",
               "./icons/icon-192.png", "./icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== location.origin) return;
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
