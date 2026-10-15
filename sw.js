// ══ Service Worker — GARRABOT Elite PWA ════════════════════
const CACHE_NAME = 'garrabot-v3';
const ASSETS = [
  '/manifest.json',
  '/assets/icon-192.png',
  '/assets/icon-512.png'
];
// index.html NUNCA é cacheado — sempre busca versão mais recente do servidor
const NEVER_CACHE = ['/', '/index.html'];

// Instalação - cacheia apenas assets estáticos (sem index.html)
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

// Fetch — Network First para index.html, Cache First para assets estáticos
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Não intercepta WebSocket nem rotas de auth/login
  if (url.protocol === 'ws:' || url.protocol === 'wss:' ||
      url.pathname.startsWith('/login') ||
      url.pathname.startsWith('/auth')) return;

  // index.html: sempre busca do servidor (Network First) — garante código mais recente
  if (NEVER_CACHE.includes(url.pathname)) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // Assets estáticos: Cache First
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

// ── Keep-Alive via Web Lock API ────────────────────────────
// Mantém o SW ativo mesmo com tela apagada ou app minimizado
self.addEventListener('message', (e) => {
  if (e.data === 'KEEP_ALIVE') {
    // Responde para confirmar que o SW está vivo
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
      // Se já tem janela aberta, foca nela
      for (const client of clientList) {
        if (client.url.includes('garrabot') && 'focus' in client) {
          return client.focus();
        }
      }
      // Senão abre nova
      return clients.openWindow(e.notification.data?.url || '/');
    })
  );
});

// ── Background Sync: tenta reconectar bot após voltar online ──
self.addEventListener('sync', (e) => {
  if (e.tag === 'bot-reconnect') {
    e.waitUntil(
      self.clients.matchAll().then((clients) => {
        clients.forEach(client => client.postMessage('RECONNECT'));
      })
    );
  }
});
