import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  DEVELOPMENT_BRANCH, SITE_PATHSPEC, assetDrift, cleanBuildEnv, deployProvenanceArgs, dirtySiteInputs,
  dirtyPageInputs, isSiteInput, liveAssetStamps, provenanceProblems, readbackCacheHit, shipProblems, stampOf, syncVerdict,
  findPreviewForPage, journalVerdict, pageFingerprint, shouldRetryReadback, sourceDigest, stampOfPage, stampOfTool, toolFingerprint,
} from "../site-inputs.mjs";

const SITE = "services/download-analytics";
const FP = "0123456789abcdef";
const blobId = ch => ch.repeat(40);

test("the development branch and the site pathspec are the agreed values", () => {
  assert.equal(DEVELOPMENT_BRANCH, "feat/target-price");
  assert.deepEqual(SITE_PATHSPEC, [SITE, "sf_constants.py", "sf_icons.py", "sf_instance.py", "sf_logging.py", "sf_privacy.py"]);
});

test("every repo-local Python module export-icon.py pulls in is a site input", () => {
  // 名单靠人维护，回答不了"还有第几份没进去"；这里从 export-icon.py 出发按 import 逐层推算，漏一份就红。
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
  const importsOf = file => {
    const found = new Set();
    for (const match of readFileSync(file, "utf8").matchAll(/^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)[\w.]*[ \t]+import\b|import[ \t]+([A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*))/gm)) {
      for (const name of (match[1] ? [match[1]] : match[2].split(","))) found.add(name.trim());
    }
    return [...found].filter(name => existsSync(resolve(repoRoot, `${name}.py`)));
  };
  const closure = new Set();
  const queue = importsOf(resolve(repoRoot, SITE, "export-icon.py"));
  while (queue.length) {
    const name = queue.pop();
    if (closure.has(name)) continue;
    closure.add(name);
    queue.push(...importsOf(resolve(repoRoot, `${name}.py`)));
  }
  assert.ok(closure.has("sf_icons") && closure.size >= 4, `推算出的依赖少得可疑：${[...closure]}（取不到输入不能当作通过）`);
  for (const name of closure) {
    assert.ok(SITE_PATHSPEC.includes(`${name}.py`) && isSiteInput(`${name}.py`), `${name}.py 会影响导出的图标，必须在站点输入名单里`);
  }
});

test("a git-ignored file that build-pages would copy into the deploy is reported as dirty", () => {
  const stray = `${SITE}/public/assets/.DS_Store`;
  assert.deepEqual(dirtySiteInputs(porcelain(`!! ${stray}`, `!! ${SITE}/node_modules/`, `!! ${SITE}/dist/`, `!! ${SITE}/.wrangler/`)), [stray],
    "被忽略的 .DS_Store 会被整体复制进部署包，必须挡住；node_modules、dist、.wrangler 不进部署包，不挡");
});

test("every pathspec entry is something isSiteInput also counts as an input", () => {
  // pathspec 决定 git 去哪里取内容，isSiteInput 决定哪些算输入；两边对不上，指纹就会漏算或多算。
  for (const entry of SITE_PATHSPEC) {
    const sample = entry.includes("/") ? `${entry}/src/index.mjs` : entry;
    assert.equal(isSiteInput(sample), true, `${sample} 在 pathspec 里，必须算站点输入`);
  }
});

test("site source, public assets, build scripts and package.json are inputs", () => {
  for (const path of [`${SITE}/src/index.mjs`, `${SITE}/src/pages/landing.html`, `${SITE}/public/assets/landing.js`,
    `${SITE}/public/assets/中文图.png`, `${SITE}/build-pages.mjs`, `${SITE}/deploy-pages.mjs`, `${SITE}/pages-control.mjs`,
    `${SITE}/package.json`, `${SITE}/export-icon.py`, `${SITE}/wrangler.jsonc`, `${SITE}/site-inputs.mjs`]) {
    assert.equal(isSiteInput(path), true, `${path} 决定页面内容，必须算输入`);
  }
});

test("docs, tests, asset sources and build output are not inputs", () => {
  for (const path of [`${SITE}/README.md`, `${SITE}/ASSETS.md`, `${SITE}/tests/site-inputs.test.mjs`, `${SITE}/tests/fixtures/a.json`,
    `${SITE}/asset-sources/icon.svg`, `${SITE}/dist/index.html`, `${SITE}/dist/assets/a.js`, `${SITE}/node_modules/x/index.js`,
    `${SITE}/.wrangler/state/v3/d1/db.sqlite`, `${SITE}/__pycache__/export-icon.cpython-314.pyc`]) {
    assert.equal(isSiteInput(path), false, `${path} 不进页面，改它不该逼人重新预览`);
  }
});

test("names that merely start like an excluded directory or doc are still inputs", () => {
  // 排除规则只认整段目录名与确切的文档名；前缀相同不等于同一个东西。
  for (const path of [`${SITE}/tests.mjs`, `${SITE}/distillery.mjs`, `${SITE}/README.md.bak`, `${SITE}/ASSETS.mdx`,
    `${SITE}/asset-sources-index.mjs`, `${SITE}/node_modules.json`, `${SITE}/src/tests/helper.mjs`]) {
    assert.equal(isSiteInput(path), true, `${path} 只是名字像被排除的东西，仍是输入`);
  }
});

