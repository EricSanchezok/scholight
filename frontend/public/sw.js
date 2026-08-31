const OFFLINE_DOCUMENT = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Scholight is offline</title><style>body{margin:0;padding:48px 24px;background:#fbfaf5;color:#0e0f14;font:16px/1.6 system-ui,sans-serif}main{max-width:560px;margin:12vh auto}h1{font:600 32px/1.2 Georgia,serif}p{color:#61636e}</style></head><body><main><p>scholight</p><h1>Reconnect to continue your research.</h1><p>Scholight does not store research data for offline use. Check your connection and try again.</p></main></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(
      () =>
        new Response(OFFLINE_DOCUMENT, {
          headers: { "Content-Type": "text/html; charset=utf-8" },
          status: 503,
        }),
    ),
  );
});
