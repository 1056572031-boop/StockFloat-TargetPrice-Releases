import { landingPage } from "./landing.mjs";

const RELEASE_BASE =
  "https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/releases/download/%E7%89%88%E6%9C%AC%E6%9B%B4%E6%96%B0/";
const VERSION_RE = /^\d+\.\d+\.\d+$/;
const CURRENT_VERSION = "__CURRENT_VERSION__";
// 构建时替换成数字（本版 DMG 的字节数）；未注入时保持字符串，页面就不显示大小。
const CURRENT_DMG_BYTES = "__CURRENT_DMG_BYTES__";
const RETENTION_DAYS = 30;
const LANDING_BROWSER_CACHE_SECONDS = 60;
const LANDING_EDGE_CACHE_SECONDS = 300;
const EVENT_DEDUPE_SECONDS = 60;
const COOKIE_NAME = "skyrocket_download_id";
// 只识别特征明确的自动访问。判断只在请求期间使用，既不保存 User-Agent，也不会拒绝或延迟下载。
// 不用泛泛的 "bot" 子串：CUBOT 这类手机型号会被误伤。改为"已知爬虫名"加"+http 自报家门"这一通行写法
// （守规矩的爬虫都会在 User-Agent 里留一个 +https://… 说明页，真实浏览器不会有）。
const AUTOMATED_USER_AGENT = new RegExp([
  "googlebot", "bingbot", "yandexbot", "duckduckbot", "applebot", "slackbot", "twitterbot", "telegrambot", "discordbot",
  "linkedinbot", "petalbot", "semrushbot", "ahrefsbot", "mj12bot", "dotbot", "gptbot", "claudebot", "ccbot", "amazonbot",
  "crawler", "spider", "slurp", "facebookexternalhit", "whatsapp", "bingpreview", "skypeuripreview", "embedly", "vkshare",
  "headless", "phantomjs", "puppeteer", "playwright", "selenium", "lighthouse", "pingdom", "uptimerobot", "statuscake",
  "curl\\/", "wget\\/", "python-requests", "python-urllib", "aiohttp", "go-http-client", "java\\/", "libwww", "node-fetch",
  "axios\\/", "postman", "\\+https?:\\/\\/",
].join("|"), "i");
const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;
const ROUTES = new Set(["public", "release-check"]);
const RELEASE_PAGE =
  "https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/releases/tag/%E7%89%88%E6%9C%AC%E6%9B%B4%E6%96%B0";
const MANIFEST_URL =
  "https://raw.githubusercontent.com/1056572031-boop/StockFloat-TargetPrice-Releases/main/Skyrocket-update.json";
const VERSION_CACHE_KEY = "https://skyrocket-downloads.pages.dev/__version-cache";
const VERSION_CACHE_SECONDS = 300;
const SECURITY_HEADERS = {
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
  // img-src 必须同时放行 data: 和 'self'：应用图标以 data URI 内嵌（和包体 icns 同一份出图），
  // 其余图片、样式表、脚本是同源静态文件（public/assets）。只写 default-src 'none' 时 Chrome 会
  // 静默拦掉它们：页面仍是 200，图标和样式却不出现。script-src 只放同源，不放 unsafe-inline。
  // style-src 保留 'unsafe-inline'：/stats 与 400 页仍是内联 <style>，收紧会让它们丢样式。
  // 真正收紧的是 script-src：只许同源文件，不许内联脚本。
  // media-src 'self'：首屏小窗是同源视频；不放它时 default-src 'none' 会让视频静默不加载（页面 200，只剩封面图）。
  "content-security-policy": "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  // 本页不需要任何设备能力：全部显式关掉，万一以后被注入也拿不到摄像头、定位这类权限。
  "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), accelerometer=(), gyroscope=(), magnetometer=()",
  // 不与别的来源共享浏览上下文，也不许别的来源把这些响应当子资源嵌进去。
  "cross-origin-opener-policy": "same-origin",
  "cross-origin-resource-policy": "same-origin",
  // 一年，不带 includeSubDomains / preload：这条是"以后别再走明文"，不替整个域名做主。
  "strict-transport-security": "max-age=31536000",
};
// 这些路径只认 GET / HEAD；其他方法给 405 和 Allow，而不是笼统的 404。
const READ_ONLY_PATHS = new Set(["/", "/index.html", "/health", "/privacy", "/robots.txt", "/favicon.ico", "/owner", "/stats"]);
// 首页没有内联样式（样式表是同源文件，内联 SVG 只用呈现属性），所以单独收紧：样式也只许同源。
// 构建时把样式表内联进页面（省掉首次绘制前的一次往返），并把内联样式的 sha256 写进来：
// 策略里没有 'unsafe-inline'，页面上多出任何别的内联样式都会被浏览器拒绝。源码形态（未构建）没有内联样式，只许同源。
const LANDING_STYLE_HASH = "__LANDING_STYLE_HASH__";
const LANDING_CSP = SECURITY_HEADERS["content-security-policy"].replace("style-src 'self' 'unsafe-inline'",
  LANDING_STYLE_HASH.startsWith("__") ? "style-src 'self'" : `style-src 'self' '${LANDING_STYLE_HASH}'`);
