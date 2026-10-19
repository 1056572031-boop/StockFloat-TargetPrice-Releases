import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { generateKeyPairSync, randomBytes, verify } from 'node:crypto';
import { ZipWriter, Uint8ArrayWriter, Uint8ArrayReader } from '@zip.js/zip.js';
import worker, { GitHubStore, INBOX_TAG, POLICY, REPOSITORY, digest, strictJson, unpackRequest, validateArchive, validateState } from '../src/index.mjs';
import { appJwt, manifest } from '../setup-github-app.mjs';

const encoder = new TextEncoder();
const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const pem = privateKey.export({ type: 'pkcs8', format: 'pem' });
let installationSequence = 10000;
const key = () => randomBytes(16).toString('hex');
const state = () => ({ app_version: '1.4.96', bundle_id: 'com.skyrocket.app', run_id: 'abcdef',
  phase: 'user_feedback', capture_started_at: '2026-10-19T00:00:00Z', capture_completed_at: '2026-10-19T00:00:00Z',
  health: null, counts: { synthetic_error_count: 1 }, summary: { capture_issue_count: 0, loaded_modules: 1 },
  omitted: ['all_real_user_data'], truncated: [] });

async function archiveFor({ text = 'SYNTHETIC TEST ONLY', value = state(), extra = false, wrongHash = false, symlink = false, problemName = 'problem.txt' } = {}) {
  const files = { 'state.json': encoder.encode(JSON.stringify(value)), 'problem.txt': encoder.encode(text) };
  const meta = { schema_version: 1, diagnostic_payload_version: 1, app_version: value.app_version,
    bundle_id: value.bundle_id, run_id: value.run_id, capture_started_at: value.capture_started_at,
    capture_completed_at: value.capture_completed_at, omitted: value.omitted, truncated: value.truncated,
    files: await Promise.all(Object.entries(files).map(async ([name, bytes]) => ({ name, size_bytes: bytes.length, sha256: wrongHash ? '0'.repeat(64) : await digest(bytes) }))) };
  files['manifest.json'] = encoder.encode(JSON.stringify(meta));
  if (extra) files['config.json'] = encoder.encode('{"target":999}');
  const writer = new ZipWriter(new Uint8ArrayWriter(), { useWebWorkers: false });
  for (const [name, bytes] of Object.entries(files)) {
    await writer.add(name === 'problem.txt' ? problemName : name, new Uint8ArrayReader(bytes), { useWebWorkers: false,
      ...(symlink && name === 'problem.txt' ? { unixMode: 0o120777 } : {}) });
  }
  const archive = await writer.close();
  const envelope = { schema_version: 1, diagnostic_payload_version: 1, app_version: value.app_version,
    consent_policy_version: POLICY, outbox_id: key(), idempotency_key: key(),
    size_bytes: archive.length, archive_sha256: await digest(archive) };
  return { archive, envelope };
}

function wire({ envelope, archive }) {
  const header = encoder.encode(JSON.stringify(envelope));
  const bytes = new Uint8Array(4 + header.length + archive.length);
  new DataView(bytes.buffer).setUint32(0, header.length);
  bytes.set(header, 4); bytes.set(archive, 4 + header.length);
  return bytes;
}

function database() {
  const db = new DatabaseSync(':memory:');
  db.exec(readFileSync(new URL('../schema.sql', import.meta.url), 'utf8'));
  return { native: db, prepare(sql) { return { bind(...params) { return {
    async first() { return db.prepare(sql).get(...params) ?? null; },
    async run() { db.prepare(sql).run(...params); return { success: true }; },
  }; } }; } };
}

