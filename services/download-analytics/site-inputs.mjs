// 下载站「敲定到上线」流程里所有不碰网络的判定。pages-control.mjs 与 deploy-pages.mjs 共用，tests/site-inputs.test.mjs 逐条守着。
import { createHash } from "node:crypto";

// 开发主线。只有主线上已经合入的内容才允许上线，否则线上会出现任何分支里都找不到的页面。
export const DEVELOPMENT_BRANCH = "feat/target-price";

// 站点构建输入：这些文件的内容决定线上页面长什么样。export-icon.py 会 import sf_icons，
// sf_icons 又 import sf_constants 与 sf_logging，sf_logging 再 import sf_privacy 与 sf_instance，
// 所以这五份 Python 也算输入（tests/site-inputs.test.mjs 按 import 逐层推算，名单漏一份就红）。
// 说明文档、测试、素材来源说明不进页面，改它们不该逼人重新预览。
const PYTHON_INPUTS = ["sf_constants.py", "sf_icons.py", "sf_instance.py", "sf_logging.py", "sf_privacy.py"];
export const SITE_PATHSPEC = ["services/download-analytics", ...PYTHON_INPUTS];
const EXTRA_INPUTS = new Set(PYTHON_INPUTS);
const SITE_PREFIX = "services/download-analytics/";
const NOT_INPUT = [
  /^services\/download-analytics\/(?:README|ASSETS)\.md$/,
  /^services\/download-analytics\/(?:tests|asset-sources|dist|node_modules|\.wrangler|__pycache__)(?:\/|$)/,
];

export function isSiteInput(path) {
  if (EXTRA_INPUTS.has(path)) return true;
  return path.startsWith(SITE_PREFIX) && !NOT_INPUT.some(pattern => pattern.test(path));
}

// `git status --porcelain -z` 的输出里挑出还没提交的站点输入。-z 让中文路径不被转义，改名记录带两个路径。
export function dirtySiteInputs(porcelainZ) {
  const tokens = String(porcelainZ).split("\0").filter(Boolean);
  const paths = [];
  for (let index = 0; index < tokens.length; index += 1) {
    const entry = tokens[index];
    paths.push(entry.slice(3));
    if (/[RC]/.test(entry.slice(0, 2))) {
      index += 1;
      if (tokens[index]) paths.push(tokens[index]);
    }
  }
  return [...new Set(paths)].filter(isSiteInput).sort();
}

// 预览只关心页面内容：工具脚本未提交不影响预览构建出的页面，没必要因此拒绝预览。
// 上线（ship）仍然要求全部站点输入都已提交，见 dirtySiteInputs。
export function dirtyPageInputs(porcelainZ) {
  return dirtySiteInputs(porcelainZ).filter(isPageInput);
}

export function stampOf(message) {
  return /(?:^|\s)site:([0-9a-f]{16})(?:\s|$)/.exec(String(message || ""))?.[1] || "";
}

// 上线前的全部硬条件。每一条都对应一次真实或可预见的出错方式，说明见 docs/DOWNLOAD_SITE_WORKFLOW.md。
export function shipProblems(state) {
  const problems = [];
  const dirty = state.dirty || [];
  if (state.branch !== DEVELOPMENT_BRANCH) {
    problems.push(`ship only from ${DEVELOPMENT_BRANCH} (this checkout is on ${state.branch || "a detached HEAD"}); merge the finished work first`);
  }
  if (dirty.length) {
    problems.push(`uncommitted site files: ${dirty.slice(0, 5).join(", ")}${dirty.length > 5 ? ` and ${dirty.length - 5} more` : ""}`);
  }
  if (state.localVersion !== state.liveVersion) {
    problems.push(`site source says ${state.localVersion} but the live site serves ${state.liveVersion}: a release is in flight or unpublished, and shipping now would advertise installers that are not online`);
  }
  if (!state.previewDeploymentId) {
    problems.push(`no successful preview of exactly this page content (page fingerprint ${state.fingerprint}); run npm run preview and have it reviewed first`);
  }
  if (!state.testsPassed) problems.push("the download-site tests are not all green");
  return problems;
}