test("the three python modules behind export-icon.py are inputs", () => {
  for (const path of ["sf_constants.py", "sf_icons.py", "sf_logging.py"]) {
    assert.equal(isSiteInput(path), true, `${path} 被 export-icon.py 间接 import，必须算输入`);
  }
});

test("other repository files are not inputs", () => {
  for (const path of ["stock_float_window.py", "outputs/x.md", "services/feedback-worker/a.mjs", "README.md", "tests/test_core.py",
    "docs/DOWNLOAD_SITE_WORKFLOW.md", "sf_constants.pyc", "sub/sf_constants.py", "sf_icons.py.bak", ""]) {
    assert.equal(isSiteInput(path), false, `${JSON.stringify(path)} 不在站点范围内`);
  }
});

test("look-alike directory names do not count as the site", () => {
  for (const path of ["services/download-analytics-old/a.mjs", "services/download-analytics.mjs", "xservices/download-analytics/a.mjs",
    "services/download-analytic/a.mjs", "services/download-analytics"]) {
    assert.equal(isSiteInput(path), false, `${path} 不是下载站目录下的文件`);
  }
});

const porcelain = (...records) => records.join("\0") + "\0";

test("dirtySiteInputs keeps site files and drops everything else", () => {
  const out = dirtySiteInputs(porcelain(` M ${SITE}/src/index.mjs`, `?? stock_float_window.py`, `A  ${SITE}/README.md`,
    `M  sf_icons.py`, ` M ${SITE}/tests/a.test.mjs`, `?? outputs/x.md`, ` M ${SITE}/public/assets/a.js`));
  assert.deepEqual(out, [`${SITE}/public/assets/a.js`, `${SITE}/src/index.mjs`, "sf_icons.py"]);
});

test("dirtySiteInputs returns an empty array when nothing relevant changed", () => {
  for (const input of ["", "\0", undefined, null, porcelain(" M stock_float_window.py", `?? ${SITE}/README.md`)]) {
    assert.deepEqual(dirtySiteInputs(input), [], `输入 ${JSON.stringify(input)} 没有站点改动`);
  }
});

test("dirtySiteInputs returns non-ASCII paths verbatim", () => {
  // -z 输出不转义中文与空格；这里原样返回才能直接拿去提示用户。
  const path = `${SITE}/public/assets/中文 图标.png`;
  assert.deepEqual(dirtySiteInputs(porcelain(`?? ${path}`)), [path]);
});

test("dirtySiteInputs considers both paths of a rename", () => {
  const into = dirtySiteInputs(porcelain(`R  ${SITE}/src/new.mjs`, "old-name.txt"));
  assert.deepEqual(into, [`${SITE}/src/new.mjs`], "改名进站点：新路径要算");
  const outOf = dirtySiteInputs(porcelain(`R  notes/new.txt`, `${SITE}/src/old.mjs`));
  assert.deepEqual(outOf, [`${SITE}/src/old.mjs`], "改名出站点：原路径也要算，否则搬走一个文件就查不出来");
  const inside = dirtySiteInputs(porcelain(`R  ${SITE}/src/b.mjs`, `${SITE}/src/a.mjs`));
  assert.deepEqual(inside, [`${SITE}/src/a.mjs`, `${SITE}/src/b.mjs`]);
});

test("dirtySiteInputs treats a copy record like a rename", () => {
  const out = dirtySiteInputs(porcelain(`C  ${SITE}/src/copy.mjs`, `${SITE}/src/orig.mjs`));
  assert.deepEqual(out, [`${SITE}/src/copy.mjs`, `${SITE}/src/orig.mjs`]);
});

test("a rename's original path is consumed with its record and not read as a status line", () => {
  // 原路径 token 没有状态码；若不跳过它，会把前三个字符当状态码吃掉，路径就被截断。
  const out = dirtySiteInputs(porcelain(`R  ${SITE}/src/new.mjs`, `${SITE}/src/old.mjs`, ` M ${SITE}/build-pages.mjs`));
  assert.deepEqual(out, [`${SITE}/build-pages.mjs`, `${SITE}/src/new.mjs`, `${SITE}/src/old.mjs`]);
});

test("dirtySiteInputs de-duplicates repeated paths and sorts the result", () => {
  const out = dirtySiteInputs(porcelain(` M ${SITE}/src/b.mjs`, `M  ${SITE}/src/b.mjs`, `?? ${SITE}/src/a.mjs`,
    ` M sf_icons.py`, `MM ${SITE}/src/a.mjs`, ` M ${SITE}/package.json`));
  assert.deepEqual(out, [`${SITE}/package.json`, `${SITE}/src/a.mjs`, `${SITE}/src/b.mjs`, "sf_icons.py"],
    "同一文件暂存和未暂存各一条，只报一次");
});

