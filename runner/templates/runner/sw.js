{% load static %}/* ZayaRun service worker — instalável + offline básico. Servido em /sw.js (escopo raiz). */
const CACHE = 'zayarun-v1';
const CORE = [
  "{% static 'runner/css/style.css' %}",
  "{% static 'runner/img/icon-192.png' %}",
  "{% static 'runner/img/favicon.svg' %}"
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE).catch(() => {})));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Só GET same-origin — nunca toca em POST, OAuth da Strava ou sync.
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;

  // Navegação (HTML): rede primeiro (sempre fresco), cache como rede de segurança offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((r) => r || new Response(
          '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
          '<title>Offline</title><body style="font-family:Inter,system-ui;background:#0B0F1A;color:#E6EDF6;' +
          'display:grid;place-items:center;height:100vh;margin:0;text-align:center">' +
          '<div><h1 style="margin:.2em">Você está offline</h1>' +
          '<p style="color:#93A1B8">Reabra quando tiver conexão para sincronizar seus treinos.</p></div>',
          { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
        )))
    );
    return;
  }

  // Estáticos: cache primeiro, atualiza em segundo plano.
  if (req.url.includes('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => cached))
    );
  }
});
