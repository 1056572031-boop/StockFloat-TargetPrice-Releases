const RELEASE_BASE =
  "https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/releases/download/%E7%89%88%E6%9C%AC%E6%9B%B4%E6%96%B0/";
const VERSION_RE = /^\d+\.\d+\.\d+$/;
const CURRENT_VERSION = "__CURRENT_VERSION__";
const ICON_LIGHT = "__ICON_LIGHT__";
const ICON_DARK = "__ICON_DARK__";
const RETENTION_DAYS = 30;
const COOKIE_NAME = "skyrocket_download_id";
const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;
const ROUTES = new Set(["public", "release-check"]);
const RELEASE_PAGE =
  "https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/releases/tag/%E7%89%88%E6%9C%AC%E6%9B%B4%E6%96%B0";
const INSTALL_PAGE =
  "https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/blob/main/docs/INSTALL.md";
const MANIFEST_URL =
  "https://raw.githubusercontent.com/1056572031-boop/StockFloat-TargetPrice-Releases/main/Skyrocket-update.json";
const VERSION_CACHE_KEY = "https://skyrocket-downloads.pages.dev/__version-cache";
const VERSION_CACHE_SECONDS = 300;
const SECURITY_HEADERS = {
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
  // img-src 必须显式放行 data: —— 应用图标是以 data URI 内嵌的（和包体 icns 同一份出图），
  // 只写 default-src 'none' 会让 Chrome 把 CSS 背景图一起拦掉：页面全绿、图标却看不见。
  "content-security-policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
};

export function downloadPath(version, asset, route = "public") {
  const query = new URLSearchParams({ version, asset });
  if (route !== "public") query.set("route", route);
  return "/download?" + query.toString();
}