const entry = (path, { mode = "100644", type = "blob", sha = blobId("a") } = {}) => `${mode} ${type} ${sha}\t${path}`;
const tree = records => records.join("\0") + "\0";
const BASE = [
  entry(`${SITE}/src/index.mjs`, { sha: blobId("a") }),
  entry(`${SITE}/build-pages.mjs`, { mode: "100755", sha: blobId("b") }),
  entry(`${SITE}/public/assets/中文图.png`, { sha: blobId("c") }),
  entry("sf_icons.py", { sha: blobId("d") }),
  entry(`${SITE}/README.md`, { sha: blobId("e") }),
  entry("stock_float_window.py", { sha: blobId("f") }),
];
const withEntry = (index, replacement) => BASE.map((record, i) => (i === index ? replacement : record));

test("entries that are not blobs are ignored", () => {
  const before = pageFingerprint(tree(BASE));
  const submodule = entry(`${SITE}/vendor`, { mode: "160000", type: "commit", sha: blobId("7") });
  const subtree = entry(`${SITE}/src`, { mode: "040000", type: "tree", sha: blobId("8") });
  assert.equal(pageFingerprint(tree([...BASE, submodule, subtree])), before, "commit/tree 类型的条目不参与计算");
  const sameNameAsCommit = withEntry(0, entry(`${SITE}/src/index.mjs`, { mode: "160000", type: "commit", sha: blobId("a") }));
  assert.notEqual(pageFingerprint(tree(sameNameAsCommit)), before, "把 blob 换成 commit 等于少了一个页面文件");
});

test("stampOf extracts a 16-character lowercase hex stamp from a commit message", () => {
  assert.equal(stampOf(`Preview site:${FP}`), FP);
  assert.equal(stampOf(`site:${FP}`), FP, "整条说明就是戳");
  assert.equal(stampOf(`Ship downloads page site:${FP}\n\nbody text`), FP, "戳后面跟换行");
  assert.equal(stampOf(`first line\nsite:${FP} trailing words`), FP, "戳在第二行行首");
  assert.equal(stampOf(`a\tsite:${FP}`), FP, "戳前面是制表符");
});

test("stampOf returns an empty string when there is no valid stamp", () => {
  const bad = {
    "没有戳": "Preview of the download page",
    "15 位": "site:0123456789abcde",
    "17 位": "site:0123456789abcdef0",
    "大写": "site:0123456789ABCDEF",
    "夹在别的单词里": `xsite:${FP}`,
    "后面紧跟字母": `site:${FP}z`,
    "非十六进制": "site:0123456789abcdeg",
    "冒号后为空": "site:",
    "空串": "",
    "前缀不同": `sites:${FP}`,
  };
  for (const [why, message] of Object.entries(bad)) assert.equal(stampOf(message), "", `${why}：${JSON.stringify(message)} 不该取出戳`);
  for (const empty of [undefined, null]) assert.equal(stampOf(empty), "", "缺失的说明按没有戳处理");
});

const OK = { name: "deploy", status: "success" };
const TOOL = "fedcba9876543210";
const preview = (id, stamp, extra = {}) => ({
  id, environment: "preview", latest_stage: OK,
  deployment_trigger: { metadata: { commit_message: `Preview page:${stamp} tool:${TOOL}` } }, ...extra,
});

test("a rejected deployment does not hide a valid one behind it", () => {
  const good = preview("good", FP);
  assert.equal(findPreviewForPage([preview("x", FP, { environment: "production" }), preview("y", FP, { latest_stage: { name: "deploy", status: "failure" } }), good], FP), good);
});

const GOOD = () => ({ branch: DEVELOPMENT_BRANCH, dirty: [], localVersion: "1.4.105", liveVersion: "1.4.105", previewDeploymentId: "abc", fingerprint: FP, testsPassed: true });
const only = (state, pattern, why) => {
  const problems = shipProblems(state);
  assert.equal(problems.length, 1, `${why}：应当恰好一条，实际 ${JSON.stringify(problems)}`);
  assert.match(problems[0], pattern, why);
  return problems[0];
};

test("a fully qualified state has no ship problems", () => {
  assert.deepEqual(shipProblems(GOOD()), []);
  assert.deepEqual(shipProblems({ ...GOOD(), dirty: undefined }), [], "dirty 缺失按没有未提交文件处理");
});

test("shipping from any other branch is the only problem when the branch is wrong", () => {
  const message = only({ ...GOOD(), branch: "main" }, /ship only from feat\/target-price/, "分支不对");
  assert.match(message, /on main/);
  assert.doesNotMatch(message, /detached/);
  only({ ...GOOD(), branch: "feat/target-price-2" }, /ship only from/, "名字相近的分支也不行");
});

test("a detached HEAD is reported as such and is the only problem", () => {
  for (const branch of ["", undefined, null]) {
    assert.match(only({ ...GOOD(), branch }, /ship only from/, "游离 HEAD"), /a detached HEAD/);
  }
});