// deploy-pages.mjs 给 wrangler 的出处参数。没设环境变量就保持原样（发版链第 6.1 步走这条）；
// 设了就必须成对且合规，宁可拒绝也不要上线一个说不清出处的部署。
export function deployProvenanceArgs(env) {
  const hash = env.SF_DEPLOY_COMMIT_HASH;
  const message = env.SF_DEPLOY_COMMIT_MESSAGE;
  if (!hash && !message) return ["--commit-dirty=true"];
  if (!/^[0-9a-f]{40}$/.test(hash || "") || !stampOfPage(message) || !stampOfTool(message)) {
    throw new Error("SF_DEPLOY_COMMIT_HASH and SF_DEPLOY_COMMIT_MESSAGE go together: a 40-character commit and a message carrying page:<fingerprint> and tool:<fingerprint>");
  }
  return ["--commit-hash", hash, "--commit-message", message, "--commit-dirty=false"];
}

// 上线那一刻的现场必须还是 ship 检查时的那一份：deploy-pages.mjs 读的是工作区，指纹却取自提交，
// 中间别的会话改了站点文件，新内容就会顶着旧戳上线。构建前和上传前各核对一次。
export function provenanceProblems(state, expected) {
  const problems = [];
  if (state.commit !== expected.commit) {
    problems.push(`HEAD moved from ${String(expected.commit).slice(0, 12)} to ${String(state.commit).slice(0, 12)}`);
  }
  const dirty = state.dirty || [];
  if (dirty.length) problems.push(`uncommitted site files appeared: ${dirty.slice(0, 5).join(", ")}`);
  if (state.pageFingerprint !== expected.pageFingerprint) problems.push(`page content changed (${expected.pageFingerprint} -> ${state.pageFingerprint})`);
  if (state.toolFingerprint !== expected.toolFingerprint) problems.push(`deploy tooling changed (${expected.toolFingerprint} -> ${state.toolFingerprint})`);
  return problems;
}

// 这几个环境变量会改变 build-pages 的产出（输出目录、版本号、安装包大小来源）；ship 与预览不许继承调用者手里的值。
const BUILD_ENV_OVERRIDES = ["SF_PAGES_OUT", "SF_APP_VERSION", "SF_DMG_PATH"];
export function cleanBuildEnv(env) {
  const copy = { ...env };
  for (const key of BUILD_ENV_OVERRIDES) delete copy[key];
  return copy;
}

// 线上首页引用的素材地址形如 /assets/名字?v=内容哈希前十位；与本地构建的哈希对账。
// 戳后面不许再跟十六进制字符：更长的串不是本构建写出的戳，不能截前十位冒充。
const ASSET_REFERENCE = /\/assets\/([A-Za-z0-9._-]+)\?v=([0-9a-f]{10})(?![0-9a-f])/g;

export function liveAssetStamps(html) {
  const stamps = new Map();
  for (const match of String(html).matchAll(ASSET_REFERENCE)) stamps.set(match[1], match[2]);
  return stamps;
}

export function assetDrift(localHashes, liveHtml) {
  const live = new Map();
  for (const match of String(liveHtml).matchAll(ASSET_REFERENCE)) {
    if (!live.has(match[1])) live.set(match[1], new Set());
    live.get(match[1]).add(match[2]);
  }
  const changed = [];
  const onlyLive = [];
  const notYetLive = [];
  for (const [name, stamps] of live) {
    if (!Object.hasOwn(localHashes, name)) onlyLive.push(name);
    // 同一素材在页面上带了两个不同的戳，本身就是不一致；只看最后一个会把漂移藏起来。
    else if (stamps.size !== 1 || !stamps.has(localHashes[name])) changed.push(name);
  }
  // 样式表已内联进页面，landing.css 只为旧页面缓存而发布，页面不再引用它。
  for (const name of Object.keys(localHashes)) if (!live.has(name) && name !== "landing.css") notYetLive.push(name);
  return { changed: changed.sort(), onlyLive: onlyLive.sort(), notYetLive: notYetLive.sort() };
}

