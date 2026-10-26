import { test } from "node:test";
import assert from "node:assert/strict";
import worker, {
  allowedAsset, cachedLatestVersion, dayKey, downloadPath, latestVersion, page, releaseUrl,
} from "../src/index.mjs";

class FakeDB {
  constructor() { this.inserted = []; this.deleted = 0; this.failInsert = false; this.expired = 1; }
  prepare(sql) {
    const statement = {
      params: [],
      bind: (...values) => { statement.params = values; return statement; },
      run: async () => {
        if (sql.startsWith("INSERT INTO download_events")) {
          if (this.failInsert) throw new Error("write_failed");
          this.inserted.push(statement.params);
          return { meta: { changes: 1 } };
        }
        if (sql.startsWith("DELETE FROM download_events")) {
          const changes = Math.min(this.expired, 500);
          this.expired -= changes;
          this.deleted += changes;
          return { meta: { changes } };
        }
        return { meta: { changes: 1 } };
      },
      first: async () => sql.includes("sqlite_master") ? { name: "download_events" }
        : sql.includes("SELECT 1") ? { ok: 1 } : { downloads: 3, visitors: 2 },
      all: async () => ({ results: [{ day: "2026-10-24", route: "public", version: "1.4.104",
        asset: "Skyrocket-V1.4.104.dmg", downloads: 3, visitors: 2 }] }),
    };
    return statement;
  }
}

const env = token => ({ DB: new FakeDB(), CHECK_TOKEN: token });

function request(path, options = {}) {
  return new Request("https://downloads.example" + path, options);
}

test("only the four shipped asset names for the stated version are allowed", () => {
  assert.equal(allowedAsset("1.4.104", "Skyrocket-1.4.104-macos.zip"), true);
  assert.equal(allowedAsset("1.4.104", "Skyrocket-1.4.104-source.zip"), true);
  assert.equal(allowedAsset("1.4.104", "Skyrocket-1.4.104-SHA256SUMS.txt"), true);
  assert.equal(allowedAsset("1.4.104", "Skyrocket-V1.4.104.dmg"), true);
  assert.equal(allowedAsset("1.4.104", "Skyrocket-1.4.103-macos.zip"), false);
  assert.equal(allowedAsset("1.4.104", "Settings.json"), false);
  assert.equal(allowedAsset("../1.4.104", "Skyrocket-1.4.104-macos.zip"), false);
});

test("the redirect target is always the official release host", () => {
  assert.equal(
    releaseUrl("1.4.104", "Skyrocket-1.4.104-macos.zip"),
    "https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/releases/download/%E7%89%88%E6%9C%AC%E6%9B%B4%E6%96%B0/Skyrocket-1.4.104-macos.zip",
  );
});

test("the landing page offers real download actions for the deployed version", () => {
  assert.equal(downloadPath("1.4.104", "Skyrocket-V1.4.104.dmg"),
    "/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg");
  const body = page("1.4.104");
  assert.match(body, /class="version">v1\.4\.104</);
  assert.match(body, /<h1>Skyrocket<\/h1>/);
  assert.match(body, /下载 macOS 版/);
  assert.match(body, /下载 DMG/);
  assert.match(body, /href="\/download\?version=1\.4\.104&amp;asset=Skyrocket-V1\.4\.104\.dmg"/);
  assert.match(body, /href="\/download\?version=1\.4\.104&amp;asset=Skyrocket-1\.4\.104-macos\.zip"/);
  assert.match(body, /href="\/download\?version=1\.4\.104&amp;asset=Skyrocket-1\.4\.104-SHA256SUMS\.txt"/);
  assert.match(body, /保存 30 天；到期后在访问或每日清理时删除/);
  assert.match(body, /GitHub 发布页/);
});