// 只能导出函数：Cloudflare 的 Worker 模块不接受字符串之类的非函数导出，运行时会直接拒绝启动（node 里的测试看不出来）。
export function landingCsp() { return LANDING_CSP; }

export function downloadPath(version, asset, route = "public") {
  const query = new URLSearchParams({ version, asset });
  if (route !== "public") query.set("route", route);
  return "/download?" + query.toString();
}

export function landingCacheControl() {
  return `public, max-age=${LANDING_BROWSER_CACHE_SECONDS}, s-maxage=${LANDING_EDGE_CACHE_SECONDS}, stale-while-revalidate=${LANDING_BROWSER_CACHE_SECONDS}`;
}

export function isAutomatedRequest(request) {
  const userAgent = request.headers.get("user-agent") || "";
  const purpose = request.headers.get("purpose") || request.headers.get("sec-purpose") || "";
  return AUTOMATED_USER_AGENT.test(userAgent) || /(?:^|,)\s*prefetch\s*(?:,|$)/i.test(purpose);
}

// dmgBytes：本版 DMG 的字节数。只有页面显示的版本就是构建时那一版才可信，
// 调用方（路由）负责判断；传 null/未注入的占位串时页面不显示大小。
export function page(version, dmgBytes = CURRENT_DMG_BYTES) {
  const available = VERSION_RE.test(version);
  const dmg = available ? downloadPath(version, `Skyrocket-V${version}.dmg`) : "";
  const zip = available ? downloadPath(version, `Skyrocket-${version}-macos.zip`) : "";
  const sums = available ? downloadPath(version, `Skyrocket-${version}-SHA256SUMS.txt`) : "";
  return landingPage({
    version,
    dmg,
    zip,
    sums,
    releasePage: RELEASE_PAGE,
    dmgBytes: typeof dmgBytes === "number" ? dmgBytes : null,
  });
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

// ---- 视频的分段请求（Range） ----
// Pages 的静态文件层对 Range 一律回 200 全量、不带 Accept-Ranges；Safari 播 <video> 依赖 206，
// 所以视频文件改由 Worker 经 ASSETS 绑定取回后自己切片。地址和内容指纹不变，其余素材仍由 Pages 直接提供。
const VIDEO_TYPES = { webm: "video/webm", mov: "video/quicktime", mp4: "video/mp4" };

export function videoTypeFor(pathname) {
  const match = /\.([a-z0-9]+)$/i.exec(pathname);
  return match ? VIDEO_TYPES[match[1].toLowerCase()] || "" : "";
}

// 只认单段 bytes=a-b / bytes=a- / bytes=-n。
// 返回 null：忽略 Range、整份回 200（没带、写法不认识、多段、b 小于 a）；"unsatisfiable"：回 416；否则 { start, end }（含端点）。
export function parseRange(header, size) {
  if (!header) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!match || !Number.isInteger(size) || size <= 0) return null;
  const [, first, last] = match;
  if (first === "" && last === "") return null;
  if (first === "") {
    const suffix = Number(last);
    return suffix === 0 ? "unsatisfiable" : { start: Math.max(size - suffix, 0), end: size - 1 };
  }
  const start = Number(first);
  if (start >= size) return "unsatisfiable";
  if (last === "") return { start, end: size - 1 };
  const end = Number(last);
  return end < start ? null : { start, end: Math.min(end, size - 1) };
}