test("uncommitted site files are the only problem when dirty is not empty", () => {
  const message = only({ ...GOOD(), dirty: [`${SITE}/src/a.mjs`, `${SITE}/src/b.mjs`] }, /uncommitted site files/, "有未提交文件");
  assert.ok(message.includes(`${SITE}/src/a.mjs, ${SITE}/src/b.mjs`), "列出文件名");
  assert.doesNotMatch(message, /more/, "不超过 5 条时不写 and N more");
});

test("the uncommitted list is cut after five names with a count of the rest", () => {
  const names = ["a", "b", "c", "d", "e", "f", "g", "h"].map(n => `${SITE}/src/${n}.mjs`);
  const five = only({ ...GOOD(), dirty: names.slice(0, 5) }, /uncommitted/, "恰好 5 条");
  assert.doesNotMatch(five, /more/, "恰好 5 条不截断");
  const six = only({ ...GOOD(), dirty: names.slice(0, 6) }, /uncommitted/, "6 条");
  assert.match(six, / and 1 more$/);
  assert.ok(!six.includes("f.mjs"), "第 6 条只计数，不列名");
  const eight = only({ ...GOOD(), dirty: names }, /uncommitted/, "8 条");
  assert.match(eight, / and 3 more$/);
  assert.ok(eight.includes("e.mjs") && !eight.includes("f.mjs"));
});

test("a version difference between source and live site is the only problem when versions differ", () => {
  const message = only({ ...GOOD(), localVersion: "1.4.106" }, /site source says 1\.4\.106 but the live site serves 1\.4\.105/, "本地比线上新");
  assert.match(message, /release is in flight/);
  only({ ...GOOD(), liveVersion: "1.4.106" }, /site source says 1\.4\.105 but the live site serves 1\.4\.106/, "线上比本地新");
});

test("a missing preview is the only problem and names the fingerprint", () => {
  for (const previewDeploymentId of [null, undefined, ""]) {
    const message = only({ ...GOOD(), previewDeploymentId }, /no successful preview of exactly this page content/, "没有预览");
    assert.ok(message.includes(`fingerprint ${FP}`), "消息里要带指纹，才知道缺的是哪一份内容的预览");
  }
});

test("failing tests are the only problem when the tests did not pass", () => {
  for (const testsPassed of [false, undefined, null, 0]) only({ ...GOOD(), testsPassed }, /tests are not all green/, "测试没过");
});

test("every failing condition is reported together", () => {
  const problems = shipProblems({ branch: "main", dirty: ["x"], localVersion: "2", liveVersion: "1", previewDeploymentId: null, fingerprint: FP, testsPassed: false });
  assert.equal(problems.length, 5, JSON.stringify(problems));
});

test("shipProblems does not modify the state it is given", () => {
  const state = { ...GOOD(), dirty: ["a", "b", "c", "d", "e", "f", "g"] };
  const snapshot = JSON.stringify(state);
  shipProblems(state);
  assert.equal(JSON.stringify(state), snapshot);
});

const HASH = "0123456789abcdef0123456789abcdef01234567";
const MESSAGE = `Preview page:${FP} tool:${TOOL}`;

test("without provenance variables the deploy keeps the release-chain behaviour", () => {
  assert.deepEqual(deployProvenanceArgs({}), ["--commit-dirty=true"]);
  assert.deepEqual(deployProvenanceArgs({ PATH: "/usr/bin", HOME: "/tmp" }), ["--commit-dirty=true"], "无关变量不影响");
  assert.deepEqual(deployProvenanceArgs({ SF_DEPLOY_COMMIT_HASH: "", SF_DEPLOY_COMMIT_MESSAGE: "" }), ["--commit-dirty=true"], "两个都是空串等于没设");
});

test("both provenance variables, when valid, become wrangler commit arguments", () => {
  const args = deployProvenanceArgs({ SF_DEPLOY_COMMIT_HASH: HASH, SF_DEPLOY_COMMIT_MESSAGE: MESSAGE });
  assert.deepEqual(args, ["--commit-hash", HASH, "--commit-message", MESSAGE, "--commit-dirty=false"]);
  assert.ok(!args.includes("--commit-dirty=true"));
});

test("setting only one provenance variable is refused", () => {
  const together = /go together/;
  assert.throws(() => deployProvenanceArgs({ SF_DEPLOY_COMMIT_HASH: HASH }), together, "只有哈希");
  assert.throws(() => deployProvenanceArgs({ SF_DEPLOY_COMMIT_MESSAGE: MESSAGE }), together, "只有说明");
  assert.throws(() => deployProvenanceArgs({ SF_DEPLOY_COMMIT_HASH: HASH, SF_DEPLOY_COMMIT_MESSAGE: "" }), together, "说明是空串");
  assert.throws(() => deployProvenanceArgs({ SF_DEPLOY_COMMIT_HASH: "", SF_DEPLOY_COMMIT_MESSAGE: MESSAGE }), together, "哈希是空串");
});

test("a message without both page and tool stamps is refused", () => {
  for (const bad of [`Preview site:${FP}`, `Preview page:${FP}`, `Preview tool:${TOOL}`, `Preview page:${FP} tool:${TOOL.slice(1)}`, ""]) {
    assert.throws(() => deployProvenanceArgs({ SF_DEPLOY_COMMIT_HASH: HASH, SF_DEPLOY_COMMIT_MESSAGE: bad }), /go together/,
      `说明 ${JSON.stringify(bad)} 缺 page 或 tool 戳，必须拒绝`);
  }
});

