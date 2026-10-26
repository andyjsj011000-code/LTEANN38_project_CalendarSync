/* Notification handling only. Authenticated pages and API responses are never cached. */
'use strict';
self.addEventListener('install', event => {self.skipWaiting();});
self.addEventListener('activate', event => {event.waitUntil(self.clients.claim());});
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let data={}; try {data=event.data ? event.data.json() : {};} catch {}
    // Keep lock-screen notifications free of school titles and private text.
    await self.registration.showNotification('CalendarSync', {
      body: typeof data.body === 'string' ? data.body.slice(0,160) : '확인할 일정이 있어요.',
      tag: typeof data.tag === 'string' ? data.tag.slice(0,80) : 'calendarsync',
      icon: '/assets/icon-192.png', badge: '/assets/icon-192.png', data:{url:'/'}
    });
  })());
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const own=windows.find(client => new URL(client.url).origin===self.location.origin);
    if (own) {await own.navigate('/'); return own.focus();}
    return self.clients.openWindow('/');
  })());
});
self.addEventListener('message', event => {
  if (event.data?.type==='clear-notifications') event.waitUntil(self.registration.getNotifications().then(items=>items.forEach(item=>item.close())));
});
