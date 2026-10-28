import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, existsSync, truncateSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// 构建脚本以前没有任何测试。这里用一个假的"导出图标"程序顶掉 Python（CI 上没有 PyQt），
// 其余——版本读取、DMG 字节数、模板内联、占位符替换、静态素材复制、路由排除——都是真跑。
const serviceDir = fileURLToPath(new URL("..", import.meta.url));

function build(extraEnv = {}) {
  const work = mkdtempSync(join(tmpdir(), "dl-build-test-"));
  const python = join(work, "fake-python.sh");
  const icon = Buffer.alloc(900, 7).toString("base64");
  writeFileSync(python, `#!/bin/sh\necho '{"light":"${icon}","size":128}'\n`);
  chmodSync(python, 0o755);
  const out = join(work, "pages");
  const env = { ...process.env, SF_PYTHON: python, SF_PAGES_OUT: out, SF_APP_VERSION: "9.8.7", ...extraEnv };
  const stdout = execFileSync(process.execPath, ["build-pages.mjs"], { cwd: serviceDir, env, encoding: "utf8" });
  return { work, out, info: JSON.parse(stdout.trim().split("\n").at(-1)) };
}

test("the build injects the version and the real DMG size, inlines the template and copies assets", async () => {
  const dmgDir = mkdtempSync(join(tmpdir(), "dl-dmg-"));
  const dmg = join(dmgDir, "Skyrocket-V9.8.7.dmg");
  writeFileSync(dmg, "");
  truncateSync(dmg, 28_400_000);
  const { work, out, info } = build({ SF_DMG_PATH: dmg });
  try {
    assert.equal(info.version, "9.8.7");
    assert.equal(info.dmgBytes, 28_400_000);
    const worker = readFileSync(join(out, "_worker.js"), "utf8");
    assert.doesNotMatch(worker, /__CURRENT_VERSION__|__CURRENT_DMG_BYTES__/, "占位符必须全部替换掉");
    assert.doesNotMatch(worker, /from "\.\/landing\.mjs"|^export function landingPage/m, "模板必须已内联进单文件");
    const built = await import(pathToFileURL(join(out, "_worker.js")).href);
    const page = built.page("9.8.7", 28_400_000);
    assert.match(page, /<span class="i">9\.8\.7 约 28 MB<\/span>/);
    assert.equal(built.dmgBytesFor("9.8.7"), 28_400_000, "构建出来的默认字节数就是 DMG 的真实大小");
    assert.equal(built.dmgBytesFor("9.9.0"), null, "页面显示的不是构建那一版时不显示大小");
    const routes = JSON.parse(readFileSync(join(out, "_routes.json"), "utf8"));
    // 视频必须落到 Worker（Pages 静态层不支持 Range），其余素材逐个排除；名单按产物目录里的实际文件生成。
    assert.deepEqual(routes.include, ["/*"]);
    const shipped = readdirSync(join(out, "assets"));
    const videos = shipped.filter(name => /\.(?:webm|mov|mp4)$/i.test(name));
    assert.ok(videos.length >= 2, "产物里应有两份视频，否则下面的对拍是空转");
    assert.deepEqual([...routes.exclude].sort(), shipped.filter(name => !videos.includes(name)).map(name => `/assets/${name}`).sort(),
      "排除名单 = 产物里所有非视频素材，一个不多一个不少");
    for (const video of videos) assert.ok(!routes.exclude.includes(`/assets/${video}`), `${video} 不能被排除，否则 Safari 拿到的是不支持分段的静态响应`);
    assert.ok(routes.exclude.length <= 80, "离平台 100 条上限要留余量");
    const { videoTypeFor } = await import(pathToFileURL(join(out, "_worker.js")).href);
    for (const video of videos) assert.ok(videoTypeFor(`/assets/${video}`), `${video} 的扩展名 Worker 不认识，会被当成普通素材放过去`);
    for (const name of ["landing.css", "landing.js", "atmo.svg", "installer-icon.png", "hero-replay-60fps.webm", "hero-replay-60fps-hevc.mov", "scene-1-switch.webp", "app-icon.png"]) {
      assert.ok(existsSync(join(out, "assets", name)), `${name} 没有进产物`);
    }
    assert.equal(readFileSync(join(out, "assets", "app-icon.png")).length, 900, "app-icon.png 来自导出程序的输出");
    assert.ok(existsSync(join(out, "_headers")));
  } finally {
    rmSync(work, { recursive: true, force: true });
    rmSync(dmgDir, { recursive: true, force: true });
  }
});

