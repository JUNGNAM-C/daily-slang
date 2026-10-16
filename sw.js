// 앱 껍데기는 캐시해 두고, 단어 목록(words.json)은 항상 네트워크를 먼저 시도합니다.
// 앱 파일을 고쳤다면 아래 VERSION 숫자를 올려야 기존 사용자에게 반영됩니다.
const VERSION = 'v2';
const CACHE = 'shinjoeo-' + VERSION;
const SHELL = ['./', './index.html', './manifest.webmanifest', './icons/icon.svg', './icons/icon-192.png', './icons/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // 단어 목록: 네트워크 우선, 실패하면 마지막으로 저장된 것을 사용
  if (url.pathname.endsWith('/words.json')) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // 그 외: 캐시 우선
  e.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
});
