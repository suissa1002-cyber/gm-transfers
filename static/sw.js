/* GreenOS service worker — Web Push לוואטסאפ (PWA) */
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

/* iOS מתעלם לעיתים מה-URL ב-openWindow/navigate — לכן היעד נשמר גם ב-IndexedDB
   והעמוד קורא אותו בעלייה/בחזרה לפוקוס (gm-nav/kv/pending). */
function idbSetPending(val) {
  return new Promise((res) => {
    try {
      const r = indexedDB.open('gm-nav', 1);
      r.onupgradeneeded = () => r.result.createObjectStore('kv');
      r.onsuccess = () => {
        const tx = r.result.transaction('kv', 'readwrite');
        tx.objectStore('kv').put(val, 'pending');
        tx.oncomplete = () => res();
        tx.onerror = () => res();
      };
      r.onerror = () => res();
    } catch (err) { res(); }
  });
}

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: 'GreenOS', body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || 'GreenOS 💬', {
    body: d.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    dir: 'rtl',
    lang: 'he',
    tag: 'gm-wa-' + (d.phone || 'all'),   // tag פר-שיחה — התראות משיחות שונות לא דורסות זו את זו
    renotify: true,
    data: { url: d.url || '/?wa=1', phone: d.phone || '' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  const url = data.url || '/?wa=1';
  e.waitUntil((async () => {
    await idbSetPending({ phone: data.phone || '', url: url, at: Date.now() });
    const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of list) {
      if ('focus' in c) {
        // אפליקציה פתוחה: פוקוס + הודעה פנימית → נפתחת השיחה בלי reload (כמו וואטסאפ)
        c.focus();
        try { c.postMessage({ type: 'open-wa', phone: data.phone || '', url: url }); } catch (err) {}
        return;
      }
    }
    return self.clients.openWindow(url);
  })());
});