export function syncVerdict({ drift, stamped, fingerprint, dirty = [], pageStamp, legacyStamp }) {
  const parts = [];
  if (drift.changed.length) parts.push(`changed: ${drift.changed.join(", ")}`);
  if (drift.notYetLive.length) parts.push(`not live yet: ${drift.notYetLive.join(", ")}`);
  if (drift.onlyLive.length) parts.push(`live only: ${drift.onlyLive.join(", ")}`);
  if (parts.length) return { state: "drift", text: `assets differ from the live page (${parts.join("; ")})` };
  // 新口径：page 戳只由页面内容算出，改工具不会让它失效。
  if (pageStamp !== undefined) {
    if (pageStamp && pageStamp !== fingerprint) {
      return { state: "drift", text: `the live deployment was shipped from different page content (${pageStamp}, this checkout is ${fingerprint})` };
    }
    if (dirty.length) {
      return { state: "dirty-checkout", text: `this checkout has uncommitted site files (${dirty.slice(0, 5).join(", ")}), so the local build is not the committed content and cannot confirm the live page` };
    }
    if (pageStamp) return { state: "in-sync", text: "in sync: shipped from exactly this page content, and every asset fingerprint matches" };
    const why = legacyStamp ? "the live deployment carries only the pre-split site stamp" : "the live deployment carries no content stamp";
    return { state: "assets-match-unstamped", text: `asset fingerprints match, but ${why}, so a text-only change cannot be ruled out until the next ship` };
  }
  if (stamped && stamped !== fingerprint) {
    return { state: "drift", text: `the live deployment was shipped from different site content (${stamped}, this checkout is ${fingerprint})` };
  }
  // 内容指纹取自提交，本地构建读的是工作区：有未提交的站点文件时，二者对得上也证明不了线上就是已提交的内容。
  if (dirty.length) {
    return { state: "dirty-checkout", text: `this checkout has uncommitted site files (${dirty.slice(0, 5).join(", ")}), so the local build is not the committed content and cannot confirm the live page` };
  }
  if (stamped) return { state: "in-sync", text: "in sync: shipped from exactly this site content, and every asset fingerprint matches" };
  return { state: "assets-match-unstamped", text: "asset fingerprints match, but the live deployment carries no content stamp, so a text-only change cannot be ruled out until the next ship" };
}

// ---- 2026-10-30 改进批：页面内容与工具分开指纹 ----
// 只有页面内容变了才必须重新预览；改工具脚本（本文件、pages-control、deploy-pages 等）不再作废预览，
// 但仍会被记录进部署说明的 tool 戳，并且仍然算"未提交站点文件"的一部分。
const TOOLING_PATHS = new Set([
  "services/download-analytics/pages-control.mjs",
  "services/download-analytics/deploy-pages.mjs",
  "services/download-analytics/site-inputs.mjs",
  "services/download-analytics/deploy-journal.mjs",
  "services/download-analytics/fetch-pool.mjs",
  "services/download-analytics/package.json",
  "services/download-analytics/wrangler.jsonc",
  "services/download-analytics/schema/download_events.sql",
]);

export function isToolInput(path) {
  return TOOLING_PATHS.has(path);
}

export function isPageInput(path) {
  return isSiteInput(path) && !isToolInput(path);
}

function fingerprintOf(lsTreeZ, predicate, label) {
  const entries = String(lsTreeZ).split("\0").filter(Boolean).map(record => {
    const tab = record.indexOf("\t");
    const [mode, type, sha] = record.slice(0, tab).split(" ");
    return { mode, type, sha, path: record.slice(tab + 1) };
  }).filter(entry => entry.type === "blob" && predicate(entry.path));
  if (entries.length === 0) throw new Error(`no ${label} input files found; refusing to fingerprint an empty set`);
  entries.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  const hash = createHash("sha256");
  for (const entry of entries) hash.update(`${entry.mode} ${entry.sha} ${entry.path}\0`);
  return hash.digest("hex").slice(0, 16);
}

