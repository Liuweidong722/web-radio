/* 网络收音机 · Service Worker
 * 只缓存应用外壳（离线也能打开界面），音频流一律直连网络、绝不缓存。 */
const CACHE = "webradio-shell-v4";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./lib/hls.min.js",
  "./icon-192.png",
  "./icon-512.png"
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  // 跨域请求（音频流、代理、RadioBrowser 等）直接放行，不进缓存
  if (url.origin !== self.location.origin) return;
  // 录音相关接口不缓存
  if (url.pathname.indexOf("/api/") === 0 || url.pathname.indexOf("/rec/") === 0) return;

  e.respondWith(
    caches.match(req).then((hit) => {
      if (hit) {
        // 后台顺带更新一次，保证下次打开是新版
        fetch(req).then((res) => {
          if (res && res.ok) caches.open(CACHE).then((c) => c.put(req, res.clone()));
        }).catch(() => {});
        return hit;
      }
      return fetch(req).then((res) => {
        if (res && res.ok && res.type === "basic") {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => caches.match("./index.html"));
    })
  );
});