async function serveVideo(request, env) {
  // 干净的 GET：不带浏览器的 Range 和 If-None-Match，免得静态层回 304 或别的半截响应，我们需要完整正文来切片。
  const upstream = await env.ASSETS.fetch(new Request(request.url, { method: "GET" }));
  if (!upstream.ok) return json(upstream.status === 404 ? 404 : 502, { error: upstream.status === 404 ? "not_found" : "asset_unavailable" });
  const body = new Uint8Array(await upstream.arrayBuffer());
  const size = body.byteLength;
  const etag = upstream.headers.get("etag");
  const base = {
    "content-type": videoTypeFor(new URL(request.url).pathname) || upstream.headers.get("content-type") || "application/octet-stream",
    "accept-ranges": "bytes",
    // 与 public/_headers 给其余素材的一致：地址带内容指纹，所以可以放心长缓存。
    "cache-control": "public, max-age=31536000, immutable",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    ...(etag ? { etag } : {}),
  };
  const rangeHeader = request.headers.get("range");
  const ifRange = request.headers.get("if-range");
  // If-Range 只认强校验的 ETag 完全相等；对不上就当没带 Range，整份回 200（安全的一侧）。
  const honored = rangeHeader && (!ifRange || (etag && !etag.startsWith("W/") && ifRange === etag));
  const range = honored ? parseRange(rangeHeader, size) : null;
  if (range === "unsatisfiable") {
    return new Response(null, { status: 416, headers: { ...base, "content-range": `bytes */${size}` } });
  }
  if (range) {
    const slice = body.subarray(range.start, range.end + 1);
    return new Response(slice, { status: 206, headers: {
      ...base, "content-range": `bytes ${range.start}-${range.end}/${size}`, "content-length": String(slice.byteLength) } });
  }
  return new Response(body, { status: 200, headers: { ...base, "content-length": String(size) } });
}

function invalidPage() {
  return html(400, `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>下载链接无效</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:48px;line-height:1.7}@media(prefers-color-scheme:dark){body{background:#15181d;color:#f2f4f8}}</style></head>
<body><h1>下载链接无效</h1><p>这个地址缺少有效的版本或文件名。</p>
<p><a href="/">返回下载页</a>，或访问 <a href="${RELEASE_PAGE}">GitHub 官方发布页</a>。</p></body></html>`);
}

// 统计说明：首页页脚的小链接指到这里。首页本身不再放整句说明（按负责人 B8 的要求），
// 但这个入口让"点下载时会存一个随机编号 cookie"有处可查。
function privacyPage() {
  return html(200, `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>统计说明</title><style>:root{color-scheme:dark}body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;margin:48px auto;max-width:34em;padding:0 20px;line-height:1.8;background:#0f1216;color:#f2f4f8}a{color:#7aa7ff}h1{font-size:22px}</style></head>
<body><h1>统计说明</h1>
<p>处理者：Skyrocket 项目维护者（GitHub 发行页的所有者）。目的：统计网页下载次数。</p>
<p>只打开本页不会保存任何标识。点击下载时，浏览器才会保存一个随机生成的标识（cookie，不含姓名等身份信息），并记录版本、文件、时间，以及该标识的哈希值。统计库不保存 IP、账号、设备信息或浏览器类型。</p>
<p>同一浏览器在 ${EVENT_DEDUPE_SECONDS} 秒内重复下载同一文件只记录一次；搜索引擎、链接预览和明确标识的自动访问不写入统计，也不会创建 cookie。它们仍会正常跳转到官方下载。</p>
<p>记录保存 ${RETENTION_DAYS} 天，到期后在访问或每日清理时删除。清除本站 cookie 后，新的下载不会再与之前的记录关联。</p>
<p>需要查询或删除记录，请到 <a href="https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/issues">GitHub 问题页</a>留言说明需求。请不要在公开页面贴出 cookie 的值，维护者回复后再商定核对方式。</p>
<p>应用内的自动更新不经过这里。</p>
<p><a href="/">返回下载页</a></p></body></html>`, { "cache-control": "no-cache" });
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

// 按数字逐段比较 X.Y.Z；字符串比较会把 1.4.9 排在 1.4.10 后面。
export function compareVersions(a, b) {
  const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < 3; i += 1) if (x[i] !== y[i]) return x[i] > y[i] ? 1 : -1;
  return 0;
}