class FakeGitHub {
  assets = new Map();
  private = true;
  loseUpload = false;
  redirect = false;
  mintCount = 0;
  fetch = async (input, options = {}) => {
    const url = new URL(input);
    const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
    if (url.hostname === 'release-assets.githubusercontent.com') {
      assert.equal(options.headers?.Authorization, undefined);
      return new Response(this.assets.get(Number(url.pathname.slice(1))).bytes);
    }
    assert.ok(['api.github.com', 'uploads.github.com'].includes(url.hostname));
    assert.equal(options.redirect, 'manual');
    if (url.pathname.startsWith('/app/installations/')) {
      this.mintCount++;
      const jwt = options.headers.Authorization.slice(7);
      const [head, payload, signature] = jwt.split('.');
      assert.ok(verify('RSA-SHA256', Buffer.from(head + '.' + payload), publicKey, Buffer.from(signature, 'base64url')));
      assert.deepEqual(JSON.parse(options.body), { repositories: ['Skyrocket-Private-Feedback'], permissions: { contents: 'write' } });
      return json({ token: 'test-installation-token', expires_at: new Date(Date.now() + 3600000).toISOString(),
        permissions: { contents: 'write', metadata: 'read' }, repository_selection: 'selected',
        repositories: [{ full_name: REPOSITORY, private: true,
          ...Object.fromEntries(Array.from({ length: 70 }, (_, n) => ['metadata_' + n, null])) }] }, 201);
    }
    assert.equal(options.headers.Authorization, 'Bearer test-installation-token');
    if (url.pathname === '/repos/' + REPOSITORY) return json({ private: this.private, full_name: REPOSITORY,
      ...Object.fromEntries(Array.from({ length: 70 }, (_, n) => ['metadata_' + n, null])) });
    if (url.pathname.endsWith('/releases/1')) return json({ id: 1, tag_name: INBOX_TAG, draft: true });
    if (url.pathname.endsWith('/releases/1/assets')) {
      if (options.method === 'POST') {
        const name = url.searchParams.get('name');
        if ([...this.assets.values()].some(x => x.name === name)) return json({ error: 'exists' }, 422);
        const id = this.assets.size + 1;
        const bytes = new Uint8Array(options.body);
        const value = { id, name, size: bytes.length, bytes };
        this.assets.set(id, value);
        if (this.loseUpload) { this.loseUpload = false; throw new Error('lost response after object stored'); }
        return json({ id, name, size: bytes.length }, 201);
      }
      return json([...this.assets.values()].map(({ bytes, ...rest }) => rest));
    }
    const match = /\/releases\/assets\/(\d+)$/.exec(url.pathname);
    if (match) {
      if (this.redirect) return new Response(null, { status: 302, headers: { Location: 'https://release-assets.githubusercontent.com/' + match[1] } });
      const asset = this.assets.get(Number(match[1]));
      return asset ? new Response(asset.bytes) : json({ error: 'missing' }, 404);
    }
    throw new Error('Unexpected GitHub route');
  };
}

function environment() {
  return { ENABLED: 'true', DB: database(), GITHUB_APP_ID: '1', GITHUB_INSTALLATION_ID: String(++installationSequence),
    GITHUB_PRIVATE_KEY_PKCS8: pem, GITHUB_RELEASE_ID: '1' };
}
function request(packet, path = '/v1/feedback') {
  const body = wire(packet);
  return new Request('https://receiver.example' + path, { method: 'POST', body,
    headers: { 'Content-Type': 'application/vnd.skyrocket.feedback-v1', 'Content-Length': String(body.length) } });
}
async function withGitHub(remote, action) {
  const original = globalThis.fetch;
  globalThis.fetch = remote.fetch;
  try { return await action(); } finally { globalThis.fetch = original; }
}

test('strict JSON refuses duplicate keys, comments, excessive depth and unsafe numbers', () => {
  for (const value of ['{"x":1,"x":2}', '{/*comment*/"x":1}', '{"x":NaN}', '{"x":9007199254740993}', '['.repeat(10) + '0' + ']'.repeat(10)]) {
    assert.throws(() => strictJson(value));
  }
  assert.deepEqual(strictJson('{"count":2}'), { count: 2 });
});

