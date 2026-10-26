import { execFileSync } from "node:child_process";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readCloudflareToken } from "../feedback-worker/cloudflare-auth.mjs";

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
    const probe = await fetch(
      `${base}/download?version=${encodeURIComponent(version)}&asset=${encodeURIComponent(`Skyrocket-V${version}.dmg`)}`,
      { method: "HEAD", redirect: "manual", signal: AbortSignal.timeout(10000) },
    );
    const location = probe.headers.get("location") || "";
    liveOk = pageResponse.ok && healthResponse.ok && healthBody.database === true
      && healthBody.build_version === version && pageBody.includes("下载 macOS 版")
      && pageBody.includes('class="logo"') && pageBody.includes("下载 DMG")
      && buttonCount >= 3 && probe.status === 302
      && location.startsWith("https://github.com/1056572031-boop/StockFloat-TargetPrice-Releases/releases/download/")
      && csp.includes("img-src data:");
    lastObservation = `page=${pageResponse.status} health=${healthResponse.status} database=${healthBody.database} build=${healthBody.build_version} buttons=${buttonCount} probe=${probe.status} location=${Boolean(location)} csp=${csp.includes("img-src data:")}`;
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
