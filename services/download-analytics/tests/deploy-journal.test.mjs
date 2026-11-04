import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import {
  JOURNAL_MAX_LINES, acquireDeployLock, appendJournal, journalPath, lockPath, processAlive, readJournal, releaseDeployLock,
} from "../deploy-journal.mjs";

async function tempRepo() {
  const root = await fs.mkdtemp(join(tmpdir(), "sf-journal-test-"));
  // 锁与清理缓存现在放在本机共享目录；每个用例指到自己的临时目录，避免碰用户缓存。
  process.env.SF_SITE_STATE_DIR = join(root, "state");
  return root;
}

test("the journal lives outside the site inputs so writing it never changes a fingerprint", async () => {
  const repo = await tempRepo();
  try {
    assert.equal(journalPath(repo), join(repo, "outputs", "site-deploys", "journal.jsonl"));
    assert.ok(!journalPath(repo).includes("services/download-analytics"), "日志不能落在站点目录里");
  } finally {
    await fs.rm(repo, { recursive: true, force: true });
  }
});

test("appending and reading the journal keeps every line and survives a corrupt one", async () => {
  const repo = await tempRepo();
  try {
    assert.deepEqual(await readJournal(repo), [], "没有日志时返回空数组，不是报错");
    await appendJournal(repo, { phase: "pending", commit: "a" });
    await appendJournal(repo, { phase: "complete", deployment_id: "d" });
    await fs.appendFile(journalPath(repo), "{not json}\n");
    const events = await readJournal(repo);
    assert.equal(events.length, 3);
    assert.equal(events[0].phase, "pending");
    assert.ok(events[0].ts, "每一行都要自带时间");
    assert.equal(events[1].phase, "complete");
    assert.equal(events[2].phase, "unreadable", "坏行要标出来，不能让整份日志读不出来");
  } finally {
    await fs.rm(repo, { recursive: true, force: true });
  }
});

test("a second deploy is refused while the first holder is alive", async () => {
  const repo = await tempRepo();
  try {
    const first = await acquireDeployLock(repo, { command: "ship" });
    assert.equal(first.acquired, true);
    await assert.rejects(acquireDeployLock(repo, { command: "ship" }), /another deploy is running: pid \d+/);
    await releaseDeployLock(repo);
    const second = await acquireDeployLock(repo, { command: "ship" });
    assert.equal(second.acquired, true, "释放之后必须能再拿到");
    await releaseDeployLock(repo);
  } finally {
    await fs.rm(repo, { recursive: true, force: true });
  }
});

test("a lock left by a dead process is reclaimed once", async () => {
  const repo = await tempRepo();
  try {
    await fs.mkdir(dirname(lockPath()), { recursive: true });
    await fs.writeFile(lockPath(), JSON.stringify({ pid: 999999, command: "ship", started_at: "2026-10-30T00:00:00Z" }) + "\n");
    const lock = await acquireDeployLock(repo, { alive: () => false });
    assert.equal(lock.acquired, true);
    assert.equal(processAlive(999999), false, "随便挑一个不存在的 pid 必须判为不在");
    await releaseDeployLock(repo);
  } finally {
    await fs.rm(repo, { recursive: true, force: true });
  }
});

test("releasing twice is harmless", async () => {
  const repo = await tempRepo();
  try {
    await releaseDeployLock(repo);
    const lock = await acquireDeployLock(repo, { command: "rollback" });
    assert.equal(lock.acquired, true);
    await releaseDeployLock(repo);
    await releaseDeployLock(repo);
    assert.equal(await readJournal(repo).then(events => events.length), 0);
  } finally {
    await fs.rm(repo, { recursive: true, force: true });
  }
});

test("releasing a lock that another process owns leaves it in place", async () => {
  const repo = await tempRepo();
  try {
    await fs.mkdir(dirname(lockPath()), { recursive: true });
    const foreign = { pid: 1, command: "release-deploy", started_at: "2026-10-30T00:00:00Z" };
    await fs.writeFile(lockPath(), JSON.stringify(foreign) + "\n");
    await releaseDeployLock(repo);
    assert.equal(JSON.parse(await fs.readFile(lockPath(), "utf8")).pid, 1, "不能删别人的锁");
  } finally {
    await fs.rm(repo, { recursive: true, force: true });
  }
});

test("a lock whose process start time does not match is reclaimed even if the pid is alive", async () => {
  const repo = await tempRepo();
  try {
    await fs.mkdir(dirname(lockPath()), { recursive: true });
    await fs.writeFile(lockPath(), JSON.stringify({ pid: process.pid, command: "ship",
      started_at: "2026-10-30T00:00:00Z", process_started_at: "Thu Jan  1 00:00:00 1970" }) + "\n");
    const lock = await acquireDeployLock(repo, { command: "ship" });
    assert.equal(lock.acquired, true, "进程号被复用（启动时刻对不上）时必须回收");
    await releaseDeployLock(repo);
  } finally {
    await fs.rm(repo, { recursive: true, force: true });
  }
});

test("the journal keeps only the most recent entries", async () => {
  const repo = await tempRepo();
  try {
    const total = JOURNAL_MAX_LINES + 5;
    for (let index = 0; index < total; index += 1) await appendJournal(repo, { phase: "complete", n: index });
    const events = await readJournal(repo);
    assert.equal(events.length, JOURNAL_MAX_LINES, "日志必须轮转，不能无限增长");
    assert.equal(events[0].n, 5, "保留的是最近 500 条");
    assert.equal(events.at(-1).n, total - 1);
  } finally {
    await fs.rm(repo, { recursive: true, force: true });
  }
});
