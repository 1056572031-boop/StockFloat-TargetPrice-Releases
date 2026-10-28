import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, statSync } from "node:fs";
import { promises as fs } from "node:fs";
import { homedir } from "node:os";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
// SF_PAGES_OUT 只给测试用：让构建测试写到临时目录，不去碰真实的 dist/pages。
const output = process.env.SF_PAGES_OUT ? resolve(process.env.SF_PAGES_OUT) : resolve(root, "dist/pages");
const constants = await fs.readFile(resolve(root, "../../sf_constants.py"), "utf8");
const match = constants.match(/^APP_VERSION\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'/m);
const version = process.env.SF_APP_VERSION || match?.[1];
if (!version || !/^\d+\.\d+\.\d+$/.test(version)) throw new Error("APP_VERSION is unavailable");

// 本版 DMG 的字节数：页面上"约 N MB"的唯一来源，找不到就不显示，绝不写死一个数字。
// 查找顺序：显式指定 > 发版链放在桌面分发目录的成品 > 本树 build/ 下的成品。
function findDmgBytes() {
  const name = `Skyrocket-V${version}.dmg`;
  const candidates = [
    process.env.SF_DMG_PATH,
    join(homedir(), "Desktop", "Skyrocket", name),
    resolve(root, "../../build", name),
  ].filter(Boolean);
  for (const path of candidates) {
    if (existsSync(path)) {
      const bytes = statSync(path).size;
      if (bytes > 1_000_000) return { bytes, path };
    }
  }
  return { bytes: null, path: null };
}
const dmg = findDmgBytes();
if (dmg.bytes === null) console.warn(`WARN: 找不到 Skyrocket-V${version}.dmg，页面将不显示大小`);

// 应用图标由 sf_icons.render_app_icon 导出（与 icon.icns 同一个出图函数），网页不重画。
const python = process.env.SF_PYTHON || resolve(root, "../../.venv/bin/python");
const iconOutput = execFileSync(python, [resolve(root, "export-icon.py"), "128"], {
  encoding: "utf8", env: { ...process.env, QT_QPA_PLATFORM: "offscreen" },
});
const icons = JSON.parse(iconOutput.trim().split("\n").at(-1));
if (typeof icons.light !== "string" || icons.light.length < 512) throw new Error("icon light is unavailable");

// Pages 只认单个 _worker.js，所以页面模板在这里内联进去：去掉 index.mjs 里对它的 import，
// 以及模板文件里的 export 关键字。名字撞车会让 Worker 在部署时才报错，所以先显式检查。
const source = await fs.readFile(resolve(root, "src/index.mjs"), "utf8");
const landing = await fs.readFile(resolve(root, "src/landing.mjs"), "utf8");
const importLine = 'import { landingPage } from "./landing.mjs";\n';
if (!source.includes(importLine)) throw new Error("src/index.mjs 缺少 landing.mjs 的 import 行");
if (!landing.includes("export function landingPage")) throw new Error("src/landing.mjs 缺少 landingPage");
for (const placeholder of ['"__CURRENT_VERSION__"', '"__CURRENT_DMG_BYTES__"', '"__LANDING_STYLE_HASH__"']) {
  if (!source.includes(placeholder)) throw new Error(`${placeholder} is missing from src/index.mjs`);
}
// 两个文件合成一个模块后，顶层同名声明会在部署时才报错；这里先把两边的顶层名字取出来比对。
const topLevel = text => new Set([...text.matchAll(/^(?:export\s+)?(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)/gm)].map(m => m[1]));
const clash = [...topLevel(landing)].filter(name => topLevel(source).has(name));
if (clash.length) throw new Error(`src/landing.mjs 与 src/index.mjs 有同名顶层声明：${clash.join(", ")}`);
// 一律用函数做替换：字符串替换里 $& $' $` 之类序列会被当成特殊写法，文件内容一变就悄悄出错。
const inlined = landing.replace("export function landingPage", () => "function landingPage");
const dmgLiteral = dmg.bytes === null ? JSON.stringify("__CURRENT_DMG_BYTES__") : String(dmg.bytes);
const built = source
  .replace(importLine, () => inlined + "\n")
  .replaceAll('"__CURRENT_VERSION__"', () => JSON.stringify(version))
  .replaceAll('"__CURRENT_DMG_BYTES__"', () => dmgLiteral);
if (built.includes("./landing.mjs")) throw new Error("构建产物里仍有对 landing.mjs 的引用");

// 静态素材：先清掉上一次构建留下的 assets，再整体复制 public/，避免旧文件残留进线上。
await fs.mkdir(output, { recursive: true });
await fs.rm(join(output, "assets"), { recursive: true, force: true });
await fs.cp(resolve(root, "public"), output, { recursive: true });
await fs.writeFile(join(output, "assets", "app-icon.png"), Buffer.from(icons.light, "base64"));

// 内容指纹：文件名不变，引用处追加 ?v=<内容哈希前 10 位>。_headers 给 /assets/* 配了一年的 immutable，
// 素材一变哈希就变、地址就变，用户不会拿到旧图；不改文件名则已经打开着的旧页面引用的地址仍然有效。
const assetsDir = join(output, "assets");
const fingerprint = async file => createHash("sha256").update(await fs.readFile(file)).digest("hex").slice(0, 10);
const hashes = {};
for (const name of await fs.readdir(assetsDir)) {
  if (name !== "landing.css") hashes[name] = await fingerprint(join(assetsDir, name));
}
// 样式表自己引用了别的素材（应用图标）：先把这些引用改成带指纹的，再算样式表自己的指纹。
const stylePath = join(assetsDir, "landing.css");
const styled = (await fs.readFile(stylePath, "utf8")).replace(/url\(\/assets\/([A-Za-z0-9._-]+)\)/g, (_all, name) => {
  if (!hashes[name]) throw new Error(`landing.css 引用了不存在的素材：${name}`);
  return `url(/assets/${name}?v=${hashes[name]})`;
});
await fs.writeFile(stylePath, styled);
hashes["landing.css"] = await fingerprint(stylePath);
// 样式表内联进页面：首次绘制不用再等一次样式表的往返。landing.css 仍随产物发布（旧页面缓存里的引用不会 404），新页面不再引用它。
// 内联的是改写（带指纹）之后的样式文本，它的 sha256 写进首页的样式策略；文本放进模板字符串前要转义反斜杠、反引号和 ${。
const STYLE_LINK = '<link rel="stylesheet" href="/assets/landing.css">';
if (built.split(STYLE_LINK).length !== 2) throw new Error("页面模板里应恰好有一处样式表外链");
if (/<\/style/i.test(styled)) throw new Error("样式表里不能出现 </style，否则会提前结束内联样式");
const escapedCss = styled.replace(/\\/g, "\\\\").replace(/`/g, "\\`").replace(/\$\{/g, "\\${");
const styleHash = "sha256-" + createHash("sha256").update(styled, "utf8").digest("base64");
const inlineBuilt = built
  .replace(STYLE_LINK, () => `<style>${escapedCss}</style>`)
  .replace('"__LANDING_STYLE_HASH__"', () => JSON.stringify(styleHash));
// 页面模板里的引用同样改写；引用到 public/ 里没有的文件，宁可构建失败也不要上线一个 404。
const versioned = inlineBuilt.replace(/\/assets\/([A-Za-z0-9._-]+\.(?:css|js|svg|png|webp|webm|mov|mp4))(?=["\s,])/g, (_all, name) => {
  if (!hashes[name]) throw new Error(`页面模板引用了不存在的素材：${name}`);
  return `/assets/${name}?v=${hashes[name]}`;
});
await fs.writeFile(join(output, "_worker.js"), versioned);
// 非视频素材不进 Worker：由 Pages 直接提供静态文件，省掉 Worker 调用，也不会被当成 404。
// 视频故意不在排除名单里：Pages 的静态层对 Range 请求一律回 200 全量，Safari 播不了，
// 所以视频由 Worker 切片回 206（src/index.mjs 的 serveVideo）。名单按产物目录里的实际文件逐个生成，
// 新增的非视频素材自动排除，新增的视频自动进 Worker，不靠人记得改。
// 扩展名要与 src/index.mjs 的 VIDEO_TYPES 一致，由 tests/build-pages.test.mjs 对拍。
const VIDEO_EXT = /\.(?:webm|mov|mp4)$/i;
const staticOnly = Object.keys(hashes).filter(name => !VIDEO_EXT.test(name)).sort().map(name => `/assets/${name}`);
// Pages 的路由规则最多 100 条；留余量，超了宁可构建失败，也不要部署一个被平台拒收的路由文件。
if (staticOnly.length > 80) throw new Error(`_routes.json 排除名单有 ${staticOnly.length} 条，接近平台 100 条上限，需要改成别的分流办法`);
await fs.writeFile(join(output, "_routes.json"),
  JSON.stringify({ version: 1, include: ["/*"], exclude: staticOnly }) + "\n");
console.log(JSON.stringify({ pagesOutput: output, version, dmgBytes: dmg.bytes, dmgSource: dmg.path,
  workerBytes: Buffer.byteLength(versioned), assetHashes: hashes }));