test("the landing page shows the application icon once the build injects it", () => {
  const body = page("1.4.104", { light: "data:image/png;base64,QUJDRA==", dark: "data:image/png;base64,REVG" });
  assert.match(body, /<link rel="icon" type="image\/png" href="data:image\/png;base64,QUJDRA==">/);
  assert.match(body, /<div class="logo" role="img" aria-label="Skyrocket 图标"><\/div>/);
  assert.match(body, /\.logo\{width:56px;height:56px;flex:none;background:url\(data:image\/png;base64,QUJDRA==\)/);
  assert.match(body, /\.logo\{background-image:url\(data:image\/png;base64,REVG\)\}/);
});

test("an un-injected build leaves no placeholder text or empty logo box", () => {
  const body = page("1.4.104");
  assert.doesNotMatch(body, /__ICON_/);
  assert.doesNotMatch(body, /class="logo"/);
  assert.match(body, /<h1>Skyrocket<\/h1>/);
});

test("an uninjected version does not emit broken download links", () => {
  const body = page("__CURRENT_VERSION__");
  assert.doesNotMatch(body, /href="\/download/);
  assert.match(body, /当前版本信息暂未生成/);
  assert.match(body, /GitHub 发布页/);
});

test("invalid download links explain the problem instead of dumping JSON", async () => {
  const context = env("secret");
  const response = await worker.fetch(request("/download?version=1.4.104&asset=other.zip"), context);
  assert.equal(response.status, 400);
  assert.match(response.headers.get("content-type"), /text\/html/);
  assert.match(await response.text(), /返回下载页/);
});

test("html and json responses carry browser security headers", async () => {
  const context = env("secret");
  for (const response of [await worker.fetch(request("/"), context),
                          await worker.fetch(request("/health"), context),
                          await worker.fetch(request("/stats"), context)]) {
    assert.equal(response.headers.get("x-content-type-options"), "nosniff");
    assert.equal(response.headers.get("referrer-policy"), "no-referrer");
    assert.equal(response.headers.get("x-frame-options"), "DENY");
  }
  // 图标是 data URI，CSP 不放行 img-src data: 的话浏览器会静默不画（页面仍是 200）。
  const policy = (await worker.fetch(request("/"), context)).headers.get("content-security-policy");
  assert.match(policy, /img-src data:/);
  assert.match(policy, /default-src 'none'/);
});

test("the landing page follows the published manifest and falls back safely", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ version: "1.4.105" }) });
    assert.equal(await latestVersion("1.4.104"), "1.4.105");
    globalThis.fetch = async () => ({ ok: false });
    assert.equal(await latestVersion("1.4.104"), "1.4.104");
    globalThis.fetch = async () => { throw new Error("offline"); };
    assert.equal(await latestVersion("1.4.104"), "1.4.104");
    assert.equal(await latestVersion("__CURRENT_VERSION__"), "__CURRENT_VERSION__");
  } finally {
    globalThis.fetch = original;
  }
});

test("a cold or warm version cache never blocks the landing page", async () => {
  const originalCaches = globalThis.caches;
  try {
    delete globalThis.caches;
    assert.equal(await cachedLatestVersion("1.4.104", { waitUntil: () => {} }), "1.4.104");
    globalThis.caches = {
      default: {
        match: async () => ({ json: async () => ({ version: "1.4.105" }) }),
        put: async () => {},
      },
    };
    assert.equal(await cachedLatestVersion("1.4.104"), "1.4.105");
  } finally {
    if (originalCaches === undefined) delete globalThis.caches;
    else globalThis.caches = originalCaches;
  }
});

test("health distinguishes a reachable worker from a provisioned download database", async () => {
  const context = env("secret");
  const healthy = await worker.fetch(request("/health"), context);
  assert.equal(healthy.status, 200);
  const healthyBody = await healthy.json();
  assert.equal(healthyBody.database, true);
  assert.equal(healthyBody.build_version, "__CURRENT_VERSION__");

  context.DB.prepare = () => ({ first: async () => ({ name: "other_table" }) });
  const missingTable = await worker.fetch(request("/health"), context);
  assert.equal((await missingTable.json()).database, false);

  context.DB.prepare = () => ({ bind: () => ({ first: async () => { throw new Error("no_db"); } }) });
  const unavailable = await worker.fetch(request("/health"), context);
  assert.equal((await unavailable.json()).database, false);
});

test("HEAD requests to the landing page and health do not look like 404s", async () => {
  const context = env("secret");
  assert.equal((await worker.fetch(request("/", { method: "HEAD" }), context)).status, 200);
  assert.equal((await worker.fetch(request("/health", { method: "HEAD" }), context)).status, 200);
});

test("robots and favicon requests do not become visible 404 noise", async () => {
  const context = env("secret");
  const robots = await worker.fetch(request("/robots.txt"), context);
  assert.equal(robots.status, 200);
  assert.match(await robots.text(), /Disallow: \//);
  assert.equal((await worker.fetch(request("/favicon.ico"), context)).status, 204);
});

test("a public download is recorded once and receives a 30-day cookie", async () => {
  const context = env("secret");
  const response = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip"), context);
  assert.equal(response.status, 302);
  assert.match(response.headers.get("location"),
    /^https:\/\/github\.com\/[^/]+\/StockFloat-TargetPrice-Releases\/releases\/download\//);
  assert.match(response.headers.get("set-cookie"), /Max-Age=2592000/);
  assert.equal(context.DB.inserted.length, 1);
  assert.equal(context.DB.inserted[0][4], "public");
});

test("a returning anonymous visitor keeps the same identifier and gets no new cookie", async () => {
  const context = env("secret");
  const first = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip"), context);
  const cookie = first.headers.get("set-cookie").split(";")[0];
  const second = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg", {
    headers: { cookie },
  }), context);
  assert.equal(second.status, 302);
  assert.equal(second.headers.get("set-cookie"), null);
  assert.equal(context.DB.inserted[0][7], context.DB.inserted[1][7]);
});

test("release checks require the configured token and cannot be forged by a query parameter", async () => {
  const context = env("secret");
  const denied = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip&route=release-check"), context);
  assert.equal(denied.status, 403);
  assert.equal(context.DB.inserted.length, 0);

  const accepted = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip&route=release-check", {
    headers: { "x-skyrocket-check": "secret" },
  }), context);
  assert.equal(accepted.status, 302);
  assert.equal(context.DB.inserted[0][4], "release-check");
});

