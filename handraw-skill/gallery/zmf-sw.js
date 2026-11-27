const CACHE_NAME = 'zmf-pwa-shell-v2';
const SHELL = [
  './zmf.html',
  './zmf-manifest.json',
  './zmf-icon-180.png',
  './zmf-icon-192.png',
  './zmf-icon-512.png',
  './zmf-icon-maskable-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (/^\/(api|img|tujian|ref|gen|recent)(\/|$)/.test(url.pathname)) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      const shellUrl = new URL('./zmf.html', self.registration.scope).href;
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(shellUrl, response.clone());
        }
        return response;
      } catch {
        return await caches.match(shellUrl) || Response.error();
      }
    })());
    return;
  }

  const shellUrls = new Set(SHELL.map(path => new URL(path, self.registration.scope).href));
  if (!shellUrls.has(request.url)) return;
  event.respondWith((async () => {
    const cached = await caches.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(request, response.clone());
    }
    return response;
  })());
});