test("a commit hash that is not 40 lowercase hex characters is refused", () => {
  for (const bad of ["HEAD", "0123456789abcdef", HASH.slice(1), `${HASH}0`, HASH.toUpperCase(), `${HASH.slice(0, 39)}g`, ` ${HASH}`, "main; rm -rf /"]) {
    assert.throws(() => deployProvenanceArgs({ SF_DEPLOY_COMMIT_HASH: bad, SF_DEPLOY_COMMIT_MESSAGE: MESSAGE }), /go together/,
      `不合规的哈希 ${JSON.stringify(bad)} 必须被拒绝，不能原样交给 wrangler`);
  }
});

test("a commit message without a valid site stamp is refused", () => {
  for (const bad of ["Preview of the site", "site:0123456789abcde", `site:${FP.toUpperCase()}`, `xsite:${FP}`, "   "]) {
    assert.throws(() => deployProvenanceArgs({ SF_DEPLOY_COMMIT_HASH: HASH, SF_DEPLOY_COMMIT_MESSAGE: bad }), /go together/,
      `说明 ${JSON.stringify(bad)} 里没有合规的 site: 戳，上线后就无法对账`);
  }
});

const PAGE = `<link href="/assets/landing.css?v=cccccccccc"><script src="/assets/a.js?v=0123456789"></script>
<img src="/assets/中.png?v=ffffffffff"><img src="/assets/b-1_x.svg?v=aaaaaaaaaa"><script src="/assets/a.js?v=0123456789"></script>`;

test("liveAssetStamps reads asset names and ten-character stamps from the page", () => {
  const stamps = liveAssetStamps(PAGE);
  assert.ok(stamps instanceof Map);
  assert.deepEqual([...stamps.entries()].sort(), [["a.js", "0123456789"], ["b-1_x.svg", "aaaaaaaaaa"], ["landing.css", "cccccccccc"]],
    "同一素材出现两次只记一条；中文名不在允许的字符里，不识别");
});

test("liveAssetStamps ignores references that are not versioned assets", () => {
  const html = `<a href="/other/a.js?v=0123456789"><script src="/assets/plain.js"></script><script src="/assets/up.js?v=ABCDEFABCD"></script>
<script src="/assets/short.js?v=abc"></script><script src="/assets/x.js?ver=0123456789"></script>`;
  assert.equal(liveAssetStamps(html).size, 0);
  assert.equal(liveAssetStamps("").size, 0);
  assert.equal(liveAssetStamps(undefined).size, 0);
});

const LOCAL = { "a.js": "0123456789", "b-1_x.svg": "aaaaaaaaaa", "landing.css": "cccccccccc" };
const NO_DRIFT = { changed: [], onlyLive: [], notYetLive: [] };

test("assetDrift is empty when the page and the local build agree", () => {
  assert.deepEqual(assetDrift(LOCAL, PAGE), NO_DRIFT);
});

test("assetDrift reports an asset whose stamp differs as changed", () => {
  const local = { ...LOCAL, "a.js": "9999999999", "b-1_x.svg": "8888888888" };
  assert.deepEqual(assetDrift(local, PAGE), { changed: ["a.js", "b-1_x.svg"], onlyLive: [], notYetLive: [] });
});

test("assetDrift reports assets the page references but the local build lacks as onlyLive", () => {
  const local = { "a.js": LOCAL["a.js"], "landing.css": LOCAL["landing.css"] };
  assert.deepEqual(assetDrift(local, PAGE), { changed: [], onlyLive: ["b-1_x.svg"], notYetLive: [] });
});

test("assetDrift matches names exactly and does not see inherited object properties", () => {
  const html = `<script src="/assets/constructor?v=0123456789"></script><script src="/assets/toString?v=0123456789"></script>`;
  assert.deepEqual(assetDrift({}, html), { changed: [], onlyLive: ["constructor", "toString"], notYetLive: [] });
});

test("assetDrift reports local assets the page does not reference yet as notYetLive", () => {
  const local = { ...LOCAL, "new.js": "1111111111", "extra.svg": "2222222222" };
  assert.deepEqual(assetDrift(local, PAGE), { changed: [], onlyLive: [], notYetLive: ["extra.svg", "new.js"] });
});

test("landing.css is published for old caches and is never counted as notYetLive", () => {
  const withoutCss = `<script src="/assets/a.js?v=0123456789"></script>`;
  assert.deepEqual(assetDrift({ "a.js": "0123456789", "landing.css": "cccccccccc" }, withoutCss), NO_DRIFT, "本地有、页面不引用，不算漂移");
  assert.deepEqual(assetDrift({ "a.js": "0123456789", "landing.css": "cccccccccc", "other.css": "dddddddddd" }, withoutCss).notYetLive, ["other.css"],
    "只豁免 landing.css 这一个名字");
  assert.deepEqual(assetDrift({ "landing.css": "1111111111" }, `<link href="/assets/landing.css?v=cccccccccc">`).changed, ["landing.css"],
    "页面若真的引用了它，哈希不同仍然是 changed");
});

