/* Deskoros service worker: makes the app installable and quick to open.
   Only the app shell (this page, icons, the React/KaTeX scripts) is cached. Anything from /api - logins, exams,
   marks, uploads - always goes to the server and is never stored, so nobody sees stale or someone else's data. */
const VERSION = "deskoros-v1";
const SHELL = ["/", "/manifest.webmanifest", "/logo.svg", "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png", "/icons/favicon-64.png"];
const CDN = ["https://cdnjs.cloudflare.com/"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET") return;                                     // uploads and changes: straight to the server
  const same = url.origin === self.location.origin;
  if (same && (url.pathname.startsWith("/api/") || url.pathname === "/health")) return;    // live data: never cached
  if (same && url.pathname === "/demo") return;                          // the demo video page: not the app shell

  if (req.mode === "navigate") {                                          // the page: fresh when online, cached when offline
    e.respondWith(fetch(req).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(VERSION).then(c => c.put("/", copy)); }
      return r;
    }).catch(() => caches.match("/")));
    return;
  }
  if ((same && (url.pathname.startsWith("/icons/") || url.pathname === "/manifest.webmanifest" || url.pathname === "/logo.svg")) ||
      CDN.some(p => req.url.startsWith(p))) {                             // icons and versioned library files
    e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(r => {
      if (r.ok || r.type === "opaque") { const copy = r.clone(); caches.open(VERSION).then(c => c.put(req, copy)); }
      return r;
    })));
  }
});
