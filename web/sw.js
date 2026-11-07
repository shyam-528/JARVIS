// JARVIS MARK 3 - Service Worker
// Phase 3: NEVER cache /api/* or /ws (was: stale sensitive data in CacheStorage).
const CACHE_NAME = 'jarvis-v2.2';
const STATIC_ASSETS = [
  '/app',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Network-first strategy for API calls, cache-first for static assets
  const url = new URL(event.request.url);
  
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws')) {
    // Authenticated + live data: network only, never store.
    event.respondWith(fetch(event.request));
    return;
  }
  
  // Cache-first for static assets
  event.respondWith(
    caches.match(event.request).then((response) => {
      return response || fetch(event.request).then((response) => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return response;
      });
    })
  );
});