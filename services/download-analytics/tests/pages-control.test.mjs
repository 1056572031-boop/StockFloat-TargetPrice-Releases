import { test } from "node:test";
import assert from "node:assert/strict";
import { commandText, isRollbackTarget, previewBranchForCommit, previewWranglerConfig } from "../pages-control.mjs";

const ok = { name: "deploy", status: "success" };
const deployment = (extra = {}) => ({ id: "aaaaaaaa-0000-0000-0000-000000000001", environment: "production", latest_stage: ok, ...extra });
const CURRENT = "bbbbbbbb-0000-0000-0000-000000000002";

test("importing the control script has no side effects and needs no credentials", () => {
  // 能走到这里，说明 import 没有读钥匙串、没有调 Cloudflare、没有 process.exit。
  assert.equal(typeof isRollbackTarget, "function");
});

test("the deployment helper accepts silent successful commands", () => {
  // build-pages.mjs 把输出交给终端时 execFileSync 返回 null；这不是失败。
  assert.equal(commandText(null), "");
  assert.equal(commandText(" output\n"), "output");
});

test("only an earlier, successful production deployment is a valid rollback target", () => {
  assert.equal(isRollbackTarget(deployment(), CURRENT), true);
  assert.equal(isRollbackTarget(undefined, CURRENT), false, "列表里找不到的 ID 不能回滚");
  assert.equal(isRollbackTarget(deployment({ id: CURRENT }), CURRENT), false, "已经是线上版本，没有可回滚的");
  assert.equal(isRollbackTarget(deployment({ environment: "preview" }), CURRENT), false, "预览部署不是合法目标");
  assert.equal(isRollbackTarget(deployment({ latest_stage: { name: "deploy", status: "failure" } }), CURRENT), false, "失败的部署不能回滚");
  assert.equal(isRollbackTarget(deployment({ latest_stage: { name: "build", status: "success" } }), CURRENT), false, "只认部署阶段成功");
  assert.equal(isRollbackTarget(deployment({ latest_stage: { name: "deploy", status: "active" } }), CURRENT), false, "还在进行中的部署不能回滚");
});

test("the preview upload config always declares the isolated database and never the production one", () => {
  const previewDb = "51e53275-0272-44e6-b30d-87fd8aec1145";
  const config = previewWranglerConfig("/tmp/out", previewDb);
  assert.deepEqual(config.d1_databases, [{ binding: "DB", database_name: "skyrocket-downloads-preview", database_id: previewDb }],
    "漏写 D1 会让 Pages 清空预览环境的绑定，健康接口报 database=false");
  assert.equal(config.pages_build_output_dir, "/tmp/out");
  assert.throws(() => previewWranglerConfig("/tmp/out", "088fcda4-015d-4116-a7ba-bafe50d0346f"), /never bind the production database/);
  for (const bad of ["", undefined, null]) {
    assert.throws(() => previewWranglerConfig("/tmp/out", bad), /preview D1 id is required/);
  }
});

test("the preview branch name is derived from a full commit hash and nothing else", () => {
  assert.equal(previewBranchForCommit("0123456789abcdef0123456789abcdef01234567"), "review-0123456789ab");
  for (const bad of ["", "HEAD", "0123456789ab", "0123456789ABCDEF0123456789ABCDEF01234567", "../../main", "main; rm -rf /"]) {
    assert.throws(() => previewBranchForCommit(bad), /commit is unavailable/, `不合规的 commit 值 ${JSON.stringify(bad)} 必须被拒绝`);
  }
  assert.notEqual(previewBranchForCommit("0123456789abcdef0123456789abcdef01234567"), "main", "预览分支永远不能撞生产分支");
});
