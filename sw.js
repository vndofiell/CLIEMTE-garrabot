// ══ Service Worker — GARRABOT Elite PWA ════════════════════
const CACHE_NAME = 'garrabot-v3';
const ASSETS = [
  '/assets/icon-192.png',
  '/assets/icon-512.png',
  '/manifest.json'
];

// Rotas de API que NUNCA devem ser cacheadas — sempre vão à rede
const API_PREFIXES = [
  '/open-login', '/clear-token', '/get-token', '/get-token-force',
  '/store-verifier', '/callback.html', '/auth/', '/login',
  '/pegar-token-robo', '/bot-config', '/tg-config', '/jc-',
  '/adaptive', '/contas/', '/quotex/', '/pocket/', '/set-modo',
  '/get-modo', '/avisos/', '/deriv/', '/ai/', '/groq', '/wa-',
  '/ping', '/debug', '/supremo/', '/ect/', '/garra-', '/bf5/',
  '/trading-pro/', '/mte/', '/dbs/', '/caixa-', '/jc-sec',
  '/login-sistema', '/auth/logout', '/sw.js'
];

function ehRotaAPI(url) {
  const path = new URL(url).pathname;
  return API_PREFIXES.some(p => path.startsWith(p));
}

// Instalação — cacheia apenas assets estáticos essenciais
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

// Ativação — limpa caches antigos (inclusive o garrabot-v2 que cacheou API)
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => {
        console.log('[SW] Removendo cache antigo:', k);
        return caches.delete(k);
      }))
    )
  );
  self.clients.claim();
});

// Fetch — Network First para TUDO; Cache First apenas para assets estáticos
self.addEventListener('fetch', (e) => {
  const url = e.request.url;

  // Ignora WebSocket
  if (url.startsWith('ws:') || url.startsWith('wss:')) return;

  // Rotas de API — sempre vai à rede, nunca cacheia
  if (ehRotaAPI(url)) {
    e.respondWith(fetch(e.request));
    return;
  }

  // Assets estáticos — Cache First com fallback à rede
  if (url.includes('/assets/') || url.endsWith('/manifest.json')) {
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
    return;
  }

  // Todo o resto (/, /index.html, etc.) — Network First, sem cache
  e.respondWith(fetch(e.request));
});

// ── Keep-Alive via Web Lock API ────────────────────────────
self.addEventListener('message', (e) => {
  if (e.data === 'KEEP_ALIVE') {
    e.source && e.source.postMessage('SW_ALIVE');
  }
  if (e.data === 'SKIP_WAITING') {
    self.skipWaiting();
  }
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
  e.waitUntil(
    clients.matchAll({ type: 'window' }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes('garrabot') && 'focus' in client) {
          return client.focus();
        }
      }
      return clients.openWindow(e.notification.data?.url || '/');
    })
  );
});

// ── Background Sync ────────────────────────────────────────
self.addEventListener('sync', (e) => {
  if (e.tag === 'bot-reconnect') {
    e.waitUntil(
      self.clients.matchAll().then((clients) => {
        clients.forEach(client => client.postMessage('RECONNECT'));
      })
    );
  }
});