test("assetDrift sorts every list and reports all three kinds at once", () => {
  const html = `<i src="/assets/z.js?v=0000000000"><i src="/assets/m.js?v=1111111111"><i src="/assets/live-b.js?v=3333333333"><i src="/assets/live-a.js?v=4444444444">`;
  const local = { "z.js": "9999999999", "m.js": "8888888888", "y.js": "5555555555", "b.js": "6666666666" };
  assert.deepEqual(assetDrift(local, html), { changed: ["m.js", "z.js"], onlyLive: ["live-a.js", "live-b.js"], notYetLive: ["b.js", "y.js"] });
});

const aligned = { drift: NO_DRIFT, fingerprint: FP };

test("assets that differ from the live page are drift, whatever the stamp says", () => {
  for (const stamped of [FP, "fedcba9876543210", ""]) {
    for (const [kind, drift] of Object.entries({
      changed: { ...NO_DRIFT, changed: ["a.js"] }, notYetLive: { ...NO_DRIFT, notYetLive: ["b.js"] }, onlyLive: { ...NO_DRIFT, onlyLive: ["c.js"] },
    })) {
      const verdict = syncVerdict({ drift, stamped, fingerprint: FP });
      assert.equal(verdict.state, "drift", `${kind} 有差异 stamped=${JSON.stringify(stamped)}`);
      assert.match(verdict.text, /assets differ from the live page/);
      assert.doesNotMatch(verdict.text, /in sync/, "有漂移时不能说 in sync");
    }
  }
  const all = syncVerdict({ drift: { changed: ["a.js"], notYetLive: ["b.js"], onlyLive: ["c.js"] }, stamped: FP, fingerprint: FP });
  assert.match(all.text, /changed: a\.js; not live yet: b\.js; live only: c\.js/);
});

test("a stamp from different site content is drift even when every asset matches", () => {
  const verdict = syncVerdict({ ...aligned, stamped: "fedcba9876543210" });
  assert.equal(verdict.state, "drift");
  assert.ok(verdict.text.includes("fedcba9876543210") && verdict.text.includes(FP), "要同时写出线上的戳和本地的指纹");
  assert.doesNotMatch(verdict.text, /in sync/);
});

test("the same stamp with no asset drift is in sync", () => {
  const verdict = syncVerdict({ ...aligned, stamped: FP });
  assert.equal(verdict.state, "in-sync");
  assert.match(verdict.text, /^in sync:/);
});

test("matching assets without a content stamp never claim to be in sync", () => {
  for (const stamped of ["", undefined, null]) {
    const verdict = syncVerdict({ ...aligned, stamped });
    assert.equal(verdict.state, "assets-match-unstamped", `stamped=${JSON.stringify(stamped)}`);
    assert.doesNotMatch(verdict.text, /in sync/i, "没有戳就证明不了纯文字改动已上线，不能声称一致");
    assert.match(verdict.text, /no content stamp/);
  }
});

test("an asset referenced with two different stamps is drift even when one of them matches", () => {
  const html = '<script src="/assets/a.js?v=0123456789"></script><img srcset="/assets/a.js?v=aaaaaaaaaa 1x">';
  for (const local of ["0123456789", "aaaaaaaaaa"]) {
    assert.deepEqual(assetDrift({ "a.js": local }, html).changed, ["a.js"],
      `页面上同一素材的两个戳不一致，本地哈希 ${local} 与其中一个相等也不能算一致`);
  }
});

test("a stamp followed by more hex digits is not a ten-character stamp", () => {
  const html = '<script src="/assets/a.js?v=0123456789ab"></script>';
  assert.equal(liveAssetStamps(html).has("a.js"), false, "截前十位会把一个不是本构建写出的戳当成对得上");
  const drift = assetDrift({ "a.js": "0123456789" }, html);
  assert.deepEqual(drift.changed, []);
  assert.deepEqual(drift.notYetLive, ["a.js"], "认不出戳的素材按尚未上线算，结论仍是漂移");
});

test("provenanceProblems is empty only when HEAD, tree and both fingerprints are what ship checked", () => {
  const expected = { commit: "a".repeat(40), pageFingerprint: FP, toolFingerprint: TOOL };
  const state = (extra = {}) => ({ commit: expected.commit, dirty: [], pageFingerprint: FP, toolFingerprint: TOOL, ...extra });
  assert.deepEqual(provenanceProblems(state(), expected), []);
  assert.equal(provenanceProblems(state({ commit: "b".repeat(40) }), expected).length, 1, "HEAD 动了必须拦");
  assert.equal(provenanceProblems(state({ dirty: [`${SITE}/src/landing.mjs`] }), expected).length, 1,
    "检查之后出现未提交的站点文件必须拦（部署读的是工作区）");
  assert.equal(provenanceProblems(state({ pageFingerprint: "0123456789abcdea" }), expected).length, 1, "页面指纹变了必须拦");
  assert.equal(provenanceProblems(state({ toolFingerprint: "0123456789abcdea" }), expected).length, 1, "工具指纹变了必须拦");
  assert.deepEqual(provenanceProblems({ commit: expected.commit, pageFingerprint: FP, toolFingerprint: TOOL }, expected), [],
    "dirty 缺省按没有处理");
});

