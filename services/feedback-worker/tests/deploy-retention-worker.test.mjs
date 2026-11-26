import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, copyFileSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const source = fileURLToPath(new URL('../deploy-retention-worker.mjs', import.meta.url));
// Deliberately invalid fixture: the deploy script checks the PEM header, not key validity.
const key = ['-----BEGIN', 'PRIVATE KEY-----\ntest-private-key\n'].join(' ');

function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'retention-deploy-test-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const script = join(dir, 'deploy.mjs');
  const config = join(dir, 'config.json');
  const keyFile = join(dir, 'key.pem');
  const calls = join(dir, 'calls.jsonl');
  const wrangler = join(dir, 'node_modules/.bin/wrangler');
  copyFileSync(source, script);
  mkdirSync(dirname(wrangler), { recursive: true });
  writeFileSync(wrangler, `#!/usr/bin/env node\nconst fs = require('node:fs');\nfs.appendFileSync(process.env.TEST_CALLS, JSON.stringify({args: process.argv.slice(2), input: fs.readFileSync(0, 'utf8')}) + '\\n');\n`, { mode: 0o755 });
  writeFileSync(config, JSON.stringify({ name: 'test', account_id: 'account', d1_databases: [{ database_id: '12345678-test', database_name: 'test-db' }], vars: { GITHUB_RELEASE_ID: '123' } }));
  writeFileSync(keyFile, key);
  const args = [script, '--configuration', config, '--app-id', '123', '--installation-id', '456', '--private-key-file', keyFile];
  const run = (...extra) => spawnSync(process.execPath, [...args, ...extra], { encoding: 'utf8', env: { ...process.env, TEST_CALLS: calls } });
  return { dir, config, keyFile, calls, run };
}

test('default plan needs no admin token, exposes no secret, and does not deploy', t => {
  const f = fixture(t);
  const result = f.run();
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.dry_run, true);
  assert.deepEqual(output.plan.secrets, ['GITHUB_PRIVATE_KEY_PKCS8']);
  assert.match(JSON.stringify(output.plan), /FEEDBACK_ADMIN_DELETE_TOKEN/);
  assert.match(JSON.stringify(output.plan), /receiver.*separately/i);
  assert.doesNotMatch(result.stdout, /test-private-key/);
  assert.equal(existsSync(f.calls), false);
  assert.equal(existsSync(join(f.dir, 'config.resolved.jsonc')), false);
});

test('apply writes only the GitHub private key using an offline wrangler stub', t => {
  const f = fixture(t);
  const result = f.run('--apply');
  assert.equal(result.status, 0, result.stderr);
  const calls = readFileSync(f.calls, 'utf8').trim().split('\n').map(JSON.parse);
  assert.deepEqual(calls.map(call => call.args[0]), ['d1', 'secret', 'deploy']);
  assert.deepEqual(calls.filter(call => call.args[0] === 'secret').map(call => call.args[2]), ['GITHUB_PRIVATE_KEY_PKCS8']);
  assert.equal(calls[1].input, key + '\n');
  assert.doesNotMatch(result.stdout, /test-private-key/);
});

test('invalid deployment identity remains rejected before any action', t => {
  const f = fixture(t);
  writeFileSync(f.config, JSON.stringify({ account_id: 'account', d1_databases: [{ database_id: '00000000-placeholder' }], vars: { GITHUB_RELEASE_ID: '123' } }));
  const result = f.run('--apply');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not bound/);
  assert.equal(existsSync(f.calls), false);
});

test('invalid private key remains rejected before any action', t => {
  const f = fixture(t);
  writeFileSync(f.keyFile, 'invalid');
  const result = f.run('--apply');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /secrets are invalid/);
  assert.equal(existsSync(f.calls), false);
});
