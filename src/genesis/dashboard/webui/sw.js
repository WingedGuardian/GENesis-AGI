// Genesis PWA Service Worker — minimal shell cache
const CACHE = 'genesis-shell-v2';
const SHELL = ['/genesis', '/vendor/alpine/alpine.min.js', '/js/initFw.js', '/js/api.js', '/js/dashboard.js', '/index.css', '/css/dashboard.css'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
