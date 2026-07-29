// ══ Service Worker — GARRABOT Elite PWA ════════════════════
const CACHE_NAME = 'garrabot-v1';
const ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/assets/icon-192.png',
  '/assets/icon-512.png'
];

// Instalação - cacheia assets
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

// Ativação - limpa caches antigos
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch - Network First para API, Cache First para assets
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Não intercepta WebSocket nem rotas de auth/login
  if (url.protocol === 'ws:' || url.protocol === 'wss:' ||
      url.pathname.startsWith('/login') ||
      url.pathname.startsWith('/auth')) return;

  e.respondWith(
    caches.match(e.request).then((cached) => {
      return cached || fetch(e.request).then((response) => {
        if (response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
        }
        return response;
      });
    })
  );
});

// Notificações Push
self.addEventListener('push', (e) => {
  const data = e.data ? e.data.json() : {};
  const options = {
    body: data.body || 'Nova notificação',
    icon: '/assets/icon-192.png',
    badge: '/assets/icon-192.png',
    vibrate: [200, 100, 200],
    data: { url: data.url || '/' }
  };
  e.waitUntil(self.registration.showNotification(data.title || '🤖 GARRABOT', options));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data?.url || '/'));
});
