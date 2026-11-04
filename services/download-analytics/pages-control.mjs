import { execFileSync, spawnSync } from "node:child_process";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { readCloudflareToken } from "../feedback-worker/cloudflare-auth.mjs";
import {
  SITE_PATHSPEC, assetDrift, cleanBuildEnv, dirtyPageInputs, dirtySiteInputs, findPreviewForPage, journalVerdict, pageFingerprint, shipProblems,
  shouldRetryReadback, stampOf, stampOfPage, stampOfTool, syncVerdict, toolFingerprint,
} from "./site-inputs.mjs";
import { acquireDeployLock, appendJournal, journalPath, readJournal, releaseDeployLock } from "./deploy-journal.mjs";

const root = fileURLToPath(new URL(".", import.meta.url));
const account = "45a8a4ed1f283f7449697c5958207c56";
const project = "skyrocket-downloads";
const productionBranch = "main";
const previewBranchPrefix = "review";
const previewDatabaseName = "skyrocket-downloads-preview";
const compatibilityDate = "2026-10-24";
// 本机到 pages.dev 的链路实测会出现十几秒的抖动：预览复核用 30 秒，并在 ship 里对网络类失败再试一次。
const PREVIEW_TIMEOUT_MS = 30_000;
const productionDatabaseId = "088fcda4-015d-4116-a7ba-bafe50d0346f";
const schemaPath = resolve(root, "schema/download_events.sql");
const wrangler = resolve(root, "../feedback-worker/node_modules/.bin/wrangler");

function usage() {
  throw new Error("usage: node pages-control.mjs preview | ship [--dry-run] [--preview <deployment-id>] | status | reconcile --confirm | rollback <successful-production-deployment-id>");
}

export function commandText(output) {
  return typeof output === "string" ? output.trim() : "";
}

function exec(command, args, options = {}) {
  return commandText(execFileSync(command, args, { cwd: root, encoding: "utf8", ...options }));
}

export function previewBranchForCommit(commit) {
  if (!/^[0-9a-f]{40}$/.test(commit)) throw new Error("current git commit is unavailable");
  return `${previewBranchPrefix}-${commit.slice(0, 12)}`;
}

