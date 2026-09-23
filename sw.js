const CACHE='aifx-hybrid-static-v4';
const STATIC=['/manifest.json','/icon-192.png','/icon-512.png'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(STATIC)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(u.origin!==location.origin || e.request.method!=='GET') return;
  // Never cache navigation, health, or API responses. These are dynamic and a
  // cached Render "Application loading" page can otherwise become permanent.
  if(u.pathname==='/' || u.pathname==='/index.html' || u.pathname==='/health' || u.pathname.startsWith('/api/')) return;
  if(STATIC.includes(u.pathname)){
    e.respondWith(caches.match(e.request).then(cached=>cached||fetch(e.request).then(r=>{if(r.ok){const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));}return r})));
  }
});
