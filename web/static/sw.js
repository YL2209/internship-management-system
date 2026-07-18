/* ===========================================================
 * Service Worker - 实习数据管理系统 PWA
 * 缓存策略:
 *   - HTML 页面:  Network First（优先网络），离线时回落缓存
 *   - 静态资源:   Cache First（优先缓存），带后台重新校验
 *   - API 请求:   Network Only（不缓存）
 *   - 离线页:     Precache /offline.html
 * ===========================================================
 */

const CACHE_VERSION = "v1.0.0";
const STATIC_CACHE = `static-${CACHE_VERSION}`;
const RUNTIME_CACHE = `runtime-${CACHE_VERSION}`;
const OFFLINE_URL = "/offline.html";

// ---- Precache: 启动时立即缓存的静态资源 ----
const PRECACHE_URLS = [
  OFFLINE_URL,
  "/static/pwa/icon-192.png",
  "/static/pwa/icon-512.png",
  "/static/css/style.css",
  "/static/js/app.js",
  "/static/icons/fontawesome-free-7.2.0-web/css/all.min.css",
];

// ============================================================
// 安装：precache 离线页与核心资源
// ============================================================
self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(STATIC_CACHE);
      // 单独 add 单个文件，单个失败不影响其它缓存
      await Promise.all(
        PRECACHE_URLS.map((url) =>
          cache
            .add(new Request(url, { cache: "reload" }))
            .catch((err) => console.warn("[SW] precache skip:", url, err))
        )
      );
      // 立即激活新版
      await self.skipWaiting();
    })()
  );
});

// ============================================================
// 激活：清理旧版本缓存
// ============================================================
self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([STATIC_CACHE, RUNTIME_CACHE]);
      const names = await caches.keys();
      await Promise.all(
        names.map((name) => (keep.has(name) ? null : caches.delete(name)))
      );
      await self.clients.claim();
    })()
  );
});

// ============================================================
// 请求拦截
// ============================================================
self.addEventListener("fetch", (event) => {
  const req = event.request;

  // 只处理 GET
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // 跨域请求直接放行
  if (url.origin !== self.location.origin) return;

  // ---- 1) API 请求 → Network Only ----
  if (url.pathname.startsWith("/api/")) {
    return; // 默认行为
  }

  // ---- 2) Service Worker 自身 → 直接放行 ----
  if (url.pathname === "/sw.js" || url.pathname === "/manifest.json") {
    return;
  }

  // ---- 3) HTML 页面（导航请求）→ Network First ----
  if (req.mode === "navigate" || req.destination === "document") {
    event.respondWith(networkFirstNavigate(req));
    return;
  }

  // ---- 4) 静态资源 → Cache First + 后台重新校验 ----
  if (
    req.destination === "style" ||
    req.destination === "script" ||
    req.destination === "image" ||
    req.destination === "font" ||
    req.destination === "manifest" ||
    url.pathname.startsWith("/static/")
  ) {
    event.respondWith(cacheFirst(req));
    return;
  }

  // 其它 → 放行
});

// ---- 策略实现 ----

async function networkFirstNavigate(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  try {
    const fresh = await fetch(request, { cache: "no-store" });
    if (fresh && fresh.ok) {
      cache.put(request, fresh.clone()).catch(() => {});
    }
    return fresh;
  } catch (err) {
    // 离线 → 找缓存，命中则返回，否则返回 offline.html
    const cached = await cache.match(request);
    if (cached) return cached;
    const offline = await caches.match(OFFLINE_URL);
    return (
      offline ||
      new Response("离线状态，且未缓存该页面。", {
        status: 503,
        statusText: "Offline",
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      })
    );
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = await cache.match(request);
  if (cached) {
    // 后台异步刷新缓存（stale-while-revalidate 行为）
    fetch(request)
      .then((res) => {
        if (res && res.ok) cache.put(request, res.clone()).catch(() => {});
      })
      .catch(() => {});
    return cached;
  }
  try {
    const res = await fetch(request);
    if (res && res.ok && res.status === 200) {
      cache.put(request, res.clone()).catch(() => {});
    }
    return res;
  } catch (err) {
    // 资源缺失时的兜底
    if (request.destination === "image") {
      return new Response("", { status: 404 });
    }
    throw err;
  }
}

// ============================================================
// 消息：支持前端手动触发 update / skipWaiting
// ============================================================
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
  if (event.data && event.data.type === "CLEAR_CACHE") {
    event.waitUntil(
      (async () => {
        const names = await caches.keys();
        await Promise.all(names.map((n) => caches.delete(n)));
      })()
    );
  }
});