export function pageFingerprint(lsTreeZ) {
  return fingerprintOf(lsTreeZ, isPageInput, "page");
}

export function toolFingerprint(lsTreeZ) {
  return fingerprintOf(lsTreeZ, isToolInput, "tool");
}

export function stampOfPage(message) {
  return /(?:^|\s)page:([0-9a-f]{16})(?:\s|$)/.exec(String(message || ""))?.[1] || "";
}

export function stampOfTool(message) {
  return /(?:^|\s)tool:([0-9a-f]{16})(?:\s|$)/.exec(String(message || ""))?.[1] || "";
}

// 只认"同页面内容指纹"的预览；pre-split 的旧 site: 戳不参与匹配。
export function findPreviewForPage(deployments, pageStamp) {
  if (!/^[0-9a-f]{16}$/.test(pageStamp || "")) throw new Error("a 16-character page fingerprint is required");
  return deployments.find(deployment => deployment.environment === "preview"
    && deployment.latest_stage?.name === "deploy" && deployment.latest_stage.status === "success"
    && stampOfPage(deployment.deployment_trigger?.metadata?.commit_message) === pageStamp) || null;
}

// ---- 部署日志判定（纯函数） ----
// 最后一条是 pending：说明上次部署没走到收尾。线上若仍是它的上一个部署，说明还没切换；否则可能已切换但没写完日志。
export function journalVerdict(events, liveDeploymentId) {
  const usable = (events || []).filter(event => event && event.phase !== "unreadable");
  const last = usable.at(-1);
  if (!last) return { state: "no-record", text: "no deploy has been recorded from this checkout yet", blocking: false };
  if (last.phase === "pending") {
    if (!liveDeploymentId) {
      // 读不到正式部署编号时无法判断有没有切换；这不是"未收尾"的证据，不能据此每天报警。
      return { state: "journal-unverified", text: `a deploy started at ${last.ts} never finished, and the live deployment could not be read; check the site before the next ship`, blocking: false };
    }
    if (liveDeploymentId === last.previous_deployment_id) {
      // 还没切换就没事可确认：线上原封不动，不该把下一次上线永久卡住。日志仍会显示这条记录。
      return { state: "interrupted-before-switch", text: `a deploy started at ${last.ts} never finished, but production is still ${liveDeploymentId}; nothing was switched`, blocking: false };
    }
    return { state: "interrupted-after-switch", text: `a deploy started at ${last.ts} never finished and production is ${liveDeploymentId || "unknown"}, not the recorded previous deployment ${last.previous_deployment_id || "unknown"}; confirm the page, then run a new ship`, blocking: true };
  }
  if (last.phase === "failed") {
    return { state: "last-attempt-failed", text: `the last deploy attempt at ${last.ts} failed: ${last.error || "no reason recorded"}`, blocking: false };
  }
  if (last.phase === "complete" && liveDeploymentId && last.deployment_id !== liveDeploymentId) {
    return { state: "changed-outside-ship", text: `production is ${liveDeploymentId}, but the last recorded ship ended at ${last.deployment_id} (a release-chain or manual deploy)`, blocking: false };
  }
  return { state: "ok", text: "the last recorded deploy finished", blocking: false };
}

// 读回的 ETag 缓存命中：URL 里已经带内容指纹，缓存里的哈希必须与它一致，且要有可用的 ETag，
// 才允许用条件请求（304）代替下载正文。
export function readbackCacheHit(entry, stamp) {
  return Boolean(entry && entry.etag && entry.hash && entry.hash.slice(0, 10) === stamp);
}

// 读回时只对"网络类"失败重试一次；404、类型不对这类立刻判红。
export function shouldRetryReadback(error) {
  const text = `${error?.name || ""} ${error?.message || ""}`;
  return /timeout|aborted|fetch failed|ECONN|UND_ERR|EAI_AGAIN|socket/i.test(text);
}

// 清理任务的源码摘要：源码没变就不重新上传（上传一次约 45 秒）。
export function sourceDigest(text) {
  return createHash("sha256").update(String(text)).digest("hex").slice(0, 16);
}
