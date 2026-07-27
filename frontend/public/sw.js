// Service worker — shell caching ONLY.
//
// Deliberately never caches /api. A cached turn would show the player a stale
// scene that the engine has already moved past, which is worse than an honest
// error: the whole game is about state you can't see, so silently serving old
// state is the one failure mode that would be genuinely confusing.
//
// Its job is narrow: make the app installable and let it open offline. Playing
// offline still needs the engine reachable (ollama on your LAN).

const CACHE = 'rp-shell-v1'
const SHELL = ['/', '/index.html', '/manifest.webmanifest', '/icon.svg']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // addAll is atomic — one missing asset fails the whole install, so we
      // tolerate individual misses rather than never installing at all.
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting())
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  const url = new URL(request.url)

  // Never intercept the API or anything non-GET.
  if (request.method !== 'GET' || url.pathname.startsWith('/api')) return

  // Navigations: network first so a deploy is picked up immediately, falling
  // back to the cached shell when offline.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/index.html'))
    )
    return
  }

  // Static assets: cache first, they are content-hashed by vite.
  event.respondWith(
    caches.match(request).then((hit) =>
      hit || fetch(request).then((response) => {
        if (response.ok && url.origin === self.location.origin) {
          const copy = response.clone()
          caches.open(CACHE).then((cache) => cache.put(request, copy))
        }
        return response
      })
    )
  )
})
