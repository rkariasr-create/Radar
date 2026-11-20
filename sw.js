// Radar: abre al instante y funciona sin conexión con la última versión guardada.
const CACHE = "radar-v2";
const SHELL = ["./", "./index.html", "./config.json", "./manifest.webmanifest", "./icon.svg", "./fonts/newsreader.woff2", "./fonts/instrument-sans-400.woff2", "./fonts/instrument-sans-500.woff2", "./fonts/instrument-sans-600.woff2"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
// Primero la red (para tener siempre lo último); si no hay conexión, la copia guardada.
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
      return r;
    }).catch(() => caches.match(e.request, {ignoreSearch: true}))
  );
});