export function page(version, icons = { light: ICON_LIGHT, dark: ICON_DARK }) {
  const available = VERSION_RE.test(version);
  const dmg = available ? downloadPath(version, `Skyrocket-V${version}.dmg`) : "";
  const zip = available ? downloadPath(version, `Skyrocket-${version}-macos.zip`) : "";
  const sums = available ? downloadPath(version, `Skyrocket-${version}-SHA256SUMS.txt`) : "";
  const hasIcon = typeof icons.light === "string" && icons.light.startsWith("data:image/")
    && typeof icons.dark === "string" && icons.dark.startsWith("data:image/");
  const brand = hasIcon ? `
<div class="brand">
  <div class="logo" role="img" aria-label="Skyrocket 图标"></div>
  <div><h1>Skyrocket</h1><p class="sub">下载 macOS 版</p></div>
</div>` : `
<h1>Skyrocket</h1>
<p class="sub">下载 macOS 版</p>`;
  const actions = available ? `
<p class="meta"><span class="version">v${version}</span><span>Apple 芯片</span><span>macOS 26 或更高</span></p>
<div class="downloads">
  <a class="primary" href="${escapeHtml(dmg)}"><span>下载 DMG</span><small>推荐</small></a>
  <div class="secondary">
    <a href="${escapeHtml(zip)}">ZIP（终端安装）</a>
    <a href="${escapeHtml(sums)}">SHA-256 校验</a>
  </div>
</div>
<p class="fine">DMG 适合普通安装。下载无反应时，请稍后重试或打开 <a href="${RELEASE_PAGE}">GitHub 发布页</a>。</p>
<p class="fine">首次安装或系统拦截？查看 <a href="${INSTALL_PAGE}">安装说明</a>。</p>`
    : `
<p class="fine">当前版本信息暂未生成，请从 <a href="${RELEASE_PAGE}">GitHub 发布页</a> 下载。</p>`;
  const logoCss = hasIcon ? `
.logo{width:56px;height:56px;flex:none;background:url(${icons.light}) center/56px 56px no-repeat}` : "";
  const logoDarkCss = hasIcon ? `
  .logo{background-image:url(${icons.dark})}` : "";
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Skyrocket 下载</title>${hasIcon ? `
<link rel="icon" type="image/png" href="${icons.light}">` : ""}
<style>
*{box-sizing:border-box}
:root{color-scheme:light dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{margin:0;min-height:100vh;background:#f3f5f7;color:#17191d;display:flex;justify-content:center;align-items:flex-start}
main{width:min(560px,100%);padding:56px 20px}
.brand{display:flex;align-items:center;gap:14px;margin:0 0 18px}${logoCss}
h1{margin:0;font-size:26px;line-height:1.2;letter-spacing:-.01em}
.sub{margin:3px 0 0;font-size:14px;color:#5d6673}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 22px;font-size:12px;color:#5d6673}
.meta span{padding:3px 8px;border:1px solid #cfd5dc;border-radius:999px}
.meta .version{background:#17191d;border-color:#17191d;color:#fff;font-weight:600}
.card{padding:24px;border:1px solid #d7dce3;border-radius:16px;background:#fff}
.downloads{display:grid;gap:10px}
.primary{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-radius:10px;background:#17191d;color:#fff;text-decoration:none;font-size:16px;font-weight:600}
.primary small{font-size:12px;font-weight:500;opacity:.7}
.primary:hover{opacity:.92}
.secondary{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.secondary a{display:block;padding:11px 12px;border:1px solid #cfd5dc;border-radius:10px;text-align:center;color:inherit;text-decoration:none;font-size:14px}
.secondary a:hover{background:#f1f4f7}
.fine{margin:14px 0 0;font-size:13px;line-height:1.6;color:#5d6673}
.fine a{color:inherit;text-decoration:underline}
.privacy{margin:18px 2px 0;font-size:12px;line-height:1.6;color:#6b7482}
a:focus-visible{outline:2px solid #4c8dff;outline-offset:2px}
@media(max-width:380px){.secondary{grid-template-columns:1fr}}
@media(prefers-color-scheme:dark){
  body{background:#0f1216;color:#f2f4f8}${logoDarkCss}
  .sub,.meta,.fine,.privacy{color:#aab3c0}
  .meta span{border-color:#39414c}
  .meta .version{background:#f2f4f8;border-color:#f2f4f8;color:#15181d}
  .card{background:#15181d;border-color:#2c333d}
  .primary{background:#f2f4f8;color:#15181d}
  .secondary a{border-color:#3d4552}
  .secondary a:hover{background:#1c2129}
}
</style></head><body><main>
<section class="card">${brand}${actions}</section>
<p class="privacy">只记录版本、文件、时间与随机匿名标识（保存 30 天；到期后在访问或每日清理时删除）；统计库不保存 IP、账号或设备信息。应用内更新不经过这里。</p>
</main></body></html>`;
}

function json(status, body, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...SECURITY_HEADERS, "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extraHeaders },
  });
}

function html(status, body, extraHeaders = {}) {
  return new Response(body, {
    status,
    headers: { ...SECURITY_HEADERS, "content-type": "text/html; charset=utf-8", "cache-control": "no-store", ...extraHeaders },
  });
}

function text(status, body, extraHeaders = {}) {
  return new Response(body, {
    status,
    headers: { ...SECURITY_HEADERS, "content-type": "text/plain; charset=utf-8", "cache-control": "no-store", ...extraHeaders },
  });
}

function invalidPage() {
  return html(400, `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>下载链接无效</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:48px;line-height:1.7}@media(prefers-color-scheme:dark){body{background:#15181d;color:#f2f4f8}}</style></head>
<body><h1>下载链接无效</h1><p>这个地址缺少有效的版本或文件名。</p>
<p><a href="/">返回下载页</a>，或访问 <a href="${RELEASE_PAGE}">GitHub 官方发布页</a>。</p></body></html>`);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

export function allowedAsset(version, asset) {
  if (typeof version !== "string" || !VERSION_RE.test(version) || typeof asset !== "string") return false;
  return new Set([
    `Skyrocket-${version}-macos.zip`,
    `Skyrocket-${version}-source.zip`,
    `Skyrocket-${version}-SHA256SUMS.txt`,
    `Skyrocket-V${version}.dmg`,
  ]).has(asset);
}

export function releaseUrl(version, asset) {
  return RELEASE_BASE + encodeURIComponent(asset);
}

function cookieValue(request) {
  const header = request.headers.get("cookie") || "";
  const matched = header.match(new RegExp(`(?:^|;\\s*)${COOKIE_NAME}=([A-Za-z0-9-]{36})(?:;|$)`));
  return matched ? matched[1] : null;
}

function cookieHeader(visitorId) {
  return `${COOKIE_NAME}=${visitorId}; Max-Age=${RETENTION_DAYS * 86_400}; Path=/; Secure; HttpOnly; SameSite=Lax`;
}

export function dayKey(date) {
  return new Date(date.getTime() + BEIJING_OFFSET_MS).toISOString().slice(0, 10);
}

export async function latestVersion(fallback, force = false) {
  if (!force && !VERSION_RE.test(fallback)) return fallback;
  try {
    const response = await fetch(MANIFEST_URL, {
      signal: AbortSignal.timeout(1500),
      cf: { cacheTtl: 300, cacheEverything: true },
    });
    if (!response.ok) return fallback;
    const manifest = await response.json();
    const candidate = typeof manifest?.version === "string" ? manifest.version : "";
    return candidate === candidate.trim() && VERSION_RE.test(candidate) ? candidate : fallback;
  } catch {
    return fallback;
  }
}

async function refreshLatestVersion() {
  try {
    const version = await latestVersion("", true);
    if (!version) return;
    await caches.default.put(new Request(VERSION_CACHE_KEY), new Response(JSON.stringify({ version }), {
      headers: {
        "content-type": "application/json",
        "cache-control": `public, max-age=${VERSION_CACHE_SECONDS}, s-maxage=${VERSION_CACHE_SECONDS}`,
      },
    }));
  } catch {
    // A cache miss or an unavailable Cache API must never delay the page.
  }
}

export async function cachedLatestVersion(fallback, ctx) {
  if (!VERSION_RE.test(fallback)) return fallback;
  try {
    const hit = await caches.default.match(new Request(VERSION_CACHE_KEY));
    if (hit) {
      const data = await hit.json().catch(() => null);
      const version = data?.version;
      if (typeof version === "string" && version === version.trim() && VERSION_RE.test(version)) return version;
    }
    if (ctx?.waitUntil) ctx.waitUntil(refreshLatestVersion());
  } catch {
    // Fall through to the build-injected version.
  }
  return fallback;
}

export async function visitorHash(visitorId) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(visitorId));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function checkAuthorized(request, env) {
  if (!env.CHECK_TOKEN) return "missing";
  const header = request.headers.get("x-skyrocket-check") || "";
  const cookie = (request.headers.get("cookie") || "")
    .match(/(?:^|;\s*)skyrocket_check=([a-f0-9]{64})(?:;|$)/)?.[1] || "";
  // 机主在浏览器里手点没有自定义请求头可加，所以同一枚标记也允许走 HttpOnly Cookie。
  return constantTimeEqual(header, env.CHECK_TOKEN) || constantTimeEqual(cookie, env.CHECK_TOKEN)
    ? "ok" : "denied";
}

function basicAuthorized(request, env) {
  if (!env.CHECK_TOKEN) return false;
  const expected = "Basic " + btoa("owner:" + env.CHECK_TOKEN);
  return constantTimeEqual(request.headers.get("authorization") || "", expected);
}

async function record(env, request, route, version, asset, now) {
  const existing = cookieValue(request);
  const visitorId = existing || crypto.randomUUID();
  const expires = new Date(now.getTime() + RETENTION_DAYS * 86_400_000);
  await env.DB.prepare(
    "INSERT INTO download_events (id, occurred_day, occurred_at, expires_at, route, version, asset, visitor_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(
    crypto.randomUUID(),
    dayKey(now),
    now.toISOString(),
    expires.toISOString(),
    route,
    version,
    asset,
    await visitorHash(visitorId),
  ).run();
  try {
    await env.DB.prepare(
      "DELETE FROM download_events WHERE id IN (SELECT id FROM download_events WHERE expires_at < ? LIMIT 100)",
    ).bind(now.toISOString()).run();
  } catch {
    // Cleanup is best effort; an expired row does not affect the redirect or the count.
  }
  return existing
    ? null
    : cookieHeader(visitorId);
}

async function stats(env, days) {
  const since = dayKey(new Date(Date.now() - (days - 1) * 86_400_000));
  const totals = await env.DB.prepare(
    "SELECT COUNT(*) AS downloads, COUNT(DISTINCT visitor_hash) AS visitors FROM download_events WHERE occurred_day >= ?",
  ).bind(since).first();
  const rows = await env.DB.prepare(
    "SELECT occurred_day AS day, route, version, asset, COUNT(*) AS downloads, COUNT(DISTINCT visitor_hash) AS visitors FROM download_events WHERE occurred_day >= ? GROUP BY occurred_day, route, version, asset ORDER BY day DESC, route, version, asset",
  ).bind(since).all();
  return { since, days, totals: totals || { downloads: 0, visitors: 0 }, rows: rows.results || [] };
}

function routeLabel(route) {
  if (route === "public") return "公开下载";
  if (route === "release-check") return "发布验收";
  return route;
}

function statsPage(data) {
  const rows = data.rows.map(row => `<tr><td>${escapeHtml(row.day)}</td><td>${escapeHtml(routeLabel(row.route))}</td><td>${escapeHtml(row.version)}</td><td>${escapeHtml(row.asset)}</td><td>${escapeHtml(row.downloads)}</td><td>${escapeHtml(row.visitors)}</td></tr>`).join("");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>下载统计</title>
<style>:root{color-scheme:light dark}body{background:#f3f5f7;color:#17191d;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:24px}.wrap{overflow-x:auto}table{border-collapse:collapse;min-width:640px}th,td{border-bottom:1px solid #d9dee5;padding:8px 14px;text-align:left;font-size:14px}th{font-weight:600}@media(prefers-color-scheme:dark){body{background:#0f1216;color:#f2f4f8}th,td{border-color:#2c333d}}</style></head><body>
<h1>下载开始统计（近 ${escapeHtml(data.days)} 天）</h1>
<p>下载开始 ${escapeHtml(data.totals.downloads)} 次（含发布验收），匿名来源 ${escapeHtml(data.totals.visitors)} 个。</p>
<p>这里只统计网页下载入口，不包含应用内自动更新流量。日期按北京时间，明细最长保留 ${RETENTION_DAYS} 天。</p>
<p>“匿名来源”按行内去重；同一个人下载多个文件会在不同行各算一次。</p>
<div class="wrap"><table><thead><tr><th>日期</th><th>通路</th><th>版本</th><th>文件</th><th>下载开始</th><th>匿名来源</th></tr></thead><tbody>${rows || "<tr><td colspan=\"6\">暂无记录</td></tr>"}</tbody></table></div>
</body></html>`;
}

async function handleDownload(request, env, url) {
  const version = url.searchParams.get("version") || "";
  const asset = url.searchParams.get("asset") || "";
  const requestedRoute = url.searchParams.get("route") || "public";
  if (!ROUTES.has(requestedRoute) || !allowedAsset(version, asset)) return invalidPage();

  // 令牌对得上就算内部通路，**不看 route 参数**：首页三个链接都不带 route，
  // 只在参数里判会让机主手点仍被记成公开下载（上一轮就是这么漏的，测试也只测了带 route 的那半）。
  const authorized = checkAuthorized(request, env);
  let route = "public";
  if (authorized === "ok") {
    route = "release-check";
  } else if (requestedRoute === "release-check") {
    if (authorized === "missing") return json(503, { error: "check_not_configured" });
    return json(403, { error: "check_not_authorized" });
  }

  if (request.method === "HEAD") {
    return new Response(null, { status: 302, headers: {
      location: releaseUrl(version, asset), "cache-control": "no-store",
      "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
    } });
  }

  let cookie = null;
  try {
    cookie = await record(env, request, route, version, asset, new Date());
  } catch (error) {
    // Statistics must never block an official download.
    console.error(JSON.stringify({ event: "download_event_unrecorded", category: error?.name || "unknown" }));
  }
  const headers = new Headers({
    location: releaseUrl(version, asset), "cache-control": "no-store",
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
  });
  if (cookie) headers.set("set-cookie", cookie);
  return new Response(null, { status: 302, headers });
}

async function route(request, env, ctx) {
  const url = new URL(request.url);
  // HEAD 与 GET 走同一套读路由，正文由外层 fetch 统一剥掉。
  // 各分支只认 GET 的话，HEAD 会一路掉到最后的 404 —— 探活工具会把好站判成挂掉。
  const reads = request.method === "GET" || request.method === "HEAD";
  if (reads && (url.pathname === "/" || url.pathname === "/index.html")) {
    const existing = cookieValue(request);
    const headers = existing ? {} : { "set-cookie": cookieHeader(crypto.randomUUID()) };
    return html(200, page(await cachedLatestVersion(CURRENT_VERSION, ctx)), headers);
  }
  if (reads && url.pathname === "/health") {
    let database = false;
    try {
      const table = await env.DB.prepare(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='download_events'",
      ).first();
      database = table?.name === "download_events";
    } catch {
      database = false;
    }
    return json(200, { ok: true, database, build_version: CURRENT_VERSION,
      retention_days: RETENTION_DAYS, free_only: true });
  }
  if (reads && url.pathname === "/robots.txt") return text(200, "User-agent: *\nDisallow: /\n");
  if (reads && url.pathname === "/favicon.ico") return new Response(null, { status: 204 });
  if (url.pathname === "/download") {
    if (request.method !== "GET" && request.method !== "HEAD") return json(405, { error: "method_not_allowed" });
    return handleDownload(request, env, url);
  }
  if (reads && url.pathname === "/owner") {
    if (!basicAuthorized(request, env)) {
      return json(401, { error: "authentication_required" }, { "www-authenticate": 'Basic realm="download-owner"' });
    }
    // 机主模式：浏览器手点没有自定义头可加，用同一枚标记换一枚 30 天的 HttpOnly Cookie。
    const headers = new Headers({
      location: "/", "cache-control": "no-store",
      "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
      "set-cookie": `skyrocket_check=${env.CHECK_TOKEN}; Max-Age=${RETENTION_DAYS * 86_400}; Path=/; Secure; HttpOnly; SameSite=Lax`,
    });
    return new Response(null, { status: 302, headers });
  }
  if (reads && url.pathname === "/stats") {
    if (!basicAuthorized(request, env)) {
      return json(401, { error: "authentication_required" }, { "www-authenticate": 'Basic realm="download-stats"' });
    }
    // 数据最长只留 RETENTION_DAYS 天，窗口不许选到比保留期更长（否则标题在承诺不存在的数据）。
    const days = Math.min(Math.max(Number(url.searchParams.get("days")) || RETENTION_DAYS, 1), RETENTION_DAYS);
    try {
      return html(200, statsPage(await stats(env, days)));
    } catch {
      return json(503, { error: "stats_unavailable" });
    }
  }
  return json(404, { error: "not_found" });
}

export default {
  async fetch(request, env, ctx) {
    const response = await route(request, env, ctx);
    // HEAD 的响应不允许带正文；集中剥一次，免得每条分支各自记得（漏一处就是 400/403 带 HTML）。
    return request.method === "HEAD" && response.body !== null
      ? new Response(null, { status: response.status, headers: response.headers })
      : response;
  },

  async scheduled(_event, env, ctx) {
    ctx.waitUntil((async () => {
      const cutoff = new Date().toISOString();
      for (let batch = 0; batch < 10; batch += 1) {
        const result = await env.DB.prepare(
          "DELETE FROM download_events WHERE id IN (SELECT id FROM download_events WHERE expires_at < ? LIMIT 500)",
        ).bind(cutoff).run();
        if (!result.meta || !result.meta.changes) break;
      }
    })());
  },
};