// git 一律在仓库根目录执行：status 的路径永远相对仓库根，在子目录里跑会让路径对不上。
function git(args) {
  const top = exec("git", ["rev-parse", "--show-toplevel"]);
  return execFileSync("git", args, { cwd: top, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
}

// 只看站点构建输入（见 site-inputs.mjs）：主树里别的未提交文件（报告、台账、别的会话的改动）不该挡住预览和上线。
function gitTop() {
  return exec("git", ["rev-parse", "--show-toplevel"]);
}

export function readSiteGitState() {
  const commit = git(["rev-parse", "HEAD"]).trim();
  const branchName = git(["rev-parse", "--abbrev-ref", "HEAD"]).trim();
  // --ignored：build-pages 把 public/ 整个复制进部署包，被 git 忽略的文件（例如误落进去的 .DS_Store）不会进指纹却会上线。
  const dirty = dirtySiteInputs(git(["status", "--porcelain", "-z", "--untracked-files=all", "--ignored", "--", ...SITE_PATHSPEC]));
  const tree = git(["ls-tree", "-r", "-z", "HEAD", "--", ...SITE_PATHSPEC]);
  const page = pageFingerprint(tree);
  const tool = toolFingerprint(tree);
  // fingerprint 保留为页面指纹的别名：只有页面内容变了才需要重新预览。
  return { commit, branch: branchName === "HEAD" ? "" : branchName, dirty, pageFingerprint: page, toolFingerprint: tool, fingerprint: page };
}

function currentCommit() {
  const state = readSiteGitState();
  const branch = previewBranchForCommit(state.commit);
  const dirty = dirtyPageInputs(git(["status", "--porcelain", "-z", "--untracked-files=all", "--ignored", "--", ...SITE_PATHSPEC]));
  // 预览只要求页面内容已提交；改部署工具不该逼人重新预览（上线仍然要求全部站点输入干净）。
  if (dirty.length) {
    throw new Error(`refusing to preview uncommitted page files: ${dirty.join(", ")}; commit or stash them first`);
  }
  return { commit: state.commit, branch, pageFingerprint: state.pageFingerprint, toolFingerprint: state.toolFingerprint };
}

let token;
let checkToken;

function apiToken() {
  token ||= readCloudflareToken(wrangler);
  return token;
}

function previewCheckToken() {
  checkToken ??= execFileSync("/usr/bin/security",
    ["find-generic-password", "-s", "SkyrocketDownloadCheck", "-w"], { encoding: "utf8" }).trim();
  if (!/^[a-f0-9]{64}$/.test(checkToken)) throw new Error("Download check token is unavailable or malformed");
  return checkToken;
}

async function cloudflare(path, method = "GET", data) {
  // 只对只读 GET 做一次网络重试：写操作重发可能重复执行，宁可失败让人重跑。
  for (let attempt = 0; ; attempt += 1) {
    try {
      const response = await fetch(`https://api.cloudflare.com/client/v4/accounts/${account}${path}`, {
        method,
        redirect: "error",
        signal: AbortSignal.timeout(30_000),
        headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
        body: data === undefined ? undefined : JSON.stringify(data),
      });
      const body = await response.json().catch(() => null);
      if (!response.ok || !body?.success) throw new Error(`Cloudflare API ${method} ${path} failed (HTTP ${response.status})`);
      return body.result;
    } catch (error) {
      if (method !== "GET" || attempt >= 1 || !shouldRetryReadback(error)) throw error;
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
  }
}

function deploymentBranch(deployment) {
  return deployment?.deployment_trigger?.metadata?.branch || deployment?.branch || "";
}

function deploymentSucceeded(deployment) {
  return deployment?.latest_stage?.name === "deploy" && deployment.latest_stage.status === "success";
}

async function projectConfig() {
  const config = await cloudflare(`/pages/projects/${project}`);
  if (!config || config.production_branch !== productionBranch) {
    throw new Error("Pages project or production branch is not the expected one");
  }
  return config;
}

// 上传预览用的临时 Wrangler 配置必须自己声明 D1：Pages 以上传的配置为准，漏写就会把预览环境的绑定清空
// （2026-10-27 实测：预览页能打开、/health 却报 database=false；生产环境的绑定不受影响）。
export function previewWranglerConfig(outputDir, databaseId) {
  if (typeof databaseId !== "string" || !databaseId) throw new Error("preview D1 id is required");
  if (databaseId === productionDatabaseId) throw new Error("preview must never bind the production database");
  return {
    name: project,
    pages_build_output_dir: outputDir,
    compatibility_date: compatibilityDate,
    d1_databases: [{ binding: "DB", database_name: previewDatabaseName, database_id: databaseId }],
  };
}

async function findDatabaseByName(name) {
  const result = await cloudflare("/d1/database?per_page=100");
  const list = Array.isArray(result) ? result : result?.result || [];
  return list.find(database => database.name === name)?.uuid || "";
}

async function ensurePreviewDatabase(config) {
  const bound = config.deployment_configs?.preview?.d1_databases?.DB?.id;
  // 预览配置里的绑定可能被清空过，所以先按名字找已有的库，找不到才新建，避免重复创建同名数据库。
  let databaseId = (typeof bound === "string" && bound) || await findDatabaseByName(previewDatabaseName);
  if (!databaseId) {
    // The preview database is intentionally separate: preview downloads must not affect production counts.
    const created = await cloudflare("/d1/database", "POST", { name: previewDatabaseName });
    databaseId = created?.uuid;
  }
  if (typeof databaseId !== "string" || !databaseId) throw new Error("Cloudflare did not return a preview D1 ID");
  if (databaseId === productionDatabaseId) throw new Error("preview must never bind the production database");
  if (bound !== databaseId) {
    await cloudflare(`/pages/projects/${project}`, "PATCH", {
      deployment_configs: {
        preview: {
          compatibility_date: compatibilityDate,
          d1_databases: { DB: { id: databaseId } },
          env_vars: { CHECK_TOKEN: { type: "secret_text", value: previewCheckToken() } },
        },
      },
    });
  }
  return databaseId;
}

async function waitForPreview(branch, beforeIds) {
  for (let attempt = 0; attempt < 12; attempt += 1) {
    const deployments = await cloudflare(`/pages/projects/${project}/deployments?per_page=25`);
    const list = Array.isArray(deployments) ? deployments : deployments?.result || [];
    const found = list.find(deployment => deployment.environment === "preview"
      && deploymentBranch(deployment) === branch && !beforeIds.has(deployment.id));
    if (found?.url && deploymentSucceeded(found)) return found;
    await new Promise(resolve => setTimeout(resolve, 1_500));
  }
  throw new Error("preview deployment did not reach a successful, addressable state");
}

async function verifyPreview(url, expectedVersion) {
  const pageResponse = await fetch(`${url}/`, {
    signal: AbortSignal.timeout(PREVIEW_TIMEOUT_MS), headers: { "cache-control": "no-cache" },
  });
  const page = await pageResponse.text();
  const healthResponse = await fetch(`${url}/health`, { signal: AbortSignal.timeout(PREVIEW_TIMEOUT_MS) });
  const health = await healthResponse.json().catch(() => ({}));
  const fingerprintedScript = /<script defer src="\/assets\/landing\.js\?v=[0-9a-f]{10}"><\/script>/.test(page);
  // 页面里每个视频都要支持分段请求（Safari 依赖 206）：取头两个字节，要求 206 且 Content-Range 对得上。
  const videos = [...new Set(page.match(/\/assets\/[A-Za-z0-9._-]+\.(?:webm|mov|mp4)\?v=[0-9a-f]{10}/g) || [])];
  const rangeResults = await Promise.all(videos.map(async path => {
    const response = await fetch(`${url}${path}`, { signal: AbortSignal.timeout(PREVIEW_TIMEOUT_MS), headers: { range: "bytes=0-1" } });
    await response.arrayBuffer();
    return response.status === 206 && /^bytes 0-1\/\d+$/.test(response.headers.get("content-range") || "")
      && response.headers.get("accept-ranges") === "bytes";
  }));
  const rangeOk = rangeResults.length === 0 || rangeResults.every(Boolean);
  const d1Bound = health.database === true;
  const versionMatches = health.build_version === expectedVersion;
  if (!pageResponse.ok || !healthResponse.ok || !fingerprintedScript || !d1Bound || !versionMatches || !rangeOk) {
    throw new Error(`preview readback failed: page=${pageResponse.status} health=${healthResponse.status} database=${health.database} version=${health.build_version} ranges=${rangeResults.join("/")}`);
  }
}

async function productionVersion(subdomain) {
  const response = await fetch(`https://${subdomain}/health`, {
    signal: AbortSignal.timeout(15_000), headers: { "cache-control": "no-cache" },
  });
  const health = await response.json().catch(() => ({}));
  if (!response.ok || health.database !== true || !/^\d+\.\d+\.\d+$/.test(health.build_version || "")) {
    throw new Error("production health does not provide a usable published version");
  }
  return health.build_version;
}

async function deployPreview() {
  const { commit, branch, pageFingerprint: page, toolFingerprint: tool } = currentCommit();
  const config = await projectConfig();
  const version = await productionVersion(config.subdomain);
  const dmgPath = resolve(process.env.HOME || "", "Desktop", "Skyrocket", `Skyrocket-V${version}.dmg`);
  const productionId = config.canonical_deployment?.id || "";
  const productionConfigHash = config.deployment_configs?.production?.wrangler_config_hash;
  const databaseId = await ensurePreviewDatabase(config);
  const schema = await fs.readFile(schemaPath, "utf8");
  await cloudflare(`/d1/database/${databaseId}/query`, "POST", { sql: schema });
  const before = await cloudflare(`/pages/projects/${project}/deployments?per_page=25`);
  const beforeIds = new Set((Array.isArray(before) ? before : before?.result || []).map(deployment => deployment.id));

  exec(process.execPath, [resolve(root, "build-pages.mjs")], {
    stdio: "inherit", env: { ...cleanBuildEnv(process.env), SF_APP_VERSION: version, SF_DMG_PATH: dmgPath },
  });
  const staging = await fs.mkdtemp(join(tmpdir(), "skyrocket-downloads-preview-"));
  try {
    await fs.writeFile(join(staging, "wrangler.jsonc"),
      JSON.stringify(previewWranglerConfig(resolve(root, "dist/pages"), databaseId), null, 2) + "\n", { mode: 0o600 });
    const deployed = spawnSync(wrangler, ["pages", "deploy", resolve(root, "dist/pages"), "--project-name", project,
      "--branch", branch, "--commit-hash", commit, "--commit-message", `Skyrocket download-page preview ${commit.slice(0, 12)} page:${page} tool:${tool}`, "--commit-dirty=false"], {
      cwd: staging,
      encoding: "utf8",
      env: { ...process.env, CLOUDFLARE_ACCOUNT_ID: account, WRANGLER_SEND_METRICS: "false" },
    });
    if (deployed.status !== 0) throw new Error(`preview deploy failed: ${(deployed.stderr || deployed.stdout || "").trim().slice(-1000)}`);
  } finally {
    await fs.rm(staging, { recursive: true, force: true });
  }

  const deployment = await waitForPreview(branch, beforeIds);
  await verifyPreview(deployment.url, version);
  const after = await projectConfig();
  if (after.canonical_deployment?.id !== productionId) throw new Error("preview deployment changed production canonical deployment");
  const afterProduction = after.deployment_configs?.production;
  if (afterProduction?.d1_databases?.DB?.id !== productionDatabaseId || afterProduction?.wrangler_config_hash !== productionConfigHash) {
    throw new Error("preview deployment changed the production configuration");
  }
  console.log(JSON.stringify({ branch, deployment_id: deployment.id, preview_url: deployment.url, preview_database_id: databaseId, verified: true }));
}

export function isRollbackTarget(target, currentId) {
  return Boolean(target && target.id !== currentId && target.environment === "production" && deploymentSucceeded(target));
}

async function rollback(deploymentId) {
  if (!/^[0-9a-f-]{36}$/i.test(deploymentId)) usage();
  const repoRoot = gitTop();
  await acquireDeployLock(repoRoot, { command: "rollback" });
  try {
    return await rollbackLocked(deploymentId);
  } finally {
    await releaseDeployLock(repoRoot);
  }
}

async function rollbackLocked(deploymentId) {
  const config = await projectConfig();
  const currentId = config.canonical_deployment?.id || "";
  const deployments = await cloudflare(`/pages/projects/${project}/deployments?per_page=25`);
  const list = Array.isArray(deployments) ? deployments : deployments?.result || [];
  const target = list.find(deployment => deployment.id === deploymentId);
  if (!isRollbackTarget(target, currentId)) {
    throw new Error("rollback target must be an earlier successful production deployment listed by Pages");
  }
  // This is deliberately never automatic: the caller must name the exact, already reviewed deployment ID.
  const result = await cloudflare(`/pages/projects/${project}/deployments/${deploymentId}/rollback`, "POST");
  const after = await projectConfig();
  const canonical = after.canonical_deployment?.id || "";
  if (canonical !== deploymentId && canonical !== result?.id) throw new Error("rollback returned success but production canonical deployment did not change to the target");
  const page = await fetch(`https://${after.subdomain}/health`, { signal: AbortSignal.timeout(15_000), headers: { "cache-control": "no-cache" } });
  const health = await page.json().catch(() => ({}));
  if (!page.ok || health.database !== true) throw new Error("rollback completed but production health readback failed");
  console.log(JSON.stringify({ rolled_back_to: canonical, requested_target: deploymentId, health: "ok" }));
}

async function previewDeployments() {
  const result = await cloudflare(`/pages/projects/${project}/deployments?env=preview&per_page=25`);
  return Array.isArray(result) ? result : result?.result || [];
}

async function localSiteVersion() {
  const constants = await fs.readFile(resolve(root, "../../sf_constants.py"), "utf8");
  const version = constants.match(/^APP_VERSION\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'/m)?.[1];
  if (!version) throw new Error("APP_VERSION is unavailable");
  return version;
}

// ship 自己再跑一遍站点测试：不信任"刚才跑过"，上线的就是此刻这份代码。
async function siteTestsPass() {
  const files = (await fs.readdir(resolve(root, "tests"))).filter(name => name.endsWith(".test.mjs")).sort();
  if (files.length < 2) return false;
  const run = spawnSync(process.execPath, ["--test", ...files.map(name => `tests/${name}`)], {
    cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024,
  });
  const count = label => Number(new RegExp(`^ℹ ${label} (\\d+)$`, "m").exec(run.stdout || "")?.[1] ?? NaN);
  // 全部跳过不算绿：通过数必须大于 0 并等于总数。
  return run.status === 0 && count("fail") === 0 && count("pass") > 0 && count("pass") === count("tests");
}

function parseShipFlags(flags) {
  const options = { dryRun: false, preview: "" };
  for (let index = 0; index < flags.length; index += 1) {
    const flag = flags[index];
    if (flag === "--dry-run") options.dryRun = true;
    else if (flag === "--preview") { options.preview = flags[index + 1] || ""; index += 1; }
    else if (flag.startsWith("--preview=")) options.preview = flag.slice("--preview=".length);
    else usage();
  }
  if (options.preview && !/^[0-9a-f-]{36}$/i.test(options.preview)) usage();
  return options;
}

// 正式上线的唯一入口。硬条件见 site-inputs.mjs 的 shipProblems；--dry-run 只检查、不部署。
// --preview 必须点名一份预览：工具只认"同页面内容"，但用户有没有真的看过，只能靠这个编号来证明。
async function ship(flags) {
  const { dryRun, preview: requestedPreview } = parseShipFlags(flags);
  const repoRoot = gitTop();
  const state = readSiteGitState();
  const config = await projectConfig();
  const liveVersion = await productionVersion(config.subdomain);
  const lastDeploy = journalVerdict(await readJournal(repoRoot), config.canonical_deployment?.id || "");
  let preview = findPreviewForPage(await previewDeployments(), state.pageFingerprint);
  let previewCheckError = null;
  if (preview) {
    // 带戳的"成功"预览不一定还能用：它可能是按旧的线上版本号构建的，或者上传后的核对没过。重新读回，对不上就不算数。
    // 网络类失败先重试一次；真正的版本/内容不符不重试。
    for (let attempt = 0; preview; attempt += 1) {
      try { await verifyPreview(preview.url, liveVersion); break; } catch (error) {
        if (attempt === 0 && shouldRetryReadback(error)) { await new Promise(resolve => setTimeout(resolve, 2000)); continue; }
        previewCheckError = error.message; preview = null;
      }
    }
  }
  const summary = { commit: state.commit, branch: state.branch, page_fingerprint: state.pageFingerprint,
    tool_fingerprint: state.toolFingerprint, live_version: liveVersion, preview_deployment_id: preview?.id || null,
    preview_url: preview?.url || null, preview_check_error: previewCheckError,
    last_deploy: { state: lastDeploy.state, detail: lastDeploy.text, journal: journalPath(repoRoot) } };
  const problems = shipProblems({ ...state, liveVersion, localVersion: await localSiteVersion(),
    previewDeploymentId: preview?.id || "", testsPassed: await siteTestsPass() });
  if (lastDeploy.blocking) problems.unshift(`the previous deploy did not finish: ${lastDeploy.text}`);
  if (!requestedPreview) problems.push(`no preview was named; after the owner has looked at it, pass --preview ${preview?.id || "<deployment-id>"}`);
  else if (preview?.id !== requestedPreview) {
    problems.push(`--preview ${requestedPreview} is not a successful preview of exactly this page content${previewCheckError ? ` (${previewCheckError})` : ""}`);
  }
  if (problems.length || dryRun) {
    console.log(JSON.stringify({ ...summary, ready: problems.length === 0, dry_run: dryRun, problems }, null, 2));
    if (problems.length) process.exitCode = 1;
    return;
  }
  await acquireDeployLock(repoRoot, { command: "ship" });
  let finished = false;
  let recoverIfSwitched = async () => "";
  try {
    // 回滚点与线上版本都在锁内重读：排队等锁期间别的部署或发版可能已经落地，用旧的版本号上线会公布错版本。
    const lockedConfig = await projectConfig();
    const previousId = lockedConfig.canonical_deployment?.id || "";
    const liveNow = await productionVersion(lockedConfig.subdomain);
    if (liveNow !== liveVersion) {
      throw new Error(`the live version changed from ${liveVersion} to ${liveNow} while waiting for the deploy lock; rerun ship`);
    }
    const message = `Skyrocket download page ${state.commit.slice(0, 12)} page:${state.pageFingerprint} tool:${state.toolFingerprint}`;
    // 部署脚本可能在“上传已经成功、最后读回失败”时才报错。先看正式站是不是已经换成了我们这次的部署：
    // 是的话把日志记成完成（附上读回失败的原因），退出码仍然非零，因为读回确实没通过。
    recoverIfSwitched = async reason => {
      try {
        const afterRecover = await projectConfig();
        const liveId = afterRecover.canonical_deployment?.id || "";
        if (!liveId || liveId === previousId) return "";
        const recordNow = await cloudflare(`/pages/projects/${project}/deployments/${liveId}`);
        const liveMessage = String(recordNow?.deployment_trigger?.metadata?.commit_message || "");
        if (stampOfPage(liveMessage) !== state.pageFingerprint || !liveMessage.includes(state.commit.slice(0, 12))) return "";
        await appendJournal(repoRoot, { phase: "complete", commit: state.commit, page_fingerprint: state.pageFingerprint,
          tool_fingerprint: state.toolFingerprint, preview_deployment_id: requestedPreview, previous_deployment_id: previousId,
          deployment_id: liveId, note: `deploy script reported a failure after the switch: ${String(reason).slice(0, 300)}` });
        finished = true;
        console.log(JSON.stringify({ ...summary, shipped: true, recovered_after_error: String(reason).slice(0, 500),
          previous_deployment_id: previousId || null, deployment_id: liveId,
          rollback: previousId ? `npm run rollback -- ${previousId}` : null }, null, 2));
        process.exitCode = 1;
        return liveId;
      } catch { return ""; }
    };
    await appendJournal(repoRoot, { phase: "pending", commit: state.commit, page_fingerprint: state.pageFingerprint,
      tool_fingerprint: state.toolFingerprint, preview_deployment_id: requestedPreview, previous_deployment_id: previousId, message });
    console.log(JSON.stringify({ ...summary, ready: true, dry_run: false, problems: [], deploying: true }));
    try {
      execFileSync(process.execPath, [resolve(root, "deploy-pages.mjs")], { cwd: root, stdio: "inherit",
        env: { ...cleanBuildEnv(process.env), SF_DEPLOY_COMMIT_HASH: state.commit, SF_DEPLOY_COMMIT_MESSAGE: message } });
    } catch (error) {
      const failure = `deploy-pages.mjs failed (${error.status ?? error.message}); production before this attempt was ${previousId || "unknown"}`;
      if (await recoverIfSwitched(failure)) return;
      throw new Error(`${failure}. Run npm run status; if the live page is wrong, npm run rollback -- ${previousId}`);
    }
    const after = await projectConfig();
    const deploymentId = after.canonical_deployment?.id || "";
    if (!deploymentId || deploymentId === previousId) {
      throw new Error(`deploy reported success but the production deployment did not change (still ${previousId || "unknown"})`);
    }
    const record = await cloudflare(`/pages/projects/${project}/deployments/${deploymentId}`);
    const deployedMessage = record?.deployment_trigger?.metadata?.commit_message;
    if (stampOfPage(deployedMessage) !== state.pageFingerprint) {
      throw new Error(`the new production deployment ${deploymentId} does not carry page:${state.pageFingerprint}`);
    }
    await appendJournal(repoRoot, { phase: "complete", commit: state.commit, page_fingerprint: state.pageFingerprint,
      tool_fingerprint: state.toolFingerprint, preview_deployment_id: requestedPreview, previous_deployment_id: previousId,
      deployment_id: deploymentId });
    finished = true;
    console.log(JSON.stringify({ ...summary, shipped: true, previous_deployment_id: previousId || null, deployment_id: deploymentId,
      deployed_tool_fingerprint: stampOfTool(deployedMessage) || null,
      rollback: previousId ? `npm run rollback -- ${previousId}` : null }, null, 2));
  } catch (error) {
    if (await recoverIfSwitched(error.message)) return;
    if (!finished) {
      await appendJournal(repoRoot, { phase: "failed", commit: state.commit, page_fingerprint: state.pageFingerprint,
        tool_fingerprint: state.toolFingerprint, error: String(error.message).slice(0, 500) });
    }
    throw error;
  } finally {
    await releaseDeployLock(repoRoot);
  }
}

// 上次部署被打断后，日志里会一直留着"进行中"。这个入口在人工看过线上之后把账收掉：
// 线上已是新部署就补一条 complete；还停在旧部署就补一条 failed。必须显式 --confirm，绝不自动跑。
// 对账也要拿部署锁：它写日志，和 ship 的 pending/complete 不能交错。
async function reconcile(flags) {
  const repoRoot = gitTop();
  await acquireDeployLock(repoRoot, { command: "reconcile" });
  try {
    return await reconcileLocked(flags, repoRoot);
  } finally {
    await releaseDeployLock(repoRoot);
  }
}

async function reconcileLocked(flags, repoRoot) {
  const allowed = flags.filter(flag => flag === "--confirm" || flag === "--abandon");
  if (!allowed.includes("--confirm") || allowed.length !== flags.length) usage();
  const abandon = flags.includes("--abandon");
  const events = await readJournal(repoRoot);
  const last = journalVerdict(events, null);
  const pending = [...events].reverse().find(event => event.phase === "pending");
  if (!pending || !["interrupted-before-switch", "interrupted-after-switch"].includes(last.state)) {
    throw new Error("there is no unfinished deploy in the journal; nothing to reconcile");
  }
  const config = await projectConfig();
  const deploymentId = config.canonical_deployment?.id || "";
  if (!deploymentId) throw new Error("the live deployment id is unavailable; check the site first");
  if (deploymentId === pending.previous_deployment_id) {
    await appendJournal(repoRoot, { phase: "failed", commit: pending.commit, page_fingerprint: pending.page_fingerprint,
      tool_fingerprint: pending.tool_fingerprint, error: "reconciled: production never switched", reconciled: true });
    console.log(JSON.stringify({ reconciled: "not-switched", live_deployment_id: deploymentId }, null, 2));
    return;
  }
  const record = await cloudflare(`/pages/projects/${project}/deployments/${deploymentId}`);
  if (stampOfPage(record?.deployment_trigger?.metadata?.commit_message) !== pending.page_fingerprint) {
    if (!abandon) {
      throw new Error(`production is ${deploymentId}, but it does not carry the interrupted deploy's page fingerprint ${pending.page_fingerprint}; inspect the site, then rerun with --abandon to record this attempt as failed`);
    }
    await appendJournal(repoRoot, { phase: "failed", commit: pending.commit, page_fingerprint: pending.page_fingerprint,
      tool_fingerprint: pending.tool_fingerprint, error: "reconciled with --abandon: production holds different content",
      reconciled: true });
    console.log(JSON.stringify({ reconciled: "abandoned", live_deployment_id: deploymentId }, null, 2));
    return;
  }
  await appendJournal(repoRoot, { phase: "complete", commit: pending.commit, page_fingerprint: pending.page_fingerprint,
    tool_fingerprint: pending.tool_fingerprint, deployment_id: deploymentId, previous_deployment_id: pending.previous_deployment_id,
    reconciled: true });
  console.log(JSON.stringify({ reconciled: "switched", live_deployment_id: deploymentId }, null, 2));
}

// 只读对账：线上首页引用的素材指纹与本地构建逐个比，再读正式部署记录里的内容指纹戳。不部署，不写线上任何东西。
async function status() {
  const repoRoot = gitTop();
  const state = readSiteGitState();
  let pageStamp = "";
  let toolStamp = "";
  let legacyStamp = "";
  let deploymentNote = "";
  let deploymentId = null;
  try {
    const config = await projectConfig();
    deploymentId = config.canonical_deployment?.id || null;
    const record = deploymentId ? await cloudflare(`/pages/projects/${project}/deployments/${deploymentId}`) : null;
    const message = record?.deployment_trigger?.metadata?.commit_message;
    pageStamp = stampOfPage(message);
    toolStamp = stampOfTool(message);
    legacyStamp = stampOf(message);
  } catch (error) {
    deploymentNote = `the production deployment record is unreadable (${error.message}), so its content stamp is unknown`;
  }
  const lastDeploy = journalVerdict(await readJournal(repoRoot), deploymentId);
  const base = "https://skyrocket-downloads.pages.dev";
  const read = async path => {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await fetch(`${base}${path}`, { signal: AbortSignal.timeout(45_000), headers: { "cache-control": "no-cache" } });
      } catch (error) {
        if (attempt >= 1 || !shouldRetryReadback(error)) throw error;
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }
  };
  const pageResponse = await read("/");
  const html = await pageResponse.text();
  const healthResponse = await read("/health");
  const health = await healthResponse.json().catch(() => ({}));
  if (!pageResponse.ok || !healthResponse.ok || !/^\d+\.\d+\.\d+$/.test(health.build_version || "")) {
    throw new Error(`the live site is not readable: page=${pageResponse.status} health=${healthResponse.status}`);
  }
  // 按线上同一个版本号在临时目录构建，只为拿到每个素材的内容哈希；真实的 dist/pages 不碰。
  const scratch = await fs.mkdtemp(join(tmpdir(), "skyrocket-downloads-status-"));
  let built;
  try {
    const output = exec(process.execPath, [resolve(root, "build-pages.mjs")], {
      env: { ...cleanBuildEnv(process.env), SF_PAGES_OUT: scratch, SF_APP_VERSION: health.build_version }, maxBuffer: 16 * 1024 * 1024,
    });
    built = JSON.parse(output.split("\n").at(-1));
  } finally {
    await fs.rm(scratch, { recursive: true, force: true });
  }
  const verdict = syncVerdict({ drift: assetDrift(built.assetHashes, html), pageStamp, legacyStamp,
    fingerprint: state.pageFingerprint, dirty: state.dirty });
  if (deploymentNote && verdict.state === "assets-match-unstamped") {
    verdict.text = `asset fingerprints match; ${deploymentNote}, so a text-only change cannot be ruled out`;
  }
  console.log(JSON.stringify({
    live_version: health.build_version, production_deployment_id: deploymentId,
    production_page_stamp: pageStamp || null, production_tool_stamp: toolStamp || null,
    production_legacy_stamp: pageStamp ? null : (legacyStamp || null),
    checkout: { commit: state.commit, branch: state.branch, page_fingerprint: state.pageFingerprint,
      tool_fingerprint: state.toolFingerprint, uncommitted_site_files: state.dirty },
    last_deploy: { state: lastDeploy.state, detail: lastDeploy.text, journal: journalPath(repoRoot) },
    verdict: verdict.state, detail: verdict.text,
  }, null, 2));
  if (verdict.state === "drift" || lastDeploy.blocking) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [command, ...rest] = process.argv.slice(2);
  try {
    if (command === "preview" && rest.length === 0) await deployPreview();
    else if (command === "ship") await ship(rest);
    else if (command === "status" && rest.length === 0) await status();
    else if (command === "reconcile") await reconcile(rest);
    else if (command === "rollback" && rest.length === 1) await rollback(rest[0]);
    else usage();
  } catch (error) {
    // 一行说清哪里失败；网络类错误带上底层原因（例如连接 Cloudflare 超时），不把堆栈丢给人看。
    const cause = error?.cause?.code ? ` (${error.cause.code})` : "";
    console.error(`pages-control ${command || ""}: ${error?.message || error}${cause}`);
    process.exitCode = 1;
  }
}
