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
  e.waitUntil((async () => {
    // מונה אדום על אייקון האפליקציה (iOS 16.4+ ב-PWA מותקן)
    if (typeof d.badge === 'number' && d.badge > 0 && self.navigator && self.navigator.setAppBadge) {
      try { self.navigator.setAppBadge(d.badge); } catch (err) {}
    }
    // דדופ: אם יש חלון אפליקציה גלוי — הערוץ הפנימי כבר התריע (צליל+טוסט), לא מציגים push כפול
    const cl = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (cl.some((c) => c.visibilityState === 'visible' || c.focused === true)) return;
    // tag: הזמנה → פר-מספר-הזמנה; וואטסאפ → פר-שיחה (התראות שונות לא דורסות זו את זו)
    const tag = (d.kind === 'order')
      ? ('gm-' + (d.tag || 'order'))
      : ('gm-wa-' + (d.phone || 'all'));
    return self.registration.showNotification(d.title || 'GreenOS 💬', {
      body: d.body || '',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      dir: 'rtl',
      lang: 'he',
      tag: tag,
      renotify: true,
      data: { url: d.url || '/?wa=1', phone: d.phone || '', kind: d.kind || 'wa' },
    });
  })());
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  const url = data.url || '/?wa=1';
  e.waitUntil((async () => {
    await idbSetPending({ phone: data.phone || '', url: url, at: Date.now() });
    const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const msgType = (data.kind === 'order') ? 'open-orders' : 'open-wa';
    for (const c of list) {
      if ('focus' in c) {
        // אפליקציה פתוחה: פוקוס + הודעה פנימית → נפתח היעד בלי reload (שיחה / טאב הזמנות)
        c.focus();
        try { c.postMessage({ type: msgType, phone: data.phone || '', url: url }); } catch (err) {}
        return;
      }
    }
    return self.clients.openWindow(url);
  })());
});
