// 部署日志与部署锁：只做文件读写，判定放在 site-inputs.mjs（可单测）。
// 日志仍按检出目录放在 outputs/site-deploys/journal.jsonl（它记录的是这棵树发起的部署）。
// 部署锁与清理缓存放在本机共享目录：同一个仓库的多个克隆也必须互相看得见，否则两个克隆会同时上线。
import { execFileSync } from "node:child_process";
import { promises as fs, readFileSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const DIR = join("outputs", "site-deploys");
export const JOURNAL_MAX_LINES = 500;

export function journalPath(repoRoot) {
  return join(repoRoot, DIR, "journal.jsonl");
}

// 本机共享状态目录；测试用 SF_SITE_STATE_DIR 指到临时目录，避免碰用户缓存。
export function stateDir() {
  return process.env.SF_SITE_STATE_DIR || join(homedir(), "Library", "Caches", "SkyrocketDeploy");
}

export function lockPath(override) {
  return override || join(stateDir(), "deploy.lock");
}

export function cleanupCachePath() {
  return join(stateDir(), "cleanup-cache.json");
}

export function readbackCachePath() {
  return join(stateDir(), "readback-cache.json");
}

export async function readJournal(repoRoot) {
  let text;
  try {
    text = await fs.readFile(journalPath(repoRoot), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
  return text.split("\n").filter(Boolean).map(line => {
    try { return JSON.parse(line); } catch { return { phase: "unreadable", raw: line.slice(0, 200) }; }
  });
}

// 只保留最近 JOURNAL_MAX_LINES 条：轮转在同一进程里做，ship 持有部署锁时不会与别的写者相撞。
async function trimJournal(path) {
  const text = await fs.readFile(path, "utf8").catch(() => "");
  const lines = text.split("\n").filter(Boolean);
  if (lines.length <= JOURNAL_MAX_LINES) return;
  const kept = lines.slice(-JOURNAL_MAX_LINES).join("\n") + "\n";
  const tmp = `${path}.tmp`;
  await fs.writeFile(tmp, kept);
  await fs.rename(tmp, path);
}

export async function appendJournal(repoRoot, entry) {
  await fs.mkdir(join(repoRoot, DIR), { recursive: true });
  await fs.appendFile(journalPath(repoRoot), JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n");
  await trimJournal(journalPath(repoRoot));
}

export function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === "EPERM"; }
}

// 只看 pid 会被"重启后进程号复用"骗到：把进程的启动时刻一起记下来，两端都对上才算同一个持有者。
export function processStartedAt(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return "";
  try { return execFileSync("ps", ["-o", "lstart=", "-p", String(pid)], { encoding: "utf8" }).trim(); }
  catch { return ""; }
}

function sameHolder(lock, pid) {
  if (!lock || lock.pid !== pid) return false;
  if (!lock.process_started_at) return true;
  const started = processStartedAt(pid);
  return !started || started === lock.process_started_at;
}

// 返回 { acquired } 或抛出带持有者信息的错误。陈旧锁（进程已不在）回收一次后重试。
// options.path 只给测试注入；生产一律用本机共享的 lockPath()。
export async function acquireDeployLock(repoRoot, { command, now = () => Date.now(), alive = processAlive, path = lockPath() } = {}) {
  await fs.mkdir(dirname(path), { recursive: true });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await fs.writeFile(path, JSON.stringify({ pid: process.pid, command, started_at: new Date(now()).toISOString(),
        process_started_at: processStartedAt(process.pid) }) + "\n", { flag: "wx" });
      return { acquired: true, path };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      const holder = await readLock(path);
      const holderAlive = Boolean(holder && alive(holder.pid) && (holder.process_started_at
        ? !processStartedAt(holder.pid) || processStartedAt(holder.pid) === holder.process_started_at : true));
      if (!holder || !holderAlive) {
        await fs.rm(path, { force: true });
        continue;
      }
      throw new Error(`another deploy is running: pid ${holder.pid} (${holder.command || "unknown"}) since ${holder.started_at}`);
    }
  }
  throw new Error("could not acquire the deploy lock");
}

async function readLock(path) {
  try { return JSON.parse(await fs.readFile(path, "utf8")); } catch { return null; }
}

// 只删自己持有的锁：锁被判定陈旧、被别人回收重建之后，原来的进程不许把新持有者的锁删掉。
export async function releaseDeployLock(repoRoot, { path = lockPath() } = {}) {
  try {
    const holder = JSON.parse(await fs.readFile(path, "utf8"));
    if (!sameHolder(holder, process.pid)) return;
  } catch { return; }
  await fs.rm(path, { force: true });
}

// 进程即将退出时同步释放（异步清理在 exit 里跑不完）。此刻只可能是自己持有的那把。
export function releaseDeployLockSync(repoRoot, { path = lockPath() } = {}) {
  try {
    const holder = JSON.parse(readFileSync(path, "utf8"));
    if (!sameHolder(holder, process.pid)) return;
  } catch { return; }
  try { rmSync(path, { force: true }); } catch { /* 退出路径不再抛 */ }
}