// 大小只对构建时那一版有效；线上清单已前进到更新版本时宁可不显示，也不显示旧版的大小。
export function dmgBytesFor(shownVersion, builtVersion = CURRENT_VERSION, builtBytes = CURRENT_DMG_BYTES) {
  return shownVersion === builtVersion && typeof builtBytes === "number" && builtBytes > 0 ? builtBytes : null;
}

export async function cachedLatestVersion(fallback, ctx) {
  if (!VERSION_RE.test(fallback)) return fallback;
  try {
    const hit = await caches.default.match(new Request(VERSION_CACHE_KEY));
    if (hit) {
      const data = await hit.json().catch(() => null);
      const version = data?.version;
      // 取缓存版本和构建版本里较新的一个：发版后马上部署时，边缘缓存里可能还是上一版清单（最长 5 分钟），
      // 直接采用会让页面显示旧版本，部署读回也会因此误失败。
      if (typeof version === "string" && version === version.trim() && VERSION_RE.test(version)) {
        return compareVersions(version, fallback) >= 0 ? version : fallback;
      }
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

// 机主 Cookie 里只放由令牌派生的值，而不是令牌本身：Cookie 泄露不等于令牌泄露，
// 发布验收脚本走请求头，仍然用真实令牌。
async function ownerCookieValue(token) {
  return visitorHash(`skyrocket-owner-cookie:${token}`);
}

async function checkAuthorized(request, env) {
  if (!env.CHECK_TOKEN) return "missing";
  const header = request.headers.get("x-skyrocket-check") || "";
  const cookie = (request.headers.get("cookie") || "")
    .match(/(?:^|;\s*)skyrocket_check=([a-f0-9]{64})(?:;|$)/)?.[1] || "";
  // 请求头仍接受真实 CHECK_TOKEN，浏览器里的 HttpOnly Cookie 只保存派生值，泄露 Cookie 不等于泄露令牌。
  const derived = await ownerCookieValue(env.CHECK_TOKEN);
  return constantTimeEqual(header, env.CHECK_TOKEN) || constantTimeEqual(cookie, derived)
    ? "ok" : "denied";
}

function basicAuthorized(request, env) {
  if (!env.CHECK_TOKEN) return false;
  const expected = "Basic " + btoa("owner:" + env.CHECK_TOKEN);
  return constantTimeEqual(request.headers.get("authorization") || "", expected);
}

// 访客标识只在内存里决定，不碰数据库：响应里的 cookie 不必等写库。
function visitorFor(request) {
  const existing = cookieValue(request);
  const visitorId = existing || crypto.randomUUID();
  return { visitorId, cookie: existing ? null : cookieHeader(visitorId) };
}

// 版本号比已知最新版还新，多半是手填的地址在灌水：不记统计，下载照常转过去。
// 已知最新版取不到、或刷新后查不到时一律放行（宁可多记一条，也不因为查不到就漏记真实下载）。
export async function versionIsPlausible(version, known, refresh = () => latestVersion("", true)) {
  if (!VERSION_RE.test(known) || compareVersions(version, known) <= 0) return true;
  const fresh = await refresh();
  if (!VERSION_RE.test(fresh)) return true;
  return compareVersions(version, fresh) <= 0;
}

// 事件主键按“来源、通路、版本、文件、分钟”确定：同一浏览器一分钟内的重复点击会原子地被 D1 忽略。
// 这不是拒绝下载的限速，写库失败或重复都不影响 302；新一分钟的真实重新下载仍会计数。
export async function downloadEventId(visitorId, route, version, asset, now) {
  const minute = Math.floor(now.getTime() / (EVENT_DEDUPE_SECONDS * 1000));
  return visitorHash(`skyrocket-download-event:v1:${visitorId}:${route}:${version}:${asset}:${minute}`);
}

async function writeEvent(env, visitorId, route, version, asset, now) {
  const expires = new Date(now.getTime() + RETENTION_DAYS * 86_400_000);
  await env.DB.prepare(
    "INSERT OR IGNORE INTO download_events (id, occurred_day, occurred_at, expires_at, route, version, asset, visitor_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(
    await downloadEventId(visitorId, route, version, asset, now),
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
<p>“匿名来源”按行内去重；同一个人下载多个文件会在不同行各算一次。相同浏览器在一分钟内重复下载同一文件只记一次；自动访问不计入。</p>
<div class="wrap"><table><thead><tr><th>日期</th><th>通路</th><th>版本</th><th>文件</th><th>下载开始</th><th>匿名来源</th></tr></thead><tbody>${rows || "<tr><td colspan=\"6\">暂无记录</td></tr>"}</tbody></table></div>
</body></html>`;
}

async function handleDownload(request, env, url, ctx) {
  const version = url.searchParams.get("version") || "";
  const asset = url.searchParams.get("asset") || "";
  const requestedRoute = url.searchParams.get("route") || "public";
  if (!ROUTES.has(requestedRoute) || !allowedAsset(version, asset)) return invalidPage();

  // 令牌对得上就算内部通路，**不看 route 参数**：首页三个链接都不带 route，
  // 只在参数里判会让机主手点仍被记成公开下载（上一轮就是这么漏的，测试也只测了带 route 的那半）。
  const authorized = await checkAuthorized(request, env);
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

  // 明确标识为自动访问时不下发 cookie、不写统计，但下载地址始终照常转跳。
  // 不把无法识别的请求当机器人，宁可多记也不能让真实用户被误过滤。
  if (route === "public" && isAutomatedRequest(request)) {
    return new Response(null, { status: 302, headers: {
      location: releaseUrl(version, asset), "cache-control": "no-store",
      "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
    } });
  }

  // 统计写库放到后台：D1 慢或不可用都不该拖慢、更不该挡住官方下载。
  // 没有后台执行上下文时（本地脚本、单元测试）退回原来的"等写完再转跳"。
  const { visitorId, cookie } = visitorFor(request);
  const now = new Date();
  const recorded = (async () => {
    if (!await versionIsPlausible(version, await cachedLatestVersion(CURRENT_VERSION, ctx))) {
      console.error(JSON.stringify({ event: "download_event_skipped", reason: "version_ahead_of_latest" }));
      return;
    }
    await writeEvent(env, visitorId, route, version, asset, now);
  })().catch(error => {
    // Statistics must never block an official download.
    console.error(JSON.stringify({ event: "download_event_unrecorded", category: error?.name || "unknown" }));
  });
  if (typeof ctx?.waitUntil === "function") ctx.waitUntil(recorded); else await recorded;
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
    // 打开页面不设任何 cookie：访客编号只在点下载（/download 的响应）时才写入，
    // 这样"处理前告知"对应的是用户自己的一次点击，而不是一次单纯的浏览。
    const shown = await cachedLatestVersion(CURRENT_VERSION, ctx);
    // 页面不带 cookie，因此可短暂缓存：浏览器 60 秒、边缘 5 分钟。版本刷新有 Worker 自己的 5 分钟缓存，
    // 两者同量级；发版部署会切换产物，旧页面至多短暂保留，静态素材又因内容指纹不会串版本。
    return html(200, page(shown, dmgBytesFor(shown)), { "content-security-policy": LANDING_CSP, "cache-control": landingCacheControl() });
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
  if (!reads && (READ_ONLY_PATHS.has(url.pathname) || url.pathname.startsWith("/assets/"))) {
    return json(405, { error: "method_not_allowed" }, { allow: "GET, HEAD" });
  }
  if (reads && url.pathname === "/privacy") return privacyPage();
  if (reads && url.pathname === "/robots.txt") return text(200, "User-agent: *\nDisallow: /\n");
  if (reads && url.pathname === "/favicon.ico") return new Response(null, { status: 204 });
  if (url.pathname === "/download") {
    if (request.method !== "GET" && request.method !== "HEAD") return json(405, { error: "method_not_allowed" }, { allow: "GET, HEAD" });
    return handleDownload(request, env, url, ctx);
  }
  if (reads && url.pathname === "/owner") {
    if (!basicAuthorized(request, env)) {
      return json(401, { error: "authentication_required" }, { "www-authenticate": 'Basic realm="download-owner"' });
    }
    // 机主模式：浏览器手点没有自定义头可加，用同一枚标记换一枚 30 天的 HttpOnly Cookie。
    const headers = new Headers({
      location: "/", "cache-control": "no-store",
      "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
      "set-cookie": `skyrocket_check=${await ownerCookieValue(env.CHECK_TOKEN)}; Max-Age=${RETENTION_DAYS * 86_400}; Path=/; Secure; HttpOnly; SameSite=Lax`,
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
  // 静态素材正常由 Pages 直接提供（_routes.json 逐个排除了非视频素材）；
  // 万一路由排除失效，这里兜底交给静态资产绑定，免得页面样式整体 404。
  // 视频不在排除名单里，一定会走到这里：由 serveVideo 负责分段请求。
  if (reads && url.pathname.startsWith("/assets/") && env.ASSETS) {
    return videoTypeFor(url.pathname) ? serveVideo(request, env) : env.ASSETS.fetch(request);
  }
  return json(404, { error: "not_found" });
}

// 路由里冒出没想到的异常时的兜底：不让访客看到平台的通用错误页。
// 统计或鉴权出问题不该挡住官方下载，所以合法的公开下载地址照样转跳；
// 发布验收那条通路不装作成功，只回 500。本函数自己也不能抛。
export function failureResponse(request, error) {
  try {
    console.error(JSON.stringify({ event: "request_failed", category: error?.name || "unknown" }));
    const url = new URL(request.url);
    if (url.pathname === "/download") {
      const version = url.searchParams.get("version") || "";
      const asset = url.searchParams.get("asset") || "";
      if ((url.searchParams.get("route") || "public") === "public" && allowedAsset(version, asset)) {
        return new Response(null, { status: 302, headers: {
          location: releaseUrl(version, asset), "cache-control": "no-store",
          "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
        } });
      }
    }
    if (url.pathname === "/" || url.pathname === "/index.html") {
      return html(503, `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Skyrocket 下载</title><style>:root{color-scheme:dark}body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;margin:48px auto;max-width:34em;padding:0 20px;line-height:1.8;background:#0f1216;color:#f2f4f8}a{color:#7aa7ff}</style></head>
<body><h1>页面暂时打不开</h1><p>稍后刷新再试，或直接到 <a href="${RELEASE_PAGE}">GitHub 官方发布页</a>下载。</p></body></html>`, { "retry-after": "60" });
    }
    return json(500, { error: "internal_error" });
  } catch {
    return new Response("Service unavailable", { status: 503, headers: { "cache-control": "no-store" } });
  }
}

export default {
  async fetch(request, env, ctx) {
    let response;
    try {
      response = await route(request, env, ctx);
    } catch (error) {
      response = failureResponse(request, error);
    }
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