test("every asset reference carries the content hash of the file it points to, and the long cache header is only safe because of that", async () => {
  const sha = file => createHash("sha256").update(readFileSync(file)).digest("hex").slice(0, 10);
  const first = build({ SF_DMG_PATH: "/nonexistent/Skyrocket-V9.8.7.dmg", HOME: mkdtempSync(join(tmpdir(), "dl-home-")) });
  const second = build({ SF_DMG_PATH: "/nonexistent/Skyrocket-V9.8.7.dmg", HOME: mkdtempSync(join(tmpdir(), "dl-home-")) });
  try {
    const worker = readFileSync(join(first.out, "_worker.js"), "utf8");
    const refs = [...worker.matchAll(/\/assets\/([A-Za-z0-9._-]+)\?v=([0-9a-f]{10})/g)];
    const names = new Set(refs.map(match => match[1]));
    for (const name of ["landing.css", "landing.js", "atmo.svg", "app-icon.png", "installer-icon.png", "hero-replay-poster.webp", "hero-replay-60fps.webm", "hero-replay-60fps-hevc.mov", "scene-1-switch.webp", "scene-4-hide.webp", "file-dmg.png"]) {
      assert.ok(names.has(name), `页面里对 ${name} 的引用必须带指纹`);
    }
    for (const [, name, hash] of refs) assert.equal(hash, sha(join(first.out, "assets", name)), `${name} 的指纹必须等于文件内容的哈希`);
    assert.doesNotMatch(worker, /\/assets\/[A-Za-z0-9._-]+\.(?:css|js|svg|png|webp|webm|mov)(?!\?v=)/, "不能留下不带指纹的引用（含视频）");
    // 样式表自己引用的应用图标也要带指纹，并且样式表的指纹是改写之后的内容算出来的。
    const css = readFileSync(join(first.out, "assets", "landing.css"), "utf8");
    assert.match(css, new RegExp(`url\\(/assets/app-icon\\.png\\?v=${sha(join(first.out, "assets", "app-icon.png"))}\\)`));
    assert.doesNotMatch(css, /url\(\/assets\/[A-Za-z0-9._-]+\)/, "样式表里不能有不带指纹的素材引用");
    // 同样的输入得到同样的指纹：否则每次部署都会让所有用户的缓存全部失效。
    assert.deepEqual(first.info.assetHashes, second.info.assetHashes);
    const headers = readFileSync(join(first.out, "_headers"), "utf8");
    assert.match(headers, /\/assets\/\*[\s\S]*?Cache-Control: public, max-age=31536000, immutable/);
    assert.doesNotMatch(headers.split("/assets/atmo.svg")[1] ?? "", /Cache-Control/, "沙箱策略那一段不重复声明缓存");
  } finally {
    rmSync(first.work, { recursive: true, force: true });
    rmSync(second.work, { recursive: true, force: true });
  }
});

test("a reference to a file that is not in public/ fails the build instead of shipping a 404", () => {
  const work = mkdtempSync(join(tmpdir(), "dl-missing-"));
  try {
    const copy = join(work, "services", "download-analytics");
    mkdirSync(copy, { recursive: true });
    cpSync(serviceDir, copy, { recursive: true, filter: source => !/node_modules|dist|__pycache__/.test(source) });
    const landing = join(copy, "src", "landing.mjs");
    writeFileSync(landing, readFileSync(landing, "utf8").replace("/assets/scene-1-switch.webp", "/assets/scene-missing.webp"));
    writeFileSync(join(work, "sf_constants.py"), "APP_VERSION = '9.8.7'\n");
    const python = join(work, "fake-python.sh");
    writeFileSync(python, `#!/bin/sh\necho '{"light":"${Buffer.alloc(900, 7).toString("base64")}","size":128}'\n`);
    chmodSync(python, 0o755);
    assert.throws(() => execFileSync(process.execPath, ["build-pages.mjs"], {
      cwd: copy, stdio: "pipe", encoding: "utf8",
      env: { ...process.env, SF_PYTHON: python, SF_PAGES_OUT: join(work, "pages"), SF_APP_VERSION: "9.8.7", SF_DMG_PATH: "/nonexistent/x.dmg" },
    }), /不存在的素材：scene-missing\.webp/);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
});

test("a missing DMG leaves the size out instead of inventing one", async () => {
  const { work, out, info } = build({ SF_DMG_PATH: "/nonexistent/Skyrocket-V9.8.7.dmg", HOME: mkdtempSync(join(tmpdir(), "dl-home-")) });
  try {
    assert.equal(info.dmgBytes, null);
    const built = await import(pathToFileURL(join(out, "_worker.js")).href);
    assert.equal(built.dmgBytesFor("9.8.7"), null);
    assert.doesNotMatch(built.page("9.8.7"), /MB</);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
});
