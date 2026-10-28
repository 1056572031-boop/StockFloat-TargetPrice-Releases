import { execFileSync, spawnSync } from "node:child_process";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { readCloudflareToken } from "../feedback-worker/cloudflare-auth.mjs";

const root = fileURLToPath(new URL(".", import.meta.url));
const account = "45a8a4ed1f283f7449697c5958207c56";
const project = "skyrocket-downloads";
const productionBranch = "main";
const previewBranchPrefix = "review";
const previewDatabaseName = "skyrocket-downloads-preview";
const compatibilityDate = "2026-10-24";
const productionDatabaseId = "088fcda4-015d-4116-a7ba-bafe50d0346f";
const schemaPath = resolve(root, "schema/download_events.sql");
const wrangler = resolve(root, "../feedback-worker/node_modules/.bin/wrangler");

function usage() {
  throw new Error("usage: node pages-control.mjs preview | rollback <successful-production-deployment-id>");
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

function currentCommit() {
  const commit = exec("git", ["rev-parse", "HEAD"]);
  const branch = previewBranchForCommit(commit);
  const status = exec("git", ["status", "--porcelain"]);
  if (status) throw new Error("refusing to deploy a dirty tree; commit or stash the changes first");
  return { commit, branch };
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
    signal: AbortSignal.timeout(15_000), headers: { "cache-control": "no-cache" },
  });
  const page = await pageResponse.text();
  const healthResponse = await fetch(`${url}/health`, { signal: AbortSignal.timeout(15_000) });
  const health = await healthResponse.json().catch(() => ({}));
  const fingerprintedScript = /<script defer src="\/assets\/landing\.js\?v=[0-9a-f]{10}"><\/script>/.test(page);
  // 页面里每个视频都要支持分段请求（Safari 依赖 206）：取头两个字节，要求 206 且 Content-Range 对得上。
  const videos = [...new Set(page.match(/\/assets\/[A-Za-z0-9._-]+\.(?:webm|mov|mp4)\?v=[0-9a-f]{10}/g) || [])];
  const rangeResults = await Promise.all(videos.map(async path => {
    const response = await fetch(`${url}${path}`, { signal: AbortSignal.timeout(15_000), headers: { range: "bytes=0-1" } });
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
  const { commit, branch } = currentCommit();
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
    stdio: "inherit", env: { ...process.env, SF_APP_VERSION: version, SF_DMG_PATH: dmgPath },
  });
  const staging = await fs.mkdtemp(join(tmpdir(), "skyrocket-downloads-preview-"));
  try {
    await fs.writeFile(join(staging, "wrangler.jsonc"),
      JSON.stringify(previewWranglerConfig(resolve(root, "dist/pages"), databaseId), null, 2) + "\n", { mode: 0o600 });
    const deployed = spawnSync(wrangler, ["pages", "deploy", resolve(root, "dist/pages"), "--project-name", project,
      "--branch", branch, "--commit-hash", commit, "--commit-message", "Skyrocket download-page preview", "--commit-dirty=false"], {
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

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [command, value] = process.argv.slice(2);
  if (command === "preview" && value === undefined) await deployPreview();
  else if (command === "rollback" && value !== undefined) await rollback(value);
  else usage();
}
