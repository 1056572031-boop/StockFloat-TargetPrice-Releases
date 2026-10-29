import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readCloudflareToken } from "../feedback-worker/cloudflare-auth.mjs";
import { mapLimit } from "./fetch-pool.mjs";

// 读回要取回页面登记的全部素材（35 份，含几份 1 到 2 MB 的视频）再做视频分段对拍。
// 2026-10-29 实测：46 个请求一次性并发，本机链路只有约 300 到 500 KB/s，带宽被摊薄，排在后面的请求撑不到 10 秒上限，
// 整轮读回连续超时，而逐条取都正常（2.2 MB 的首屏视频单独取 6 到 7 秒）。并发 6 + 30 秒仍有一份 2.2 MB 的视频每轮超时；
// 并发 2 + 45 秒连续三轮 0 超时，最慢一项 17.9 秒。所以带正文的请求限并发、单项上限放宽到能在慢线路上取完一份 2 MB 视频；
// 判据本身（状态码、类型、缓存头、内容哈希、Range 对拍）一条不放宽。
const READBACK_CONCURRENCY = 2;
const READBACK_BODY_TIMEOUT_MS = 45000;

const root = fileURLToPath(new URL(".", import.meta.url));
const account = "45a8a4ed1f283f7449697c5958207c56";
const project = "skyrocket-downloads";
const expectedSubdomain = "skyrocket-downloads.pages.dev";
const databaseId = "088fcda4-015d-4116-a7ba-bafe50d0346f";
const schemaPath = resolve(root, "schema/download_events.sql");
const wrangler = resolve(root, "../feedback-worker/node_modules/.bin/wrangler");
const token = readCloudflareToken(wrangler);
const checkToken = execFileSync("/usr/bin/security",
  ["find-generic-password", "-s", "SkyrocketDownloadCheck", "-w"], { encoding: "utf8" }).trim();
