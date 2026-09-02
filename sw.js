const CACHE='aifx-v2';
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/index.html','/manifest.webmanifest']))));
self.addEventListener('fetch',e=>{if(e.request.url.includes('/api/'))return;e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)))});