test('wire and ZIP are bound to allowed fields, hashes, sizes and member types', async () => {
  const packet = await archiveFor();
  assert.deepEqual((await unpackRequest(wire(packet))).envelope, packet.envelope);
  assert.equal((await validateArchive(packet.archive, packet.envelope)).run_id, 'abcdef');
  for (const options of [{ extra: true }, { wrongHash: true }, { symlink: true }, { text: 'x'.repeat(4001) }]) {
    const malformed = await archiveFor(options);
    await assert.rejects(() => validateArchive(malformed.archive, malformed.envelope));
  }
  for (const problemName of ['__proto__', 'constructor']) {
    const malformed = await archiveFor({ problemName });
    await assert.rejects(() => validateArchive(malformed.archive, malformed.envelope), /zip_member/);
  }
  await assert.rejects(() => unpackRequest(new Uint8Array(2)));
  await assert.rejects(() => unpackRequest(wire({ ...packet, envelope: { ...packet.envelope, archive_sha256: '0'.repeat(64) } })));
  for (const change of [{ health: true }, { summary: { path: 'secret' } }, { counts: { 'user/path': 1 } }, { run_id: 'unknown' }, { omitted: ['unknown'] }]) {
    assert.throws(() => validateState({ ...state(), ...change }, packet.envelope));
  }
});

test('private GitHub publication reads actual bytes back and retries with one case', async () => {
  const remote = new FakeGitHub();
  const store = new GitHubStore('test-installation-token', '1', remote.fetch);
  const packet = await archiveFor();
  remote.redirect = true;
  const receipt = await store.publish(packet.envelope, packet.archive);
  assert.equal(receipt.archive_sha256, packet.envelope.archive_sha256);
  assert.deepEqual(await store.publish(packet.envelope, packet.archive), receipt);
  assert.equal(remote.assets.size, 2);
  remote.private = false;
  await assert.rejects(() => store.publish(packet.envelope, packet.archive));
  remote.private = true;
  remote.assets.delete(receipt.asset_id);
  await assert.rejects(() => store.publish(packet.envelope, packet.archive));
});

test('receipt loss does not create duplicate objects, corrupt remote bytes cannot succeed', async () => {
  const remote = new FakeGitHub();
  const store = new GitHubStore('test-installation-token', '1', remote.fetch);
  const packet = await archiveFor();
  remote.loseUpload = true;
  await assert.rejects(() => store.publish(packet.envelope, packet.archive));
  assert.equal(remote.assets.size, 1);
  const receipt = await store.publish(packet.envelope, packet.archive);
  assert.equal(remote.assets.size, 2);
  remote.assets.get(receipt.asset_id).bytes = encoder.encode('corrupt');
  await assert.rejects(() => store.publish(packet.envelope, packet.archive));
});

test('GitHub transport preserves the Workers global fetch receiver contract', async () => {
  const remote = new FakeGitHub();
  function receiverChecked(input, options) {
    if (this !== globalThis) throw new TypeError('Illegal invocation: incorrect fetch receiver');
    return remote.fetch(input, options);
  }
  const packet = await archiveFor();
  const receipt = await new GitHubStore('test-installation-token', '1', receiverChecked).publish(packet.envelope, packet.archive);
  assert.equal(receipt.archive_sha256, packet.envelope.archive_sha256);
});

test('disabled service, invalid headers and quotas never transmit data', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    assert.equal((await worker.fetch(request(packet), { ENABLED: 'false' })).status, 503);
    assert.equal((await worker.fetch(new Request('https://receiver.example/health'), { ENABLED: 'false' })).status, 200);
    assert.equal((await worker.fetch(new Request('https://receiver.example/unknown'), env)).status, 404);
    assert.equal((await worker.fetch(new Request('https://receiver.example/v1/feedback', { method: 'POST' }), env)).status, 400);
    env.DB.native.prepare('INSERT INTO budget VALUES (?,100)').run(new Date().toISOString().slice(0, 10));
    assert.equal((await worker.fetch(request(packet), env)).status, 429);
  });
  assert.equal(remote.assets.size, 0);
  env.DB.native.close();
});

