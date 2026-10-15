// Lets SimpleDesk install as an app, and shows a friendly page when offline.
//
// Only the design files (styles, script, icons) are kept on the device.
// Pages with tickets or client details are never stored, so nothing private
// stays on a phone after someone signs out.
const CACHE = "simpledesk-v4";
const KEEP = [
  "/static/style.css",
  "/static/app.js",
  "/static/live.js",
  "/static/favicon.svg",
  "/static/offline.html",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(KEEP)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;

  // Pages: always ask the server. If there is no connection, show the offline page.
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/static/offline.html")));
    return;
  }

  // Design files: use the fresh copy when online, the saved copy when not.
  if (KEEP.includes(new URL(req.url).pathname)) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
  }
});