test("cleanBuildEnv drops the variables that redirect or reshape the build and keeps the rest", () => {
  const env = { PATH: "/usr/bin", HOME: "/h", SF_PAGES_OUT: "/tmp/elsewhere", SF_APP_VERSION: "9.9.9", SF_DMG_PATH: "/tmp/x.dmg", SF_PYTHON: "/p/python" };
  const clean = cleanBuildEnv(env);
  for (const key of ["SF_PAGES_OUT", "SF_APP_VERSION", "SF_DMG_PATH"]) assert.equal(key in clean, false, `${key} 会改变构建产出，不能继承`);
  assert.deepEqual(clean, { PATH: "/usr/bin", HOME: "/h", SF_PYTHON: "/p/python" });
  assert.equal(env.SF_PAGES_OUT, "/tmp/elsewhere", "不能改动传入的对象");
});

test("a checkout with uncommitted site files never reports in-sync", () => {
  const dirty = [`${SITE}/src/landing.mjs`];
  for (const stamped of [FP, ""]) {
    const verdict = syncVerdict({ ...aligned, stamped, dirty });
    assert.equal(verdict.state, "dirty-checkout", `stamped=${JSON.stringify(stamped)}`);
    assert.doesNotMatch(verdict.text, /^in sync/);
    assert.ok(verdict.text.includes("landing.mjs"), "要点名是哪个文件");
  }
  assert.equal(syncVerdict({ ...aligned, stamped: FP, dirty: [] }).state, "in-sync", "没有未提交文件时照旧");
  assert.equal(syncVerdict({ drift: { ...NO_DRIFT, changed: ["a.js"] }, stamped: FP, fingerprint: FP, dirty }).state, "drift", "真漂移优先于未提交提示");
});

// ---- 2026-10-30 改进批：指纹拆分、部署日志、锁、读回重试 ----

test("the page fingerprint ignores tooling files while the tool fingerprint ignores page files", () => {
  const withTooling = tree([...BASE, entry(`${SITE}/pages-control.mjs`, { sha: blobId("1") })]);
  assert.equal(pageFingerprint(withTooling), pageFingerprint(tree(BASE)), "改工具不该让页面指纹变，否则每次改工具都要重新预览");
  const withPage = tree([...BASE, entry(`${SITE}/src/index.mjs`, { sha: blobId("2") })]);
  assert.notEqual(pageFingerprint(withPage), pageFingerprint(tree(BASE)), "页面内容变了页面指纹必须变");
  assert.throws(() => toolFingerprint(tree(BASE)), /no tool input files found/, "没有任何工具文件时拒绝，不能给空集发免检");
  const t1 = toolFingerprint(tree([entry(`${SITE}/pages-control.mjs`, { sha: blobId("1") })]));
  const t2 = toolFingerprint(tree([entry(`${SITE}/pages-control.mjs`, { sha: blobId("2") })]));
  assert.match(t1, /^[0-9a-f]{16}$/);
  assert.notEqual(t1, t2, "工具内容变了工具指纹必须变");
});

test("page and tool stamps are read separately and do not accept each other", () => {
  assert.equal(stampOfPage(`x page:${FP} tool:${TOOL}`), FP);
  assert.equal(stampOfTool(`x page:${FP} tool:${TOOL}`), TOOL);
  assert.equal(stampOfPage(`x tool:${TOOL}`), "");
  assert.equal(stampOfTool(`x page:${FP}`), "");
  assert.equal(stampOfPage(`xpage:${FP}`), "", "戳必须独立成词，夹在单词里不算");
  assert.equal(stampOfPage(`legacy site:${FP}`), "", "旧 site: 戳不能冒充页面戳");
  assert.equal(stampOfTool(`legacy site:${FP}`), "", "旧 site: 戳也不能冒充工具戳");
});

test("a preview is matched by page content only, never by tooling", () => {
  const previews = [
    { id: "aaaaaaaa-0000-0000-0000-000000000001", environment: "preview", latest_stage: { name: "deploy", status: "success" }, deployment_trigger: { metadata: { commit_message: `p page:${FP} tool:${TOOL}` } } },
    { id: "bbbbbbbb-0000-0000-0000-000000000002", environment: "preview", latest_stage: { name: "deploy", status: "success" }, deployment_trigger: { metadata: { commit_message: `p page:${FP} tool:0123456789abcdea` } } },
  ];
  assert.equal(findPreviewForPage(previews, FP)?.id, previews[0].id, "页面内容相同就命中，工具不同不影响");
  assert.equal(findPreviewForPage(previews, "0123456789abcdea"), null);
  assert.throws(() => findPreviewForPage(previews, ""), /page fingerprint/);
});