test("a missing server-side token fails loudly for release checks but not for public downloads", async () => {
  const context = env("");
  const check = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip&route=release-check"), context);
  assert.equal(check.status, 503);
  const publicResponse = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip"), context);
  assert.equal(publicResponse.status, 302);
});

test("the owner cookie keeps manual clicks out of the public count", async () => {
  const hex = "a".repeat(64);
  const context = env(hex);
  // 首页链接不带 route，机主点下去也必须算内部通路（上一轮就是漏在这里）。
  const mine = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg", {
    headers: { cookie: `skyrocket_check=${hex}` },
  }), context);
  assert.equal(mine.status, 302);
  assert.equal(context.DB.inserted[0][4], "release-check");
  const headerOnly = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip", {
    headers: { "x-skyrocket-check": hex },
  }), context);
  assert.equal(headerOnly.status, 302);
  assert.equal(context.DB.inserted[1][4], "release-check");
  const visitor = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-SHA256SUMS.txt"), context);
  assert.equal(visitor.status, 302);
  assert.equal(context.DB.inserted[2][4], "public");
  const forged = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg", {
    headers: { cookie: "skyrocket_check=" + "b".repeat(64) },
  }), context);
  assert.equal(forged.status, 302);
  assert.equal(context.DB.inserted[3][4], "public");
});

test("HEAD responses never carry a body, including the error pages", async () => {
  const context = env("secret");
  for (const path of ["/", "/health", "/download?version=1.4.104&asset=other.zip",
                      "/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg&route=release-check"]) {
    const response = await worker.fetch(request(path, { method: "HEAD" }), context);
    assert.equal(response.body, null, `${path} 的 HEAD 带了正文`);
  }
  assert.equal(context.DB.inserted.length, 0);
});

test("the stats window cannot exceed the retention period", async () => {
  const context = env("secret");
  const response = await worker.fetch(request("/stats?days=90", {
    headers: { authorization: "Basic " + btoa("owner:secret") },
  }), context);
  const body = await response.text();
  assert.match(body, /近 30 天/);
  assert.doesNotMatch(body, /近 90 天/);
});

test("the owner route needs the same credential and hands back the cookie", async () => {
  const context = env("secret");
  assert.equal((await worker.fetch(request("/owner"), context)).status, 401);
  const granted = await worker.fetch(request("/owner", {
    headers: { authorization: "Basic " + btoa("owner:secret") },
  }), context);
  assert.equal(granted.status, 302);
  assert.equal(granted.headers.get("location"), "/");
  assert.match(granted.headers.get("set-cookie"), /skyrocket_check=secret;.*HttpOnly/);
});

test("invalid input and unsupported methods are rejected without redirecting", async () => {
  const context = env("secret");
  assert.equal((await worker.fetch(request("/download?version=1.4.104&asset=anything.zip"), context)).status, 400);
  assert.equal((await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip&route=other"), context)).status, 400);
  assert.equal((await worker.fetch(request("/download", { method: "POST" }), context)).status, 405);
});

test("a HEAD probe gets the redirect without creating a download event", async () => {
  const context = env("secret");
  const response = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip", { method: "HEAD" }), context);
  assert.equal(response.status, 302);
  assert.match(response.headers.get("location"), /^https:\/\/github\.com\//);
  assert.equal(context.DB.inserted.length, 0);
});

test("a database failure never blocks the official download", async () => {
  const context = env("secret");
  context.DB.failInsert = true;
  const response = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip"), context);
  assert.equal(response.status, 302);
  assert.match(response.headers.get("location"), /^https:\/\/github\.com\//);
});

test("the landing page establishes the anonymous id before any download", async () => {
  const context = env("secret");
  const first = await worker.fetch(request("/"), context);
  assert.match(first.headers.get("set-cookie"), /skyrocket_download_id=[A-Za-z0-9-]{36}/);
  assert.equal(context.DB.inserted.length, 0);
  const cookie = first.headers.get("set-cookie").split(";")[0];
  const second = await worker.fetch(request("/", { headers: { cookie } }), context);
  assert.equal(second.headers.get("set-cookie"), null);
});

test("statistics days follow the Beijing calendar", () => {
  assert.equal(dayKey(new Date("2026-10-24T15:59:59Z")), "2026-10-24");
  assert.equal(dayKey(new Date("2026-10-24T16:00:00Z")), "2026-10-25");
});

test("statistics require owner authentication", async () => {
  const context = env("secret");
  assert.equal((await worker.fetch(request("/stats"), context)).status, 401);
  assert.equal((await worker.fetch(request("/stats", { headers: { authorization: "Basic " + btoa("owner:wrong") } }), context)).status, 401);
  const response = await worker.fetch(request("/stats", { headers: { authorization: "Basic " + btoa("owner:secret") } }), context);
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.match(body, /近 30 天/);
  assert.match(body, /Skyrocket-V1\.4\.104\.dmg/);
  assert.match(body, /日期按北京时间/);
});

test("the scheduled cleanup deletes expired rows", async () => {
  const context = env("secret");
  let waited = null;
  await worker.scheduled({}, context, { waitUntil: promise => { waited = promise; } });
  await waited;
  assert.equal(context.DB.deleted, 1);
});