if (!/^[a-f0-9]{64}$/.test(checkToken)) throw new Error("Download check token is unavailable or malformed");
const constants = await fs.readFile(resolve(root, "../../sf_constants.py"), "utf8");
const version = constants.match(/^APP_VERSION\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'/m)?.[1];
if (!version) throw new Error("APP_VERSION is unavailable");

// Never deploy a stale landing page: build injects the current sf_constants.APP_VERSION.
execFileSync(process.execPath, [resolve(root, "build-pages.mjs")], { cwd: root, stdio: "inherit" });

async function cloudflare(path, method = "GET", data) {
  const response = await fetch(`https://api.cloudflare.com/client/v4/accounts/${account}${path}`, {
    method, redirect: "error", signal: AbortSignal.timeout(30000),
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const result = await response.json();
  if (response.status === 404 && method === "GET") return null;
  if (!response.ok || !result.success) throw new Error(`Cloudflare API HTTP ${response.status}`);
  return result.result;
}

let existing = await cloudflare(`/pages/projects/${project}`);
if (!existing) {
  existing = await cloudflare("/pages/projects", "POST", { name: project, production_branch: "main" });
  console.log("created_pages_project");
}
await cloudflare(`/pages/projects/${project}`, "PATCH", {
  deployment_configs: {
    production: {
      compatibility_date: "2026-10-24",
      d1_databases: { DB: { id: databaseId } },
      env_vars: { CHECK_TOKEN: { type: "secret_text", value: checkToken } },
    },
  },
});
console.log("configured_d1_and_check_token");

const schema = await fs.readFile(schemaPath, "utf8");
await cloudflare(`/d1/database/${databaseId}/query`, "POST", { sql: schema });
console.log("applied_download_events_schema");

// Pages only auto-detects wrangler.jsonc in the working directory and rejects --config.
// Deploy from a private temporary directory so the Worker config in this folder is untouched.
const worker = `${project}-cleanup`;
const cleanupCredentials = await fs.mkdtemp(join(tmpdir(), "skyrocket-downloads-worker-"));
try {
  // Wrangler resolves main relative to its config. Make the source absolute in the private staging config.
  const cleanupWorkerConfig = {
    name: worker,
    main: resolve(root, "src/index.mjs"),
    compatibility_date: "2026-10-24",
    account_id: account,
    workers_dev: false,
    observability: { enabled: false },
    d1_databases: [{ binding: "DB", database_name: "skyrocket-downloads", database_id: databaseId }],
    triggers: { crons: ["17 3 * * *"] },
  };
  await fs.writeFile(join(cleanupCredentials, "wrangler.jsonc"), JSON.stringify(cleanupWorkerConfig, null, 2) + "\n", {
    mode: 0o600,
  });
  execFileSync(wrangler, ["deploy", "--config", join(cleanupCredentials, "wrangler.jsonc")], {
    cwd: cleanupCredentials, stdio: "inherit",
    env: { ...process.env, CLOUDFLARE_ACCOUNT_ID: account, WRANGLER_SEND_METRICS: "false" },
  });
} finally {
  await fs.rm(cleanupCredentials, { recursive: true, force: true });
}
const schedules = await cloudflare(`/workers/scripts/${worker}/schedules`);
const registeredSchedules = Array.isArray(schedules) ? schedules : schedules?.schedules;
if (!Array.isArray(registeredSchedules) || !registeredSchedules.some(schedule => schedule.cron === "17 3 * * *")) {
  throw new Error("cleanup cron is not registered");
}
console.log("deployed_cleanup_cron_worker");

const staging = await fs.mkdtemp(join(tmpdir(), "skyrocket-downloads-pages-"));
try {
  await fs.writeFile(join(staging, "wrangler.jsonc"), JSON.stringify({
    name: project,
    pages_build_output_dir: resolve(root, "dist/pages"),
    compatibility_date: "2026-10-24",
    d1_databases: [{ binding: "DB", database_name: "skyrocket-downloads", database_id: databaseId }],
  }, null, 2) + "\n", { mode: 0o600 });
  execFileSync(wrangler, ["pages", "deploy", resolve(root, "dist/pages"), "--project-name", project,
    "--branch", "main", "--commit-dirty=true"], {
    cwd: staging, stdio: "inherit",
    env: { ...process.env, CLOUDFLARE_ACCOUNT_ID: account, WRANGLER_SEND_METRICS: "false" },
  });
} finally {
  await fs.rm(staging, { recursive: true, force: true });
}
const readback = await cloudflare(`/pages/projects/${project}`);
if (readback.subdomain !== expectedSubdomain) {
  throw new Error(`unexpected Pages subdomain: ${readback.subdomain}`);
}
const base = `https://${readback.subdomain}`;
let liveOk = false;
let lastObservation = "not attempted";
for (let attempt = 0; attempt < 6 && !liveOk; attempt += 1) {
  try {
    const pageResponse = await fetch(base + "/", {
      signal: AbortSignal.timeout(10000), headers: { "cache-control": "no-cache" },
    });
    const pageBody = await pageResponse.text();
    const healthResponse = await fetch(base + "/health", { signal: AbortSignal.timeout(10000) });
    const healthBody = await healthResponse.json().catch(() => ({}));
    const buttonCount = (pageBody.match(/href="\/download\?/g) || []).length;
    const csp = pageResponse.headers.get("content-security-policy") || "";
    // script-src 必须恰好是 'self'：子串匹配拦不住有人追加 'unsafe-inline'（读回演练实测漏过）。
    const scriptSrc = (csp.match(/(?:^|;)\s*script-src([^;]*)/) || [])[1]?.trim();
    const cspOk = csp.includes("img-src 'self' data:") && csp.includes("media-src 'self'") && scriptSrc === "'self'";
    // 素材地址从线上页面里读出来（带 ?v=内容哈希），而不是自己拼：拼出来的地址即使能取到，
    // 也证明不了页面里写的那个地址能取到。每个素材还要满足：长缓存头在、内容哈希与地址里的一致。
    const assetNames = ["landing.js", "atmo.svg", "app-icon.png", "installer-icon.png", "file-pdf.png", "file-dmg.png", "file-zip.png",
      "hero-replay-poster.webp", "hero-replay-60fps.webm", "hero-replay-60fps-hevc.mov", "scene-1-switch.webp", "scene-2-alert.webp", "scene-3-fullscreen.webp", "scene-4-hide.webp",
      "scene-4-hide-poster.webp", "scene-4-hide.mp4", "scene-4-hide.webm", "scene-4-hide-1470.mp4", "scene-4-hide-1470.webm",
      ...["scene-1-switch", "scene-2-alert", "scene-3-fullscreen", "scene-4-hide", "scene-4-hide-poster"].flatMap(base => [720, 1280, 2100].map(width => `${base}-${width}.webp`))];
    const referenced = new Map();
    for (const match of pageBody.matchAll(/\/assets\/([A-Za-z0-9._-]+)\?v=([0-9a-f]{10})/g)) referenced.set(match[1], match[2]);
    const assets = await mapLimit(assetNames, READBACK_CONCURRENCY, async name => {
      const stamp = referenced.get(name);
      if (!stamp) return { name, status: 0, bytes: 0, type: "", cache: "", hashOk: false };
      const response = await fetch(`${base}/assets/${name}?v=${stamp}`, { signal: AbortSignal.timeout(READBACK_BODY_TIMEOUT_MS), headers: { "cache-control": "no-cache" } });
      const body = Buffer.from(await response.arrayBuffer());
      // 样式表里的 url(...) 在构建时被改写后才算指纹，所以对样式表也是"取回来的内容哈希 = 地址里的指纹"。
      const hashOk = createHash("sha256").update(body).digest("hex").slice(0, 10) === stamp;
      return { name, status: response.status, bytes: body.byteLength, type: response.headers.get("content-type") || "",
        cache: response.headers.get("cache-control") || "", hashOk, stamp, body: /\.(?:webm|mov|mp4)$/i.test(name) ? body : null };
    });
    // 视频必须支持分段请求：Safari 播 <video> 依赖 206，Pages 的静态层不支持，漏掉就是 Worker 没接管视频。
    // 头两个字节、最后一个字节都和整份文件逐字节对拍，不只看状态码。
    const rangeChecks = await mapLimit(assets.filter(asset => asset.body), READBACK_CONCURRENCY, async asset => {
      const url = `${base}/assets/${asset.name}?v=${asset.stamp}`;
      const ask = async range => {
        const response = await fetch(url, { signal: AbortSignal.timeout(READBACK_BODY_TIMEOUT_MS), headers: { range, "cache-control": "no-cache" } });
        return { response, body: Buffer.from(await response.arrayBuffer()) };
      };
      const last = asset.bytes - 1;
      const head = await ask("bytes=0-1");
      const tail = await ask(`bytes=${last}-`);
      const ok = head.response.status === 206 && head.response.headers.get("content-range") === `bytes 0-1/${asset.bytes}`
        && head.response.headers.get("accept-ranges") === "bytes" && head.body.equals(asset.body.subarray(0, 2))
        && tail.response.status === 206 && tail.response.headers.get("content-range") === `bytes ${last}-${last}/${asset.bytes}`
        && tail.body.equals(asset.body.subarray(last));
      return { name: asset.name, ok, head: head.response.status, tail: tail.response.status };
    });
    // 页面上登记的每一个视频都必须做过分段对拍，数量对不上就是有视频没被读回到。
    const videoCount = assetNames.filter(name => /\.(?:webm|mov|mp4)$/i.test(name)).length;
    const rangeOk = rangeChecks.length === videoCount && rangeChecks.every(check => check.ok);
    // 样式表已内联进页面（省一次往返）：策略头里的哈希必须等于页面上那一段内联样式的 sha256，
    // 且页面上只有这一段内联样式、没有外链样式表、没有 'unsafe-inline'——哈希对不上，浏览器会整段拒绝，页面照样 200 但没有任何样式。
    const inlineStyles = [...pageBody.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(match => match[1]);
    const styleHash = inlineStyles.length === 1 ? "sha256-" + createHash("sha256").update(inlineStyles[0], "utf8").digest("base64") : "";
    const styleSrc = (csp.match(/(?:^|;)\s*style-src([^;]*)/) || [])[1]?.trim() || "";
    const styleCspOk = inlineStyles.length === 1 && !/rel="stylesheet"/.test(pageBody) && styleSrc === `'self' '${styleHash}'`;
    const cssRefsOk = styleCspOk && !/url\(\/assets\/[A-Za-z0-9._-]+\)/.test(inlineStyles[0] || "") && /url\(\/assets\/app-icon\.png\?v=[0-9a-f]{10}\)/.test(inlineStyles[0] || "");
    const expectedTypes = { "landing.js": "javascript", "atmo.svg": "image/svg+xml",
      "app-icon.png": "image/png", "installer-icon.png": "image/png", "file-pdf.png": "image/png", "file-dmg.png": "image/png", "file-zip.png": "image/png",
      "hero-replay-poster.webp": "image/webp", "hero-replay-60fps.webm": "video/webm", "hero-replay-60fps-hevc.mov": "video/quicktime",
      "scene-1-switch.webp": "image/webp", "scene-2-alert.webp": "image/webp", "scene-3-fullscreen.webp": "image/webp", "scene-4-hide.webp": "image/webp",
      "scene-4-hide-poster.webp": "image/webp", "scene-4-hide.mp4": "video/mp4", "scene-4-hide.webm": "video/webm",
      "scene-4-hide-1470.mp4": "video/mp4", "scene-4-hide-1470.webm": "video/webm" };
    for (const name of assetNames) if (/-(?:720|1280|2100)\.webp$/.test(name)) expectedTypes[name] = "image/webp";
    const assetsOk = cssRefsOk && assets.every(asset => asset.status === 200 && asset.bytes > 0 && asset.type.includes(expectedTypes[asset.name])
      && /immutable/.test(asset.cache) && /max-age=\d{7,}/.test(asset.cache) && asset.hashOk);
    const privacyResponse = await fetch(base + "/privacy", { signal: AbortSignal.timeout(10000), headers: { "cache-control": "no-cache" } });
    const privacyBody = await privacyResponse.text();
    // 打开首页与说明页都不许下发 cookie：访客编号只在点下载时才写。
    const noCookieOnOpen = !pageResponse.headers.get("set-cookie") && !privacyResponse.headers.get("set-cookie");
    // 隔离类响应头也要读回：Worker 漏发这些头，页面照样 200，只是少了一层防护。
    const has = (response, name, pattern) => pattern.test(response.headers.get(name) || "");
    const headersOk = [pageResponse, privacyResponse, healthResponse].every(response =>
      has(response, "permissions-policy", /camera=\(\)/) && has(response, "cross-origin-opener-policy", /^same-origin$/)
      && has(response, "cross-origin-resource-policy", /^same-origin$/) && has(response, "strict-transport-security", /max-age=\d{7,}/)
      && has(response, "x-content-type-options", /^nosniff$/))
      && has(pageResponse, "cache-control", /^public, max-age=60, s-maxage=300, stale-while-revalidate=60$/)
      && has(privacyResponse, "cache-control", /^no-cache$/) && has(healthResponse, "cache-control", /^no-store$/);
    const downloadAssets = [
      `Skyrocket-V${version}.dmg`,
      `Skyrocket-${version}-macos.zip`,
      `Skyrocket-${version}-source.zip`,
      `Skyrocket-${version}-SHA256SUMS.txt`,
    ];
    // 逐项验证页面入口和官方发布入口，但一律 HEAD + manual redirect，绝不下载 27 MB DMG。
    // 先查 Pages 的 302，再查 GitHub 是否把同一资产交给临时签名的 release-assets 地址。
    const downloadChecks = await Promise.all(downloadAssets.map(async asset => {
      const entry = await fetch(`${base}/download?version=${encodeURIComponent(version)}&asset=${encodeURIComponent(asset)}`, {
        method: "HEAD", redirect: "manual", signal: AbortSignal.timeout(10000),
      });
      const target = entry.headers.get("location") || "";
      if (entry.status !== 302 || target !== `https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/releases/download/%E7%89%88%E6%9C%AC%E6%9B%B4%E6%96%B0/${encodeURIComponent(asset)}`) {
        return { asset, entry: entry.status, github: 0, ok: false };
      }
      const github = await fetch(target, { method: "HEAD", redirect: "manual", signal: AbortSignal.timeout(10000) });
      const signed = github.headers.get("location") || "";
      return { asset, entry: entry.status, github: github.status,
        ok: github.status === 302 && /^https:\/\/release-assets\.githubusercontent\.com\//.test(signed) };
    }));
    const downloadsOk = downloadChecks.every(check => check.ok);
    const expectedDmg = `Skyrocket-V${version}.dmg`;
    liveOk = pageResponse.ok && healthResponse.ok && healthBody.database === true
      && healthBody.build_version === version && pageBody.includes("下载 Mac 版")
      && pageBody.includes('class="logo"') && pageBody.includes("三步装好")
      && pageBody.includes(expectedDmg) && pageBody.includes(`version=${version}`)
      && pageBody.includes('data-dl href="/download?') && /<script defer src="\/assets\/landing\.js\?v=[0-9a-f]{10}"><\/script>/.test(pageBody)
      && !pageBody.includes('href="#"') && !pageBody.includes("#started") && !pageBody.includes("__CURRENT_")
      && buttonCount >= 3 && assetsOk && rangeOk && privacyResponse.ok && noCookieOnOpen && headersOk && downloadsOk
      && privacyBody.includes("处理者") && privacyBody.includes("保存 30 天") && privacyBody.includes("同一浏览器在 60 秒内重复下载同一文件只记录一次")
      && privacyBody.includes("自动访问不写入统计") && cspOk;
    lastObservation = `page=${pageResponse.status} health=${healthResponse.status} database=${healthBody.database} build=${healthBody.build_version} buttons=${buttonCount} assets=${assets.map(a => `${a.name}:${a.status}/${a.bytes}`).join(",")} downloads=${downloadChecks.map(check => `${check.asset}:${check.entry}/${check.github}`).join(",")} ranges=${rangeChecks.map(c => `${c.name}:${c.head}/${c.tail}`).join(",")} csp=${cspOk}`;
  } catch (error) {
    lastObservation = error?.name || "unknown";
  }
  if (!liveOk) await new Promise(resolve => setTimeout(resolve, 1500));
}
if (!liveOk) throw new Error(`live landing page verification failed: ${lastObservation}`);
console.log(JSON.stringify({
  project: readback.name,
  subdomain: readback.subdomain,
  database_bound: Boolean(readback.deployment_configs?.production?.d1_databases?.DB),
  check_token_configured: Boolean(readback.deployment_configs?.production?.env_vars?.CHECK_TOKEN),
  deployment_id: readback.canonical_deployment?.id,
  live_version: version,
  live_verified: true,
}));