test('full Worker flow uses restricted GitHub App permissions and persistent SQL identity', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    const first = await worker.fetch(request(packet), env);
    assert.equal(first.status, 200, await first.clone().text());
    const receipt = await first.json();
    const retry = await worker.fetch(request(packet), env);
    assert.equal(retry.status, 200);
    assert.deepEqual(await retry.json(), receipt);
    assert.equal(remote.assets.size, 2);
    assert.equal(remote.mintCount, 1);
    const other = await archiveFor({ text: 'different data under same identity' });
    other.envelope.idempotency_key = packet.envelope.idempotency_key;
    assert.equal((await worker.fetch(request(other), env)).status, 409);
    assert.equal(env.DB.native.prepare('SELECT state FROM cases').get().state, 'received');
  });
  env.DB.native.close();
});

test('failed GitHub response releases only its own lease and can retry', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  remote.loseUpload = true;
  await withGitHub(remote, async () => {
    assert.equal((await worker.fetch(request(packet), env)).status, 503);
    assert.equal(env.DB.native.prepare('SELECT state FROM cases').get().state, 'unknown');
    assert.equal((await worker.fetch(request(packet), env)).status, 200);
    assert.equal(remote.assets.size, 2);
  });
  env.DB.native.close();
});

async function heldPublication(t, action) {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  let entered, release;
  const waiting = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const original = remote.fetch;
  remote.fetch = async (url, options) => {
    if (new URL(url).pathname === '/repos/' + REPOSITORY) { entered(); await held; }
    return original(url, options);
  };
  t.after(() => { release(); env.DB.native.close(); });
  await withGitHub(remote, async () => {
    const first = worker.fetch(request(packet), env);
    await waiting;
    try { await action({ packet, env, remote, first, release }); } finally { release(); await first; }
  });
}

test('a concurrent retry cannot clear the active upload lease or duplicate a case', async t => {
  await heldPublication(t, async ({ packet, env, remote, first, release }) => {
    const second = await worker.fetch(request(packet), env);
    assert.equal(second.status, 409);
    assert.equal((await second.json()).error, 'submission_in_progress');
    assert.ok(env.DB.native.prepare('SELECT lease_until FROM cases').get().lease_until > Date.now());
    release();
    assert.equal((await first).status, 200);
    assert.equal(remote.assets.size, 2);
  });
});

test('a superseded worker cannot overwrite or release the replacement lease', async t => {
  await heldPublication(t, async ({ env, first, release }) => {
    const replacement = crypto.randomUUID();
    env.DB.native.prepare("UPDATE cases SET lease_token=?, lease_until=?").run(replacement, Date.now() + 120000);
    release();
    const result = await first;
    assert.equal(result.status, 409);
    assert.equal((await result.json()).error, 'lease_superseded');
    const saved = env.DB.native.prepare('SELECT lease_token,state FROM cases').get();
    assert.equal(saved.lease_token, replacement);
    assert.equal(saved.state, 'uploading');
  });
});

test('case capacity is a persistent hard limit before any GitHub request', async () => {
  const packet = await archiveFor();
  packet.envelope.idempotency_key = 'f'.repeat(32);
  const env = environment();
  for (let n = 0; n < 1000; n++) env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'new', 0, '', NULL)").run(n.toString(16).padStart(32, '0'), '0'.repeat(64));
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    const response = await worker.fetch(request(packet), env);
    assert.equal(response.status, 429);
    assert.equal((await response.json()).error, 'case_budget');
    assert.equal(remote.mintCount, 0);
  });
  env.DB.native.close();
});

test('GitHub App setup requests only private feedback contents and signs standard JWTs', () => {
  const value = manifest({ origin: 'http://127.0.0.1:8863', workerURL: 'https://example.workers.dev', nonce: 'a'.repeat(32) });
  assert.equal(value.public, false);
  assert.equal(new URL(value.redirect_url).hostname, 'localhost');
  assert.equal(new URL(value.redirect_url).search, '');
  assert.equal(new URL(value.setup_url).search, '');
  assert.deepEqual(value.default_permissions, { contents: 'write' });
  assert.equal(value.hook_attributes.active, false);
  const jwt = appJwt(1, pem, 1000000);
  const [header, payload, signature] = jwt.split('.');
  assert.ok(verify('RSA-SHA256', Buffer.from(header + '.' + payload), publicKey, Buffer.from(signature, 'base64url')));
  assert.equal(JSON.parse(Buffer.from(payload, 'base64url')).iss, '1');
});
