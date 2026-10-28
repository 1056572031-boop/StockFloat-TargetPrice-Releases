import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import worker, {
  allowedAsset, cachedLatestVersion, compareVersions, dayKey, dmgBytesFor, downloadEventId, downloadPath, isAutomatedRequest,
  landingCacheControl, latestVersion, page, parseRange, releaseUrl, videoTypeFor,
} from "../src/index.mjs";

class FakeDB {
  constructor() { this.inserted = []; this.insertedIds = new Set(); this.deleted = 0; this.failInsert = false; this.expired = 1; }
  prepare(sql) {
    const statement = {
      params: [],
      bind: (...values) => { statement.params = values; return statement; },
      run: async () => {
        if (sql.startsWith("INSERT")) {
          if (this.failInsert) throw new Error("write_failed");
          const duplicate = this.insertedIds.has(statement.params[0]);
          if (!duplicate) {
            this.insertedIds.add(statement.params[0]);
            this.inserted.push(statement.params);
          }
          return { meta: { changes: duplicate ? 0 : 1 } };
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
  const body = page("1.4.104", 27_472_418);
  assert.match(body, /<h1>把股价放在桌面一角<em>到价就提醒你<\/em><\/h1>/);
  assert.match(body, /下载 Mac 版/);
  // 主按钮是一条真链接：没有脚本时也能下载；脚本只负责切"已开始下载"的状态。
  assert.match(body, /<a class="cta" id="cta" data-dl href="\/download\?version=1\.4\.104&amp;asset=Skyrocket-V1\.4\.104\.dmg">/);
  assert.match(body, /href="\/download\?version=1\.4\.104&amp;asset=Skyrocket-1\.4\.104-macos\.zip"/);
  assert.match(body, /href="\/download\?version=1\.4\.104&amp;asset=Skyrocket-1\.4\.104-SHA256SUMS\.txt"/);
  assert.match(body, /<span class="i">1\.4\.104 约 27 MB<\/span>/);
  assert.match(body, /Skyrocket-V1\.4\.104\.dmg<\/span>/, "第 1 步示意图里的文件名要跟着版本走");
  assert.match(body, /releases\/tag\//, "备用下载页指向 GitHub 发布页");
  assert.doesNotMatch(body, /href="#"|#started|__[A-Z_]+__/, "不许留空链接、演示用锚点或未替换的占位符");
});

test("the displayed size is rounded from bytes and simply omitted when unknown", () => {
  assert.match(page("1.4.104", 27_472_418), /1\.4\.104 约 27 MB</);
  assert.match(page("1.4.104", 400_000), /1\.4\.104 约 1 MB</, "不足 1 MB 也不能显示成 0 MB");
  for (const unknown of [null, undefined, 0, -5, "__CURRENT_DMG_BYTES__"]) {
    const body = page("1.4.104", unknown);
    assert.match(body, /<span class="i">1\.4\.104<\/span>/, `大小=${String(unknown)} 时只显示版本号`);
    assert.doesNotMatch(body, /MB</);
  }
});

test("the landing page loads only same-origin scripts and every asset it names exists", async () => {
  const body = page("1.4.104", 27_472_418);
  assert.doesNotMatch(body, /<script(?![^>]*\ssrc=)[^>]*>/, "CSP 的 script-src 只放同源，页面里不能有内联脚本");
  assert.doesNotMatch(body, /\son[a-z]+=/i, "不能有内联事件处理属性");
  const { existsSync } = await import("node:fs");
  const generatedAtBuild = new Set(["/assets/app-icon.png"]);
  const referenced = [...new Set(body.match(/\/assets\/[A-Za-z0-9._-]+/g) || [])];
  assert.ok(referenced.length >= 5, "至少引用样式、脚本、图标、背景与安装图标");
  for (const path of referenced) {
    if (generatedAtBuild.has(path)) continue;
    assert.ok(existsSync(new URL(`../public${path}`, import.meta.url)), `${path} 不在 public/assets 里，上线会 404`);
  }
  for (const path of ["landing.css", "landing.js"]) {
    assert.ok(referenced.includes(`/assets/${path}`), `页面没有引用 ${path}`);
  }
});

test("the application icon is a same-origin file shared by favicon, header and install steps", () => {
  const body = page("1.4.104", 27_472_418);
  assert.match(body, /<link rel="icon" type="image\/png" href="\/assets\/app-icon\.png">/);
  assert.match(body, /<div class="logo" role="img" aria-label="Skyrocket 图标"><\/div>/);
  assert.match(body, /<span class="appic"><\/span>/, "第 2 步的程序图标与页头、标签页图标用同一份文件");
  // 唯一允许的内嵌图是懒加载用的 1x1 透明占位图（37 字节、整串精确匹配）；任何别的 data URI 仍然不许出现。
  const placeholder = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
  assert.doesNotMatch(body.replaceAll(placeholder, ""), /data:image\//i, "页面本身不再内嵌 data URI（1x1 占位图除外，大小写都不行）");
  // 占位图恰好四处（场景 1–3 与第 4 张的静止图），而且都在 <img src="…"> 里，不能出现在样式或别的属性里。
  assert.equal(body.split(placeholder).length - 1, 4);
  assert.equal((body.match(new RegExp(`<img class="lz"[^>]* src="${placeholder.replace(/[+/=]/g, "\\$&")}"`, "g")) || []).length, 4);
});

test("an uninjected version does not emit broken download links", () => {
  const body = page("__CURRENT_VERSION__", null);
  assert.doesNotMatch(body, /href="\/download/);
  assert.doesNotMatch(body, /data-dl/, "没有真实下载链接时，不能挂'已开始下载'的状态切换");
  assert.match(body, /前往发布页/);
  assert.match(body, /当前版本准备中/);
  assert.match(body, /releases\/tag\//);
  assert.match(body, /Skyrocket\.dmg<\/span>/, "没有版本号时示意图只写通用文件名");
  assert.doesNotMatch(body, /压缩包版本|校验文件/, "没有版本号就没有可核对的压缩包和校验文件，不能指向发布页冒充");
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
  // 样式表、脚本、图片都是同源静态文件：CSP 不放行 'self' 时浏览器会静默不加载（页面仍是 200，
  // 却没有样式和交互）。脚本只放同源，绝不放 'unsafe-inline'，这是这条策略真正收紧的地方。
  const policy = (await worker.fetch(request("/"), context)).headers.get("content-security-policy");
  assert.match(policy, /default-src 'none'/);
  assert.match(policy, /img-src 'self' data:/);
  assert.match(policy, /media-src 'self'(;|$)/, "首屏视频是同源文件：不放 media-src 时 default-src 'none' 会让它静默不加载");
  assert.doesNotMatch(policy.match(/media-src[^;]*/)[0], /\*|https?:|data:/, "视频只许同源");
  assert.match(policy, /style-src 'self'/);
  assert.match(policy, /script-src 'self'(;|$)/);
  assert.doesNotMatch(policy.match(/script-src[^;]*/)[0], /unsafe-inline|unsafe-eval|\*/);
});

test("static assets fall back to the ASSETS binding when the Pages route exclusion does not apply", async () => {
  const seen = [];
  const context = { ...env("secret"), ASSETS: { fetch: async req => { seen.push(new URL(req.url).pathname); return new Response("ok", { status: 200 }); } } };
  const hit = await worker.fetch(request("/assets/landing.css"), context);
  assert.equal(hit.status, 200);
  assert.deepEqual(seen, ["/assets/landing.css"]);
  const bare = await worker.fetch(request("/assets/landing.css"), env("secret"));
  assert.equal(bare.status, 404, "没有 ASSETS 绑定时仍是 404，而不是抛异常");
  const other = await worker.fetch(request("/not-assets/x"), context);
  assert.equal(other.status, 404);
  assert.equal(seen.length, 1, "只有 /assets/ 前缀才交给静态资产");
});

// ---- 视频分段请求：Pages 静态层不支持 Range，Safari 播 <video> 依赖 206，所以视频由 Worker 切片 ----
const CLIP = Uint8Array.from({ length: 100 }, (_, index) => index);

function videoContext({ etag = '"clip-v1"', missing = false } = {}) {
  const seen = [];
  return { ...env("secret"), seen, ASSETS: { fetch: async req => {
    seen.push({ method: req.method, range: req.headers.get("range"), ifNoneMatch: req.headers.get("if-none-match"), path: new URL(req.url).pathname });
    return missing ? new Response("nope", { status: 404 })
      : new Response(CLIP, { status: 200, headers: { "content-type": "application/octet-stream", ...(etag ? { etag } : {}) } });
  } } };
}

const bytesOf = async response => [...new Uint8Array(await response.arrayBuffer())];

test("a Range header is parsed to a single inclusive byte span, and anything it cannot honour is ignored or rejected as the RFC says", () => {
  const span = (header, size = 100) => parseRange(header, size);
  assert.deepEqual({ ...span("bytes=0-9") }, { start: 0, end: 9 });
  assert.deepEqual({ ...span("bytes=20-29") }, { start: 20, end: 29 });
  assert.deepEqual({ ...span("bytes=90-") }, { start: 90, end: 99 }, "开区间读到末尾");
  assert.deepEqual({ ...span("bytes=-10") }, { start: 90, end: 99 }, "后缀区间是最后 10 个字节");
  assert.deepEqual({ ...span("bytes=-500") }, { start: 0, end: 99 }, "后缀比文件还长就整份");
  assert.deepEqual({ ...span("bytes=0-999") }, { start: 0, end: 99 }, "结尾超出就截到文件末尾");
  assert.deepEqual({ ...span("bytes=99-99") }, { start: 99, end: 99 }, "最后一个字节");
  assert.equal(span("bytes=100-"), "unsatisfiable", "起点已在文件之外");
  assert.equal(span("bytes=-0"), "unsatisfiable");
  for (const ignored of [undefined, null, "", "bytes=-", "bytes=5-2", "bytes=0-1,5-9", "items=0-1", "bytes=a-b", "0-9"]) {
    assert.equal(span(ignored), null, `${String(ignored)} 应当忽略 Range、整份回 200`);
  }
  assert.equal(parseRange("bytes=0-9", 0), null, "空文件没有可切的区间");
});

test("only video extensions are served through the range-aware path, with fixed content types", () => {
  assert.equal(videoTypeFor("/assets/clip.webm"), "video/webm");
  assert.equal(videoTypeFor("/assets/clip.MOV"), "video/quicktime");
  assert.equal(videoTypeFor("/assets/clip.mp4"), "video/mp4");
  for (const other of ["/assets/landing.css", "/assets/poster.webp", "/assets/webm", "/assets/clip.webm.png", "/"]) {
    assert.equal(videoTypeFor(other), "", `${other} 不是视频`);
  }
});

test("a byte-range request for a video gets 206 with the exact slice, and the headers Safari needs", async () => {
  const context = videoContext();
  const response = await worker.fetch(request("/assets/clip.webm?v=0123456789", { headers: { range: "bytes=20-29" } }), context);
  assert.equal(response.status, 206);
  assert.equal(response.headers.get("content-range"), "bytes 20-29/100");
  assert.equal(response.headers.get("content-length"), "10");
  assert.equal(response.headers.get("accept-ranges"), "bytes");
  assert.equal(response.headers.get("content-type"), "video/webm");
  assert.match(response.headers.get("cache-control"), /immutable/);
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.deepEqual(await bytesOf(response), [20, 21, 22, 23, 24, 25, 26, 27, 28, 29], "切片的首尾字节都不能偏一位");
  assert.deepEqual(context.seen, [{ method: "GET", range: null, ifNoneMatch: null, path: "/assets/clip.webm" }],
    "向静态层要的是不带 Range 的完整文件，否则它会回半截或 304，Worker 就没法切");
});

test("open-ended and suffix ranges, the way Safari probes a video, return the right tail", async () => {
  const tail = await worker.fetch(request("/assets/clip.mov", { headers: { range: "bytes=95-" } }), videoContext());
  assert.equal(tail.status, 206);
  assert.equal(tail.headers.get("content-range"), "bytes 95-99/100");
  assert.equal(tail.headers.get("content-type"), "video/quicktime");
  assert.deepEqual(await bytesOf(tail), [95, 96, 97, 98, 99]);
  const probe = await worker.fetch(request("/assets/clip.mov", { headers: { range: "bytes=0-1" } }), videoContext());
  assert.equal(probe.status, 206, "Safari 先用 bytes=0-1 探一下是否支持分段");
  assert.equal(probe.headers.get("content-range"), "bytes 0-1/100");
  const suffix = await worker.fetch(request("/assets/clip.mov", { headers: { range: "bytes=-3" } }), videoContext());
  assert.equal(suffix.headers.get("content-range"), "bytes 97-99/100");
  assert.deepEqual(await bytesOf(suffix), [97, 98, 99]);
});

test("without a Range header the whole video is returned with 200 and advertises range support", async () => {
  const response = await worker.fetch(request("/assets/clip.webm"), videoContext());
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("accept-ranges"), "bytes", "没带 Range 时也要声明支持，浏览器才会改用分段请求");
  assert.equal(response.headers.get("content-length"), "100");
  assert.equal(response.headers.get("content-range"), null);
  assert.equal((await bytesOf(response)).length, 100);
});

test("an unsatisfiable range is 416 with the size, and an unparseable one falls back to the whole file", async () => {
  const beyond = await worker.fetch(request("/assets/clip.webm", { headers: { range: "bytes=500-" } }), videoContext());
  assert.equal(beyond.status, 416);
  assert.equal(beyond.headers.get("content-range"), "bytes */100");
  assert.deepEqual(await bytesOf(beyond), []);
  for (const odd of ["bytes=0-1,5-9", "bytes=9-2", "garbage"]) {
    const response = await worker.fetch(request("/assets/clip.webm", { headers: { range: odd } }), videoContext());
    assert.equal(response.status, 200, `${odd} 不认识就整份回 200`);
    assert.equal((await bytesOf(response)).length, 100);
  }
});

test("HEAD on a video reports the size and range support without a body", async () => {
  const response = await worker.fetch(request("/assets/clip.webm", { method: "HEAD" }), videoContext());
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-length"), "100");
  assert.equal(response.headers.get("accept-ranges"), "bytes");
  assert.equal(response.body, null);
});

test("If-Range only keeps the partial answer when it equals the strong ETag, otherwise the whole file is sent", async () => {
  const same = await worker.fetch(request("/assets/clip.webm", { headers: { range: "bytes=0-9", "if-range": '"clip-v1"' } }), videoContext());
  assert.equal(same.status, 206);
  assert.equal(same.headers.get("etag"), '"clip-v1"');
  const stale = await worker.fetch(request("/assets/clip.webm", { headers: { range: "bytes=0-9", "if-range": '"clip-v0"' } }), videoContext());
  assert.equal(stale.status, 200, "文件已经换了就不能拼半截旧的");
  assert.equal((await bytesOf(stale)).length, 100);
  const noEtag = await worker.fetch(request("/assets/clip.webm", { headers: { range: "bytes=0-9", "if-range": '"clip-v1"' } }), videoContext({ etag: "" }));
  assert.equal(noEtag.status, 200, "静态层没给 ETag 就没法比对，安全的一侧是整份");
  const weak = await worker.fetch(request("/assets/clip.webm", { headers: { range: "bytes=0-9", "if-range": 'W/"x"' } }), videoContext({ etag: 'W/"x"' }));
  assert.equal(weak.status, 200, "If-Range 不接受弱校验");
});

test("a missing video is 404 and writes are still refused", async () => {
  const gone = await worker.fetch(request("/assets/missing.webm", { headers: { range: "bytes=0-9" } }), videoContext({ missing: true }));
  assert.equal(gone.status, 404);
  const post = await worker.fetch(request("/assets/clip.webm", { method: "POST" }), videoContext());
  assert.equal(post.status, 405);
});

test("the page keeps working when no static binding exists, and non-video assets still go straight to the binding untouched", async () => {
  assert.equal((await worker.fetch(request("/assets/clip.webm"), env("secret"))).status, 404, "没有 ASSETS 绑定时不抛异常");
  const seen = [];
  const context = { ...env("secret"), ASSETS: { fetch: async req => { seen.push(req.headers.get("range")); return new Response("css", { status: 200 }); } } };
  const css = await worker.fetch(request("/assets/landing.css", { headers: { range: "bytes=0-1" } }), context);
  assert.equal(css.status, 200);
  assert.deepEqual(seen, ["bytes=0-1"], "非视频原样转交，不替它改请求");
});

// 页面上不出现真实股票名称和代码：显示的是占位名称 股票名称 和代码 038492。
// 文字层这里守；烧进视频、封面图和四张场景图里的像素没法在 node 里读，由发布前用系统文字识别逐张核对（见 ASSETS.md）。
test("the page text and every asset file name carry the placeholder name and code, never the real stock", async () => {
  const body = page("1.4.104", 27_472_418);
  assert.doesNotMatch(body, /茅台|贵州|600519|moutai/i, "页面文字、alt、aria-label 里不能出现真实名称和代码");
  assert.match(body, /aria-label="[^"]*股票名称 038492[^"]*"/, "视频说明里写占位名称和代码");
  assert.match(body, /alt="[^"]*股票名称 038492[^"]*"/, "静止图说明里写占位名称和代码");
  assert.match(body, /alt="股票名称到价/, "第二张场景图说明用占位名称");
  const { readdirSync } = await import("node:fs");
  const names = readdirSync(fileURLToPath(new URL("../public/assets/", import.meta.url)));
  assert.ok(names.length >= 10, "素材目录不应是空的，否则下面的检查是空转");
  for (const name of names) assert.doesNotMatch(name, /moutai|maotai|600519/i, `${name} 的文件名里不能带真实股票`);
  const { videoTypeFor: kind } = await import("../src/index.mjs");
  assert.ok(names.some(name => kind(`/assets/${name}`)), "视频文件在目录里，名称检查才有对象");
});

// 程序坞里不再有苹果应用图标：三张带程序坞的场景图（1、2、4）里是自绘淡色纯色块加 Skyrocket 自己的图标。
// 像素没法在 node 里读，这里守登记的说法；像素本身在替换前后用系统文字识别和轮廓对拍核对过。
test("the register says the dock icons are pale flat blocks plus the product's own icon, for every scene that has a dock", async () => {
  const { readFileSync } = await import("node:fs");
  const register = readFileSync(fileURLToPath(new URL("../ASSETS.md", import.meta.url)), "utf8");
  const row = name => register.split("\n").find(line => line.startsWith(`| \`${name}\` |`)) || "";
  for (const name of ["scene-1-switch.webp", "scene-2-alert.webp", "scene-4-hide.webp"]) {
    assert.match(row(name), /淡色纯色块[\s\S]*icon\.png/, `${name} 的登记行必须写明程序坞图标是自绘色块加 Skyrocket 自己的图标`);
  }
  assert.doesNotMatch(row("scene-3-fullscreen.webp"), /程序坞/, "全屏那张本来就没有程序坞");
  assert.match(register, /程序坞里的系统应用图标已在 2026-10-28 换成淡色纯色块和 Skyrocket 自己的图标，不再含苹果图标/, "未授权清单里要写明程序坞图标已换掉，只剩玻璃底座");
  const readme = readFileSync(fileURLToPath(new URL("../README.md", import.meta.url)), "utf8");
  assert.match(readme, /pale flat blocks[\s\S]*no Apple app icons/, "README 要同步");
});

// 四张场景图的桌面背景是 2026-10-28 用户选定的 CC0 照片：每张都要登记标题、作者、许可和文件页，
// 并且不能再把壁纸算成苹果素材。像素没法在 node 里读，这里守登记；壁纸与成品的对应关系在换图时按文件哈希核对过。
test("the register names the four CC0 wallpapers with title, author, licence and page, and no longer calls the wallpapers Apple's", async () => {
  const { readFileSync } = await import("node:fs");
  const register = readFileSync(fileURLToPath(new URL("../ASSETS.md", import.meta.url)), "utf8");
  const section = register.split("## 壁纸来源")[1] || "";
  assert.ok(section.length > 0, "必须有“壁纸来源”一节");
  const rows = section.split("\n").filter(line => line.startsWith("| `scene-"));
  assert.equal(rows.length, 4, "四张场景图各有一行壁纸来源");
  for (const line of rows) {
    assert.match(line, /CC0/, `壁纸来源行必须写明许可：${line.slice(0, 30)}`);
    assert.match(line, /https:\/\/commons\.wikimedia\.org\/wiki\/File:/, "必须给出 Commons 文件页");
    assert.match(line, /\.jpg/, "必须写明原文件名");
  }
  assert.equal(new Set(rows.map(line => line.split("|")[3].trim())).size, 4, "四张用的壁纸互不相同");
  assert.match(section, /没有回到原站[\s\S]*再核对/, "必须如实写明许可没有回到原站核对");
  const unlicensed = register.split("## 未取得授权的素材")[1].split("## 壁纸来源")[0];
  assert.doesNotMatch(unlicensed, /桌面壁纸、菜单栏/, "壁纸已不是苹果的，不该再和菜单栏并列算苹果素材");
  assert.doesNotMatch(register, /Mac Blue/, "第四张已不用苹果 Mac Blue 壁纸");
  const readme = readFileSync(fileURLToPath(new URL("../README.md", import.meta.url)), "utf8");
  assert.match(readme, /four CC0 wallpaper photos[\s\S]*Sintel[\s\S]*is gone/, "README 要同步");
});

// 第二张的通知横幅是白字压在系统玻璃上，亮色壁纸下玻璃偏亮，文字对比度只有 4.4:1。
// 现在垫了一层黑色衬底。像素没法在 node 里读，这里守登记；7.6:1 是对着无文字的同一张衬底图量出来的。
test("the register says the alert banner has a dark backing layer and states the measured contrast", async () => {
  const { readFileSync } = await import("node:fs");
  const register = readFileSync(fileURLToPath(new URL("../ASSETS.md", import.meta.url)), "utf8");
  const row = register.split("\n").find(line => line.startsWith("| `scene-2-alert.webp` |")) || "";
  assert.match(row, /黑色衬底/, "第二张必须写明横幅垫了衬底");
  assert.match(row, /7\.6:1/, "必须写明量出来的对比度");
  assert.match(row, /4\.4:1/, "必须写明垫衬底前的对比度，数字才有参照");
  assert.match(row, /横幅以外的像素没有改动/, "必须写明只动了横幅");
});

test("the page has a skip link, one main landmark and a footer landmark, in that order", () => {
  const body = page("1.4.104", 27_472_418);
  assert.match(body, /<body><a class="skip" href="#main">跳到主要内容<\/a>/, "跳转链接必须是 body 里的第一个可聚焦元素");
  assert.equal((body.match(/<main[\s>]/g) || []).length, 1, "只能有一个 main");
  assert.match(body, /<main id="main" tabindex="-1"><section class="hero">/, "main 要能接收焦点，跳转后键盘从这里继续");
  assert.ok(body.indexOf("</header>") < body.indexOf("<main"), "页头在 main 之外");
  assert.ok(body.indexOf("</main>") < body.indexOf('<footer class="end">'), "页脚在 main 之外");
  assert.match(body, /<footer class="end"><a href="\/privacy">统计说明<\/a>/);
  assert.equal((body.match(/id="main"/g) || []).length, 1);
});

test("the hero plays the recorded replay, falls back to a still frame, and the four scene images load lazily with their size written down", () => {
  const body = page("1.4.104", 27_472_418);
  const video = body.match(/<video id="mv"[^>]*>[\s\S]*?<\/video>/)?.[0] || "";
  assert.match(video, /muted loop playsinline preload="none"/, "无声循环内联；preload=none：脚本不叫它之前一个字节都不下");
  assert.doesNotMatch(video, /autoplay/, "不写 autoplay：开始时机由脚本定（首屏图片到位之后，省流量与弱动效时不播）");
  assert.match(video, /poster="\/assets\/hero-replay-poster\.webp"/, "视频没加载出来时先显示封面");
  assert.ok(video.indexOf("video/quicktime") < video.indexOf("video/webm"), "Safari 的 HEVC 源要排在 WebM 前面，Chrome 会跳过它");
  assert.match(body, /<img class="still" src="\/assets\/hero-replay-poster\.webp"[^>]*alt="[^"]*非实时[^"]*"/, "减少动态效果时显示的静止图");
  assert.match(video, /aria-label="[^"]*真实分钟线回放 非实时[^"]*"/, "读屏用户能听到这是回放、非实时");
  // 前三张是脚本懒加载的图片：HTML 里只有占位图 + data-src，真地址靠脚本在接近视野时换上；
  // 第四张是循环视频（下一条用例单独守），所以这里按 scene-1..3 的 data-src 取。
  const shots = body.match(/<img class="lz" src="data:[^"]*" data-src="\/assets\/scene-[1-3]-[a-z]+\.webp"[^>]*>/g) || [];
  assert.equal(shots.length, 3);
  for (const tag of shots) {
    assert.match(tag, /width="2940" height="1912"/, "屏下场景图写明宽高，避免版面跳动");
    assert.match(tag, /decoding="async"/);
    assert.doesNotMatch(tag, /loading="lazy"/, "不用浏览器自带懒加载：它的提前量常把首屏下面的图在首屏加载时就一起拉了");
    assert.doesNotMatch(tag, /\ssrcset=/, "真的 srcset 只能放在 data-srcset 里，写成 srcset 浏览器会立刻去选一档下载");
    const set = tag.match(/data-srcset="([^"]+)"/)?.[1] || "";
    assert.deepEqual(set.split(", ").map(item => item.split(" ")[1]), ["720w", "1280w", "2100w", "2940w"], "四档宽度，最大一档是原图");
    assert.match(tag, /sizes="\(min-width:1120px\) 1040px, \(min-width:981px\) calc\(100vw - 80px\), calc\(100vw - 48px\)"/, "sizes 要与版面一致：容器最宽 1040，窄屏两侧留白 40 和 24");
  }
  assert.equal(new Set(shots.map(tag => tag.match(/scene-\d/)[0])).size, 3, "三张用三张不同的图");
  // 没有脚本时占位图会让图空着，所以每张都要有 noscript 里的真图；真地址不能只存在于 data-src 里。
  for (const name of ["scene-1-switch", "scene-2-alert", "scene-3-fullscreen"]) {
    assert.match(body, new RegExp(`<noscript><img src="/assets/${name}\\.webp" srcset="[^"]+" sizes="[^"]+" width="2940" height="1912"[^>]*></noscript>`), `${name} 缺 noscript 备份`);
  }
  assert.doesNotMatch(body, /<img src="\/assets\/scene-[1-3]-[a-z]+\.webp"[^>]*>(?!<\/noscript>)/, "场景图不能再以真地址直接出现在 HTML 里，否则会随首屏一起下载");
  assert.doesNotMatch(body, /class="scene |class="dock"|hero-window|wall-\d/, "自绘场景和上一版素材不再出现");
});

test("the first screen preloads its cover image at high priority and keeps the decorative background out of the way", () => {
  const body = page("1.4.104", 27_472_418);
  const head = body.slice(0, body.indexOf("</head>"));
  assert.match(head, /<link rel="preload" as="image" href="\/assets\/hero-replay-poster\.webp" fetchpriority="high">/, "首屏封面要在解析 HTML 时就开始拉，不等样式表和脚本");
  assert.ok(head.indexOf('rel="preload"') < head.indexOf('rel="stylesheet"'), "预加载排在样式表之前才有意义");
  assert.equal((head.match(/rel="preload"/g) || []).length, 1, "只预加载首屏封面一个：多预加载只会互相抢带宽");
  assert.doesNotMatch(head, /rel="preload"[^>]*(scene-|\.mp4|\.webm)/, "屏下的图和视频绝不预加载");
  assert.match(body, /<img src="\/assets\/atmo\.svg" alt="" fetchpriority="low" decoding="async">/, "纯装饰的背景图降低优先级，不和首屏封面抢");
  // 小图标在屏下，不随首屏一起拉。
  for (const name of ["file-pdf", "file-dmg", "file-zip", "installer-icon"]) {
    assert.match(body, new RegExp(`<img src="/assets/${name}\\.png"[^>]*loading="lazy"`), `${name} 在屏下，应延后加载`);
  }
});

test("the fourth scene is a looping video that waits for the viewer, keeps a poster, and has a still frame for reduced motion", () => {
  const body = page("1.4.104", 27_472_418);
  const video = body.match(/<video id="sv"[^>]*>[\s\S]*?<\/video>/)?.[0] || "";
  assert.ok(video, "第 4 张卖点图应是 id=sv 的视频");
  assert.match(video, /muted loop playsinline/, "无声循环内联播放，手机和 Safari 才不会弹全屏");
  assert.match(video, /preload="none"/, "没滚到就不下载：四千像素的视频合计约 10MB，不能随首屏一起拉");
  assert.doesNotMatch(video, /autoplay/, "播放时机交给脚本（滚进屏幕才播），不能写 autoplay");
  assert.match(video, /width="2940" height="1912"/, "写明宽高，加载前后版面不跳动");
  // 封面约 300KB：写成 poster 属性浏览器会随首屏就拉，所以先放 data-poster，接近视野时由脚本换成 poster。
  assert.doesNotMatch(video, /\sposter=/, "不能直接写 poster 属性，否则封面随首屏一起下载");
  assert.match(video, /data-poster="\/assets\/scene-4-hide-poster\.webp"/, "接近视野时脚本把它换成 poster，与视频第一帧一致");
  const posterSet = video.match(/data-posterset="([^"]+)"/)?.[1] || "";
  assert.deepEqual(posterSet.split(", ").map(item => item.split(" ")[1]), ["720w", "1280w", "2100w", "2940w"], "封面也分档，按屏幕挑");
  // 视频分小档（1470 宽，手机和普通屏）与大档（2940 宽，高清屏）。源不写 src：写了浏览器在解析时就会挑一个，preload=none 也拦不住它去读元数据之外的东西。
  const sources = [...video.matchAll(/<source ([^>]*)>/g)].map(match => match[1]);
  assert.equal(sources.length, 4);
  assert.ok(sources.every(attrs => !/\ssrc=|^src=/.test(attrs) && /data-src="\/assets\/scene-4-hide[^"]*\.(?:mp4|webm)\?v=[0-9a-f]{10}"|data-src="\/assets\/scene-4-hide[^"]*\.(?:mp4|webm)"/.test(attrs)), "四个源都只有 data-src");
  assert.deepEqual(sources.map(attrs => attrs.match(/data-tier="(\w)"/)[1]), ["s", "s", "l", "l"], "每档里 mp4 在前 webm 在后");
  assert.match(sources[0], /scene-4-hide-1470\.mp4/);
  assert.match(sources[1], /scene-4-hide-1470\.webm/);
  assert.match(sources[2], /scene-4-hide\.mp4/);
  assert.match(sources[3], /scene-4-hide\.webm/);
  assert.match(video, /<video id="sv" hidden /, "脚本接管前视频是隐藏的，占位由静止图的占位图撑着");
  assert.ok(video.indexOf("video/mp4") < video.indexOf("video/webm"), "Safari 的 HEVC 源要排在 WebM 前面，Chrome 会跳过它");
  assert.match(video, /codecs="hvc1"/, "HEVC 要标 hvc1，Safari 才认");
  assert.match(video, /aria-label="[^"]*自动收进程序坞[^"]*示意数据[^"]*"/, "读屏用户能听到画面讲了什么、是示意数据");
  assert.match(body, /<figure class="sc-shot" id="s4"><video id="sv"/, "容器 id=s4 是脚本观察的对象");
  const still = body.match(/<img class="lz" id="ss"[^>]*>/)?.[0] || "";
  assert.match(still, /data-still="\/assets\/scene-4-hide\.webp"/, "静止图的真地址只存在 data-still 里，静态模式才由脚本换上");
  assert.match(still, /data-stillset="[^"]*720w[^"]*1280w[^"]*2100w[^"]*2940w"/, "静止图也分档");
  assert.match(still, /alt="[^"]*收进程序坞[^"]*"/, "静止图有替代文字");
  assert.match(still, /width="2940" height="1912"/);
  assert.doesNotMatch(still, /\ssrc="\/assets\/|\ssrcset=/, "静止图加载时只是占位，不然视频用户也会白下 350KB");
  assert.match(body, /<noscript><img src="\/assets\/scene-4-hide\.webp"[^>]*width="2940" height="1912"[^>]*alt="[^"]*收进程序坞[^"]*"><\/noscript>/, "没有脚本时：视频是隐藏的，改显静止图");
  assert.equal((body.match(/id="sv"/g) || []).length, 1);
  assert.doesNotMatch(body, /<img src="\/assets\/scene-4-hide\.webp"[^>]*>(?!<\/noscript>)/, "第四张不能再是不会动的普通图片");
});

test("the footer links to a statistics note that states what is stored and for how long", async () => {
  const body = page("1.4.104", 27_472_418);
  assert.match(body, /<a href="\/privacy">统计说明<\/a>/);
  const context = env("secret");
  const response = await worker.fetch(request("/privacy"), context);
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type"), /text\/html/);
  assert.equal(response.headers.get("x-frame-options"), "DENY");
  const note = await response.text();
  // 个人信息保护法第 17 条要求事前告知的要素：处理者、目的、方式与内容、保存期限、行使权利的渠道。
  assert.match(note, /处理者：[^。]*。目的：统计网页下载次数/);
  // 公开源码导出会拒绝正文里出现账号里的数字（它与旧的个人号码同形）；账号只能出现在 GitHub 地址里。
  const owner = (note.match(/github\.com\/(\d+)-/) || [])[1];
  assert.ok(owner, "说明页里应有指向发行页的链接，账号数字只在链接里");
  assert.doesNotMatch(note.replaceAll(/https:\/\/github\.com\/\d+-[A-Za-z0-9-]+/g, "https://github.com/owner"), new RegExp(owner), "账号数字不能出现在链接以外的正文里");
  assert.match(note, /只打开本页不会保存任何标识/, "说明页必须讲清打开页面不存标识");
  assert.match(note, /点击下载时，浏览器才会保存一个随机生成的标识/);
  assert.match(note, /记录版本、文件、时间，以及该标识的哈希值/);
  assert.match(note, /保存 30 天/);
  assert.match(note, /同一浏览器在 60 秒内重复下载同一文件只记录一次/);
  assert.match(note, /自动访问不写入统计，也不会创建 cookie/);
  assert.match(note, /不保存 IP、账号、设备信息或浏览器类型/);
  assert.match(note, /href="https:\/\/github\.com\/[^"]+\/issues"/, "必须给出行使权利的联系渠道");
  assert.doesNotMatch(note, /匿名/, "哈希后的标识仍能对应到同一个浏览器，不能叫匿名");
  assert.equal((await worker.fetch(request("/privacy", { method: "HEAD" }), context)).body, null);
  assert.equal(context.DB.inserted.length, 0, "看说明页不产生下载记录");
});

test("each public asset is registered with an origin and licence status", async () => {
  const { readdirSync, readFileSync, statSync } = await import("node:fs");
  const { join } = await import("node:path");
  const assetsDir = fileURLToPath(new URL("../public/assets/", import.meta.url));
  const assets = readdirSync(assetsDir).filter(name => statSync(join(assetsDir, name)).isFile());
  const register = readFileSync(fileURLToPath(new URL("../ASSETS.md", import.meta.url)), "utf8");
  for (const asset of assets) {
    // 必须是登记表里的一行（行首 | `文件名` |）；文档别处顺口提到文件名不算登记。
    assert.ok(register.split("\n").some(line => line.startsWith(`| \`${asset}\` |`)), `${asset} 没有来源登记`);
  }
  // 这三类素材现在就在 public/ 里：登记表必须如实写“没有取得授权”，不能写成已经没事。
  assert.match(register, /未取得授权的素材[\s\S]*真实股票行情/, "必须点名真实行情没有授权");
  assert.match(register, /未取得授权的素材[\s\S]*苹果素材/, "必须点名苹果素材没有授权");
  // 第三方影视画面已在 2026-10-28 换成 CC0 壁纸：登记里不应再残留需要署名的说法，否则登记与图片对不上。
  assert.doesNotMatch(register, /Sintel|CC BY 3\.0|Blender Foundation/, "第三张图已不含影视画面，登记里不该再写需要署名的第三方画面");
  assert.match(register, /公开的 GPL 源码快照/, "必须写明这些文件会随公开快照分发");
});

test("every restored file without clearance is marked as such in its own row, and the original sources are named", async () => {
  const { readFileSync } = await import("node:fs");
  const register = readFileSync(fileURLToPath(new URL("../ASSETS.md", import.meta.url)), "utf8");
  const rows = name => register.split("\n").find(line => line.startsWith(`| \`${name}\` |`)) || "";
  for (const name of ["hero-replay-60fps.webm", "hero-replay-60fps-hevc.mov", "hero-replay-poster.webp",
    "scene-1-switch.webp", "scene-2-alert.webp", "scene-3-fullscreen.webp", "scene-4-hide.webp", "scene-4-hide-poster.webp",
    "scene-4-hide.mp4", "scene-4-hide.webm", "file-pdf.png", "file-dmg.png", "file-zip.png"]) {
    assert.match(rows(name), /未取得授权/, `${name} 的登记行必须写明没有授权`);
  }
  assert.match(rows("scene-3-fullscreen.webp"), /壁纸 13/, "第三张必须写明用的是哪张壁纸");
  assert.doesNotMatch(rows("scene-3-fullscreen.webp"), /Sintel/, "第三张已不含影视画面");
  assert.match(rows("hero-replay-60fps.webm"), /真实分钟线/, "视频必须写明是真实行情");
  for (const name of ["landing.css", "landing.js", "atmo.svg", "installer-icon.png"]) {
    assert.doesNotMatch(rows(name), /未取得授权/, `${name} 是本项目自己的东西`);
  }
});

test("the restored hero video's PingFang SC rendering is disclosed in the register", async () => {
  const { readFileSync } = await import("node:fs");
  const register = readFileSync(fileURLToPath(new URL("../ASSETS.md", import.meta.url)), "utf8");
  assert.match(register, /苹方（PingFang SC）渲染，网站图片是否允许使用该字体未核实/, "字体风险必须如实写明，不能悄悄回到系统字体");
  assert.doesNotMatch(register, /Noto Sans CJK SC.*已重渲|中文用 Noto/, "视频不是用开源字体重渲的，不能沿用上一版的说法");
});

test("versions compare numerically and a stale cache never beats the build's own version", async () => {
  assert.equal(compareVersions("1.4.10", "1.4.9"), 1, "按数字比，不按字符串");
  assert.equal(compareVersions("1.4.9", "1.4.10"), -1);
  assert.equal(compareVersions("1.4.106", "1.4.106"), 0);
  assert.equal(compareVersions("2.0.0", "1.99.99"), 1);
  const original = globalThis.caches;
  try {
    const cacheWith = version => ({ default: { match: async () => ({ json: async () => ({ version }) }), put: async () => {} } });
    globalThis.caches = cacheWith("1.4.103");
    assert.equal(await cachedLatestVersion("1.4.106"), "1.4.106", "缓存里是上一版清单时，仍显示刚部署的这一版");
    globalThis.caches = cacheWith("1.4.107");
    assert.equal(await cachedLatestVersion("1.4.106"), "1.4.107", "清单已前进时跟着前进");
    globalThis.caches = cacheWith("1.4.106");
    assert.equal(await cachedLatestVersion("1.4.106"), "1.4.106");
  } finally {
    if (original === undefined) delete globalThis.caches;
    else globalThis.caches = original;
  }
});

test("the size belongs to the built version only", () => {
  assert.equal(dmgBytesFor("1.4.106", "1.4.106", 27_472_418), 27_472_418);
  assert.equal(dmgBytesFor("1.4.107", "1.4.106", 27_472_418), null, "清单前进后不能拿旧版的大小");
  assert.equal(dmgBytesFor("1.4.106", "1.4.106", "__CURRENT_DMG_BYTES__"), null, "没注入大小就不显示");
  assert.equal(dmgBytesFor("1.4.106", "1.4.106", 0), null);
});

test("the landing page alone drops inline styles from its policy and carries the trademark note", async () => {
  const context = env("secret");
  const landing = (await worker.fetch(request("/"), context)).headers.get("content-security-policy");
  assert.match(landing, /style-src 'self';/);
  assert.doesNotMatch(landing, /unsafe-inline/, "首页没有内联样式，策略里不留 unsafe-inline");
  const privacy = (await worker.fetch(request("/privacy"), context)).headers.get("content-security-policy");
  assert.match(privacy, /style-src 'self' 'unsafe-inline'/, "说明页仍用内联 <style>");
  const body = page("1.4.104", 27_472_418);
  assert.doesNotMatch(body, /\sstyle=/, "首页不能有内联 style 属性，否则收紧后会丢样式");
  assert.match(body, /Mac 和 macOS 是 Apple Inc\. 在美国及其他国家和地区的商标/);
  assert.match(body, /与 Apple 无关联/);
});

test("only the build's own version may show the build's DMG size", async () => {
  const context = env("secret");
  const response = await worker.fetch(request("/"), context, { waitUntil: () => {} });
  const body = await response.text();
  assert.equal(response.status, 200);
  assert.match(body, /<span class="i">__CURRENT_VERSION__[^<]*<\/span>|前往发布页/);
  assert.doesNotMatch(body, /MB</, "未注入大小的源码态不能显示大小");
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

test("repeat clicks still redirect but record once per browser, file and minute", async () => {
  const context = env("secret");
  const first = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg"), context);
  const cookie = first.headers.get("set-cookie").split(";")[0];
  const second = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg", { headers: { cookie } }), context);
  assert.equal(first.status, 302);
  assert.equal(second.status, 302, "去重只影响统计，不能拦下载");
  assert.equal(context.DB.inserted.length, 1, "同一浏览器一分钟内同一文件只记一次");
  const now = new Date("2026-10-27T14:00:15Z");
  const sameMinute = await downloadEventId("visitor", "public", "1.4.104", "Skyrocket-V1.4.104.dmg", now);
  const nextMinute = await downloadEventId("visitor", "public", "1.4.104", "Skyrocket-V1.4.104.dmg", new Date("2026-10-27T14:01:00Z"));
  const otherAsset = await downloadEventId("visitor", "public", "1.4.104", "Skyrocket-1.4.104-macos.zip", now);
  assert.notEqual(sameMinute, nextMinute, "下一分钟重新下载可计数");
  assert.notEqual(sameMinute, otherAsset, "不同文件独立计数");
});

test("explicit automated requests redirect without storing cookies or statistics", async () => {
  const context = env("secret");
  for (const headers of [
    { "user-agent": "Googlebot/2.1" },
    { "user-agent": "curl/8.7.1" },
    { purpose: "prefetch" },
  ]) {
    const response = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg", { headers }), context);
    assert.equal(response.status, 302, "自动访问也必须能拿到官方地址");
    assert.equal(response.headers.get("set-cookie"), null, "自动访问不创建访客 cookie");
  }
  assert.equal(context.DB.inserted.length, 0, "自动访问不污染下载统计");
  assert.equal(isAutomatedRequest(request("/download", { headers: { "user-agent": "Mozilla/5.0" } })), false, "普通浏览器不误判");
  assert.equal(isAutomatedRequest(request("/download", { headers: { "user-agent": "CUBOT/1.0" } })), false, "不能按裸 bot 子串误伤手机型号");
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

// 机主 Cookie 里放的是由令牌派生的值，不是令牌本身；测试里按同一规则自己算一遍，不去读产品内部函数。
async function ownerCookie(token) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`skyrocket-owner-cookie:${token}`));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

test("the owner cookie keeps manual clicks out of the public count", async () => {
  const hex = "a".repeat(64);
  const context = env(hex);
  // 首页链接不带 route，机主点下去也必须算内部通路（上一轮就是漏在这里）。
  const mine = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg", {
    headers: { cookie: `skyrocket_check=${await ownerCookie(hex)}` },
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
  const cookie = granted.headers.get("set-cookie");
  assert.match(cookie, /^skyrocket_check=[a-f0-9]{64}; .*HttpOnly/);
  assert.equal(cookie.split(";")[0], `skyrocket_check=${await ownerCookie("secret")}`, "Cookie 是派生值");
  assert.doesNotMatch(cookie, /=secret(;|$)/, "令牌原文不能出现在 Cookie 里");
});

test("the owner cookie round-trips, and the raw token is no longer accepted as a cookie", async () => {
  const token = "c".repeat(64);
  const context = env(token);
  const granted = await worker.fetch(request("/owner", { headers: { authorization: "Basic " + btoa(`owner:${token}`) } }), context);
  const cookie = granted.headers.get("set-cookie").split(";")[0];
  assert.doesNotMatch(cookie, new RegExp(token), "Cookie 里不含令牌");
  const viaCookie = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg", { headers: { cookie } }), context);
  assert.equal(viaCookie.status, 302);
  assert.equal(context.DB.inserted.at(-1)[4], "release-check", "/owner 发的 Cookie 能把手点算进内部通路");
  const rawAsCookie = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg", { headers: { cookie: `skyrocket_check=${token}` } }), context);
  assert.equal(rawAsCookie.status, 302);
  assert.equal(context.DB.inserted.at(-1)[4], "public", "令牌原文放进 Cookie 不再通过");
  const header = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip", { headers: { "x-skyrocket-check": token } }), context);
  assert.equal(header.status, 302);
  assert.equal(context.DB.inserted.at(-1)[4], "release-check", "发布验收脚本走请求头，仍用真实令牌");
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

test("merely opening the landing page sets no cookie and records nothing", async () => {
  const context = env("secret");
  for (const path of ["/", "/index.html", "/privacy"]) {
    const response = await worker.fetch(request(path), context);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("set-cookie"), null, `${path} 不能在打开时设置 cookie`);
  }
  assert.equal(context.DB.inserted.length, 0);
});

test("the identifier is first written by the download click, then reused", async () => {
  const context = env("secret");
  await worker.fetch(request("/"), context);
  const first = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg"), context);
  assert.equal(first.status, 302);
  assert.match(first.headers.get("set-cookie"), /skyrocket_download_id=[A-Za-z0-9-]{36}/);
  assert.equal(context.DB.inserted.length, 1);
  const cookie = first.headers.get("set-cookie").split(";")[0];
  // 换一个文件再点：同一文件一分钟内的重复点击会被去重，这里要观察的是"同一个浏览器标识被沿用"。
  const again = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-1.4.104-macos.zip", { headers: { cookie } }), context);
  assert.equal(again.headers.get("set-cookie"), null, "已有标识就沿用，不重复下发");
  const hashes = context.DB.inserted.map(row => row[row.length - 1]);
  assert.equal(hashes[0], hashes[1], "同一浏览器两次点击对应同一个哈希");
});

test("every HTML and JSON response carries the isolation headers", async () => {
  const context = env("secret");
  for (const path of ["/", "/privacy", "/health", "/stats", "/nope", "/download?version=bad&asset=x"]) {
    const response = await worker.fetch(request(path), context);
    assert.match(response.headers.get("permissions-policy"), /camera=\(\)/, `${path} 缺 permissions-policy`);
    assert.match(response.headers.get("permissions-policy"), /geolocation=\(\)/);
    assert.equal(response.headers.get("cross-origin-opener-policy"), "same-origin", `${path} 缺 COOP`);
    assert.equal(response.headers.get("cross-origin-resource-policy"), "same-origin", `${path} 缺 CORP`);
    assert.match(response.headers.get("strict-transport-security"), /^max-age=\d{7,}$/, `${path} 缺 HSTS`);
    assert.doesNotMatch(response.headers.get("strict-transport-security"), /includeSubDomains|preload/, "不替整个域名做主");
  }
});

test("landing uses a short browser and edge cache while privacy stays fresh and stateful routes stay no-store", async () => {
  const context = env("secret");
  const landing = await worker.fetch(request("/"), context);
  assert.equal(landing.headers.get("cache-control"), landingCacheControl());
  assert.match(landing.headers.get("cache-control"), /^public, max-age=60, s-maxage=300, stale-while-revalidate=60$/,
    "首页浏览器缓存 60 秒，边缘缓存 5 分钟");
  assert.equal(landing.headers.get("set-cookie"), null);
  const privacy = await worker.fetch(request("/privacy"), context);
  assert.equal(privacy.headers.get("cache-control"), "no-cache", "说明页不进共享边缘缓存");
  assert.equal(privacy.headers.get("set-cookie"), null);
  for (const path of ["/health", "/stats", "/nope", "/download?version=bad&asset=x", "/robots.txt"]) {
    assert.equal((await worker.fetch(request(path), context)).headers.get("cache-control"), "no-store", path);
  }
  const redirect = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg"), context);
  assert.equal(redirect.headers.get("cache-control"), "no-store");
});

test("unsupported methods on known paths get 405 with Allow, unknown paths stay 404", async () => {
  const context = env("secret");
  for (const path of ["/", "/privacy", "/health", "/robots.txt", "/stats", "/owner", "/assets/landing.css", "/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg"]) {
    for (const method of ["POST", "PUT", "DELETE", "OPTIONS"]) {
      const response = await worker.fetch(request(path, { method }), context);
      assert.equal(response.status, 405, `${method} ${path}`);
      assert.equal(response.headers.get("allow"), "GET, HEAD", `${method} ${path} 要带 Allow`);
    }
  }
  assert.equal(context.DB.inserted.length, 0, "被拒绝的方法不产生下载记录");
  assert.equal((await worker.fetch(request("/nope", { method: "POST" }), context)).status, 404);
});

test("an unexpected exception never shows the platform error page, and never blocks a public download", async () => {
  const exploding = { get CHECK_TOKEN() { throw new TypeError("boom"); }, DB: new FakeDB() };
  const quiet = console.error; console.error = () => {};
  try {
    const stats = await worker.fetch(request("/stats", { headers: { authorization: "Basic eA==" } }), exploding);
    assert.equal(stats.status, 500);
    assert.deepEqual(await stats.json(), { error: "internal_error" });
    assert.equal(stats.headers.get("cache-control"), "no-store");
    assert.equal(stats.headers.get("x-content-type-options"), "nosniff");
    const download = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg"), exploding);
    assert.equal(download.status, 302, "统计或鉴权出问题时，官方下载照样转跳");
    assert.match(download.headers.get("location"), /^https:\/\/github\.com\/.*Skyrocket-V1\.4\.104\.dmg$/);
    const check = await worker.fetch(request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg&route=release-check"), exploding);
    assert.equal(check.status, 500, "发布验收通路不装作成功");
    const bad = await worker.fetch(request("/download?version=9.9.9&asset=evil.dmg"), exploding);
    assert.notEqual(bad.status, 302, "不合法的地址不能借兜底转跳");
  } finally { console.error = quiet; }
  const home = (await import("../src/index.mjs")).failureResponse(request("/"), new Error("x"));
  assert.equal(home.status, 503);
  assert.equal(home.headers.get("retry-after"), "60");
  assert.match(await home.text(), /github\.com\/[^"]+\/releases\/tag/, "兜底页给出官方发布页");
  const weird = (await import("../src/index.mjs")).failureResponse({ get url() { throw new Error("no url"); } }, null);
  assert.equal(weird.status, 503, "连兜底自己也出错时，仍然只回一个最小响应而不抛出");
});

// 写库被故意拦住时，如果转跳还在等它，这条会一直等下去；给它一个明确的超时，让"又等写库了"表现为红而不是挂起。
test("the redirect does not wait for the statistics write, and the cookie is still issued", { timeout: 5000 }, async () => {
  const context = env("secret");
  let release; const gate = new Promise(resolve => { release = resolve; });
  const realPrepare = context.DB.prepare.bind(context.DB);
  context.DB.prepare = sql => {
    const statement = realPrepare(sql);
    const run = statement.run;
    statement.run = async () => { if (sql.startsWith("INSERT")) await gate; return run(); };
    return statement;
  };
  const waiting = [];
  const response = await worker.fetch(
    request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg"), context, { waitUntil: promise => waiting.push(promise) });
  assert.equal(response.status, 302);
  assert.match(response.headers.get("set-cookie"), /skyrocket_download_id=[A-Za-z0-9-]{36}/);
  assert.equal(context.DB.inserted.length, 0, "写库还没放行，转跳已经回来了");
  assert.equal(waiting.length, 1, "写库交给后台执行");
  release();
  await Promise.all(waiting);
  assert.equal(context.DB.inserted.length, 1, "放行后照常写入");
});

test("a failing background write is swallowed and never surfaces", async () => {
  const context = env("secret");
  context.DB.failInsert = true;
  const waiting = [];
  const quiet = console.error; console.error = () => {};
  try {
    const response = await worker.fetch(
      request("/download?version=1.4.104&asset=Skyrocket-V1.4.104.dmg"), context, { waitUntil: promise => waiting.push(promise) });
    assert.equal(response.status, 302);
    await Promise.all(waiting);
  } finally { console.error = quiet; }
  assert.equal(context.DB.inserted.length, 0);
});

test("a version newer than the latest known one is not counted, but unknown or older ones are", async () => {
  const { versionIsPlausible } = await import("../src/index.mjs");
  const never = async () => { throw new Error("不该联网"); };
  assert.equal(await versionIsPlausible("1.4.106", "1.4.106", never), true, "等于最新版");
  assert.equal(await versionIsPlausible("1.4.9", "1.4.106", never), true, "旧版本照常记，按数字比较");
  assert.equal(await versionIsPlausible("9.9.9", "1.4.106", async () => "1.4.106"), false, "刷新后仍比最新版新：灌水");
  assert.equal(await versionIsPlausible("1.4.107", "1.4.106", async () => "1.4.107"), true, "刚发新版、本地缓存还旧：刷新后确认存在");
  assert.equal(await versionIsPlausible("9.9.9", "1.4.106", async () => ""), true, "刷新也取不到时放行，宁可多记一条");
  assert.equal(await versionIsPlausible("9.9.9", "__CURRENT_VERSION__", never), true, "已知最新版本身不可信时放行");
});

// 下面这条读样式表文本：窄屏溢出、旧浏览器降级、强制配色这几类规则只有真浏览器才能量出效果，
// node 里没法渲染，所以只守"规则还在、顺序对"。效果本身用 Chrome 实测过（320 宽 scrollWidth 等于 clientWidth）。
test("the stylesheet keeps the narrow-screen fix and the fallbacks, and the fallbacks come after the rules they override", async () => {
  const { readFileSync } = await import("node:fs");
  const css = readFileSync(fileURLToPath(new URL("../public/assets/landing.css", import.meta.url)), "utf8");
  assert.match(css, /@media\(max-width:980px\)\{\.hero\{grid-template-columns:minmax\(0,1fr\)/, "窄屏单列必须用 minmax(0,1fr)，写 1fr 会被按钮固定宽度撑出横向滚动条");
  assert.match(css, /\.cta\{[^}]*max-width:100%/, "下载按钮不能比容器宽");
  assert.doesNotMatch(css, /\.cta\{[^}]*min-width:340px/, "不能退回写死最小宽度");
  assert.match(css, /@media\(forced-colors:active\)/);
  assert.match(css, /@media\(prefers-contrast:more\)/);
  assert.match(css, /@media\(prefers-reduced-motion:reduce\)\{\.win video\{display:none\}\.win \.still\{display:block\}\}/,
    "减少动态效果时必须藏视频、显示静止图");
  assert.match(css, /\.win \.still\{display:none\}/, "平时静止图是藏着的，否则会盖在视频上");
  // 第四张卖点图：视频与静止图谁显示由脚本切 hidden 属性（含减少动态效果、省流量），样式只负责三件事：
  // 隐藏态真的不占位、图和视频都按比例占住位置（占位图是 1 像素，不写比例会塌成一条线）、无脚本时藏起占位图。
  // 这条规则必须存在：视频自带的 display:block 会盖过浏览器默认的 [hidden]；它靠选择器优先级（类+属性）取胜，与书写顺序无关。
  assert.match(css, /\.sc-shot \[hidden\]\{display:none\}/, "hidden 属性要真的藏起来");
  assert.match(css, /\.sc-shot img,\.sc-shot video\{[^}]*aspect-ratio:2940\/1912/, "视频和占位图在加载前后都要占住同样的位置");
  // 无脚本时占位图留在原处撑住比例，noscript 里的真图绝对定位盖在它上面：不依赖较新的 scripting 媒体查询（旧浏览器不认它会叠成两倍高）。
  assert.match(css, /\.sc-shot\{[^}]*position:relative/, "真图要相对这个容器定位");
  assert.match(css, /\.sc-shot noscript img\{position:absolute;top:0;left:0;width:100%;height:100%;aspect-ratio:auto\}/, "无脚本时真图盖住占位框");
  assert.doesNotMatch(css, /scripting:none/, "不用 scripting 媒体查询：旧浏览器不认它");
  assert.doesNotMatch(css, /\.sc-shot \.still|\.sc-shot video\{display:none\}/, "旧的靠样式切换静止图的写法不能残留，否则会和脚本的切换打架");
  assert.doesNotMatch(css, /\.scene|\.dock|cqw/, "自绘场景已经换成整图，样式不应残留");
  // 被覆盖的基础规则以最后一条为界：降级和强制配色块必须排在它之后，否则同优先级下基础规则会赢。
  const lastBase = css.indexOf(".nojs-note{");
  assert.ok(lastBase > 0, "找不到基础规则的界标，下面的顺序判断会变成恒真");
  for (const marker of ["@media(forced-colors:active)", "@media(prefers-contrast:more)"]) {
    assert.ok(css.indexOf(marker) > lastBase, `${marker} 必须排在被它覆盖的基础规则之后，否则同优先级下基础规则会赢`);
  }
  assert.match(page("1.4.104", 27_472_418), /<noscript><p class="nojs-note">无法开始下载？<a href="https:\/\/github\.com\/[^"]+\/releases\/tag\//, "没有脚本时也要有备用下载入口");
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
