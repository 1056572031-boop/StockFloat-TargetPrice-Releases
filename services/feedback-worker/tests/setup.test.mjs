import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { generateKeyPairSync } from 'node:crypto';
import { OWNER, REPOSITORY, startSetup } from '../setup-github-app.mjs';
import { readCloudflareToken } from '../cloudflare-auth.mjs';
import { pagesConfiguration } from '../pages-configuration.mjs';

const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const pem = privateKey.export({ type: 'pkcs1', format: 'pem' });
const repo = { id: 42, private: true, full_name: REPOSITORY, owner: { id: 7 } };

test('failed credential commands never expose captured secret output in exceptions', () => {
  const secret = 'SYNTHETIC-SECRET-DO-NOT-LOG';
  for (const result of [{ status: 1, stdout: secret }, { status: 0, stdout: secret }]) {
    assert.throws(() => readCloudflareToken('/synthetic/wrangler', () => result), error => !String(error).includes(secret));
  }
  assert.equal(readCloudflareToken('/synthetic/wrangler', () => ({ status: 0, stdout: JSON.stringify({ token: secret }) })), secret);
});

test('Pages configuration separates account selection and never writes private key as a plain variable', () => {
  const config = { name: 'synthetic', account_id: 'owner-account', compatibility_date: '2026-10-19',
    vars: { GITHUB_RELEASE_ID: '1', GITHUB_PRIVATE_KEY_PKCS8: 'MUST-NOT-BE-COPIED' },
    d1_databases: [{ database_name: 'budget', database_id: 'approved-database' }] };
  const app = { repository: REPOSITORY, app_id: 1, installation_id: 2 };
  const result = pagesConfiguration(config, app, '/synthetic/output', true);
  assert.equal(result.account_id, undefined);
  assert.equal(result.vars.GITHUB_PRIVATE_KEY_PKCS8, undefined);
  assert.equal(result.vars.ENABLED, 'true');
  assert.equal(result.d1_databases[0].binding, 'DB');
  assert.throws(() => pagesConfiguration(config, { ...app, repository: 'other/repo' }, '/output', true));
});

async function fixture(t, { selected = true, count = 1 } = {}) {
  const directory = await fs.mkdtemp(join(tmpdir(), 'skyrocket-app-setup-'));
  const calls = [];
  const api = async (path, options = {}) => {
    calls.push({ path, method: options.method });
    if (path.includes('/app-manifests/')) return { id: 123, owner: { login: OWNER }, pem, html_url: 'https://github.com/apps/synthetic-app' };
    if (path.endsWith('/access_tokens')) return { token: 'synthetic-only' };
    if (path.startsWith('/app/installations/')) return { app_id: 123, account: { login: OWNER },
      repository_selection: selected ? 'selected' : 'all', permissions: { contents: 'write', metadata: 'read' } };
    if (path.startsWith('/installation/repositories')) return { total_count: count, repositories: [repo] };
    throw new Error('Unexpected setup API route');
  };
  const setup = await startSetup({ directory, workerURL: 'https://synthetic.workers.dev', port: 0,
    openBrowser: false, loadRepository: () => repo, api });
  t.after(async () => { setup.server.closeAllConnections(); setup.server.close(); await fs.rm(directory, { recursive: true, force: true }); });
  const origin = new URL(setup.url).origin;
  const state = new URL(setup.url).searchParams.get('state');
  return { setup, directory, origin, state, calls };
}

test('setup rejects forged callbacks and does not write configuration before selected-repo authorization', async t => {
  const f = await fixture(t, { selected: false });
  assert.equal((await fetch(f.origin + '/created?state=forged&code=synthetic')).status, 403);
  const created = await fetch(f.origin + '/created?state=' + f.state + '&code=synthetic-code', { redirect: 'manual' });
  assert.equal(created.status, 302);
  assert.match(created.headers.get('Location'), /^https:\/\/github.com\/apps\/synthetic-app\/installations\/new/);
  const refused = await fetch(f.origin + '/installed?state=' + f.state + '&installation_id=456');
  assert.equal(refused.status, 400);
  await assert.rejects(fs.access(join(f.directory, 'configuration.json')));
  assert.equal(f.calls.filter(x => x.path.endsWith('/access_tokens')).length, 0);
});

test('setup verifies the sole private repository and stores a restricted PKCS8 secret outside source', async t => {
  const f = await fixture(t);
  await fetch(f.origin + '/created?state=' + f.state + '&code=synthetic-code', { redirect: 'manual' });
  const result = await fetch(f.origin + '/installed?state=' + f.state + '&installation_id=456');
  assert.equal(result.status, 200);
  const ready = await f.setup.completion;
  const configuration = JSON.parse(await fs.readFile(ready.config_path, 'utf8'));
  assert.equal(configuration.repository, REPOSITORY);
  assert.equal(configuration.installation_id, '456');
  const key = await fs.readFile(configuration.key_path, 'utf8');
  assert.ok(key.startsWith(['-----BEGIN', 'PRIVATE KEY-----'].join(' ')));
  assert.equal((await fs.stat(configuration.key_path)).mode & 0o777, 0o600);
  assert.equal((await fs.stat(f.directory)).mode & 0o777, 0o700);
  assert.ok(!('token' in configuration));
});

test('setup refuses a selected installation with additional repositories', async t => {
  const f = await fixture(t, { count: 2 });
  await fetch(f.origin + '/created?state=' + f.state + '&code=synthetic-code', { redirect: 'manual' });
  const response = await fetch(f.origin + '/installed?state=' + f.state + '&installation_id=456');
  assert.match(await response.text(), /Only select repositories/);
  await assert.rejects(fs.access(join(f.directory, 'configuration.json')));
});

test('query-free localhost callbacks preserve anti-forgery state in their path', async t => {
  const f = await fixture(t);
  assert.equal(new URL(f.setup.url).hostname, 'localhost');
  const created = await fetch(f.origin + '/created/' + f.state + '?code=synthetic-code', { redirect: 'manual' });
  assert.equal(created.status, 302);
  const refused = await fetch(f.origin + '/installed/' + f.state + '?installation_id=456&state=forged');
  assert.equal(refused.status, 403);
  const ready = await fetch(f.origin + '/installed/' + f.state + '?installation_id=456');
  assert.equal(ready.status, 200);
  assert.ok((await f.setup.completion).config_path);
});