test("journalVerdict distinguishes an unfinished deploy before and after the switch, and reports outside changes", () => {
  const pending = { phase: "pending", ts: "2026-10-30T10:00:00Z", previous_deployment_id: "prev" };
  const before = journalVerdict([pending], "prev");
  assert.equal(before.state, "interrupted-before-switch");
  assert.equal(before.blocking, false, "还没切换就没事可确认，不该把下一次上线永久卡住");
  const after = journalVerdict([pending], "new");
  assert.equal(after.state, "interrupted-after-switch");
  assert.equal(after.blocking, true);
  const complete = { phase: "complete", deployment_id: "new" };
  assert.equal(journalVerdict([pending, complete], "new").state, "ok");
  assert.equal(journalVerdict([pending, complete], "other").state, "changed-outside-ship");
  assert.equal(journalVerdict([pending, complete], "other").blocking, false, "别的路径部署不是错误，只是提示");
  assert.equal(journalVerdict([], "x").state, "no-record");
  const failed = journalVerdict([{ phase: "failed", ts: "2026-10-30T10:05:00Z", error: "network down" }], "x");
  assert.equal(failed.state, "last-attempt-failed", "失败记录要单独报出来，不能当作没发生");
  assert.match(failed.text, /network down/);
  assert.equal(failed.blocking, false, "失败记录本身不算没写完，不该拦住下一次上线");
});

test("only network-shaped readback failures are retried", () => {
  for (const error of [{ name: "TimeoutError" }, { name: "TypeError", message: "fetch failed" }, { name: "Error", message: "UND_ERR_CONNECT_TIMEOUT" }]) {
    assert.equal(shouldRetryReadback(error), true, JSON.stringify(error));
  }
  for (const error of [{ name: "HttpError", message: "404" }, { name: "TypeError", message: "invalid content-type" }, null]) {
    assert.equal(shouldRetryReadback(error), false, JSON.stringify(error));
  }
});

test("sourceDigest is a short stable digest", () => {
  assert.match(sourceDigest("abc"), /^[0-9a-f]{16}$/);
  assert.equal(sourceDigest("abc"), sourceDigest("abc"));
  assert.notEqual(sourceDigest("abc"), sourceDigest("abd"));
});

test("with a page stamp, tooling changes do not turn a matching page into drift", () => {
  const base = { drift: NO_DRIFT, fingerprint: FP, dirty: [] };
  assert.equal(syncVerdict({ ...base, pageStamp: FP }).state, "in-sync");
  assert.equal(syncVerdict({ ...base, pageStamp: "0123456789abcdea" }).state, "drift");
  const legacy = syncVerdict({ ...base, pageStamp: "", legacyStamp: FP });
  assert.equal(legacy.state, "assets-match-unstamped");
  assert.match(legacy.text, /pre-split/, "旧格式戳要说清是拆分前的戳，不能假装完全一致");
  assert.equal(syncVerdict({ ...base, pageStamp: FP, dirty: [`${SITE}/src/landing.mjs`] }).state, "dirty-checkout");
  assert.equal(syncVerdict({ ...base, pageStamp: "", legacyStamp: "", dirty: [] }).state, "assets-match-unstamped");
});

test("the preview dirty check ignores tooling edits but still blocks page edits", () => {
  const pageEdit = `${SITE}/src/landing.mjs`;
  const toolEdit = `${SITE}/pages-control.mjs`;
  const ignoredPageFile = `${SITE}/public/assets/.DS_Store`;
  assert.deepEqual(dirtyPageInputs(porcelain(` M ${pageEdit}`, ` M ${toolEdit}`, `!! ${ignoredPageFile}`)), [ignoredPageFile, pageEdit],
    "工具改动不该拦住预览；页面改动和会被打进部署包的忽略文件仍要拦");
  assert.deepEqual(dirtyPageInputs(porcelain(` M ${toolEdit}`)), [], "只改工具时预览可以直接跑");
});

test("an unreadable live deployment during a pending journal entry is not reported as a blocking switch", () => {
  const pending = { phase: "pending", ts: "2026-10-31T00:00:00Z", previous_deployment_id: "prev", page_fingerprint: FP };
  const verdict = journalVerdict([pending], null);
  assert.equal(verdict.state, "journal-unverified", "读不到线上编号时只能承认未核验");
  assert.equal(verdict.blocking, false, "不能据此每天把下一轮巡检判成故障");
  assert.match(verdict.text, /could not be read/);
});

test("the readback cache is used only for the same content stamp and only with an ETag", () => {
  const stamp = FP.slice(0, 10);
  const good = { etag: 'W/"abc"', hash: `${FP}${"a".repeat(48)}` };
  assert.equal(readbackCacheHit(good, stamp), true);
  assert.equal(readbackCacheHit(good, "abcdef0123"), false, "地址里的指纹变了就不能拿旧缓存冒充");
  assert.equal(readbackCacheHit({ ...good, etag: "" }, stamp), false, "没有 ETag 就没法发条件请求");
  assert.equal(readbackCacheHit({ ...good, hash: "" }, stamp), false);
  assert.equal(readbackCacheHit(undefined, stamp), false);
});
