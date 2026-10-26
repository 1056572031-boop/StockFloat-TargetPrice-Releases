import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { generateKeyPairSync, randomBytes, verify } from 'node:crypto';
import { ZipReader, ZipWriter, Uint8ArrayWriter, Uint8ArrayReader } from '@zip.js/zip.js';
import worker, { CASE_PRUNE_AT, CLIENT_DAILY_LIMIT, FULL_POLICY, FULL_V3_POLICY, GitHubStore, INBOX_TAG, LEASE_MS, POLICY, PUBLISH_DEADLINE_MS, REFUND_DAILY_LIMIT, REPOSITORY, digest, retryAfter, strictJson, unpackRequest, validateArchive, validateState } from '../src/index.mjs';
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

async function archiveFor({ text = 'SYNTHETIC TEST ONLY', value = state(), extra = false, wrongHash = false, symlink = false, problemName = 'problem.txt', diagnostics = null, payload = diagnostics ? 2 : 1 } = {}) {
  const files = { 'state.json': encoder.encode(JSON.stringify(value)), 'problem.txt': encoder.encode(text) };
  if (diagnostics) files['diagnostics.zip'] = diagnostics;
  const meta = { schema_version: 1, diagnostic_payload_version: payload, app_version: value.app_version,
    bundle_id: value.bundle_id, run_id: value.run_id, capture_started_at: value.capture_started_at,
    capture_completed_at: value.capture_completed_at, omitted: value.omitted, truncated: value.truncated,
    files: await Promise.all(Object.entries(files).map(async ([name, bytes]) => ({ name, size_bytes: bytes.length, sha256: wrongHash ? '0'.repeat(64) : await digest(bytes) }))) };
  files['manifest.json'] = encoder.encode(JSON.stringify(meta));
  if (extra) files['config.json'] = encoder.encode('{"target":999}');
  const writer = new ZipWriter(new Uint8ArrayWriter(), { useWebWorkers: false });
  for (const [name, bytes] of Object.entries(files)) {
    await writer.add(name === 'problem.txt' ? problemName : name, new Uint8ArrayReader(bytes), { useWebWorkers: false,
      ...(name === 'diagnostics.zip' ? { level: 0 } : {}),
      ...(symlink && name === 'problem.txt' ? { unixMode: 0o120777 } : {}) });
  }
  const archive = await writer.close();
  const envelope = { schema_version: 1, diagnostic_payload_version: payload, app_version: value.app_version,
    consent_policy_version: payload === 3 ? FULL_V3_POLICY : diagnostics ? FULL_POLICY : POLICY, outbox_id: key(), idempotency_key: key(),
    size_bytes: archive.length, archive_sha256: await digest(archive) };
  return { archive, envelope };
}

async function v3Diagnostics() {
  const extra = {
    'environment.json': encoder.encode(JSON.stringify({ schema: 'environment.v1', available: true, data: {} })),
    'permissions.json': encoder.encode(JSON.stringify({ schema: 'permissions.v1', available: false, reason: 'no_bundle' })),
    'feedback_preferences.json': encoder.encode(JSON.stringify({ schema: 'feedback_preferences.v1', available: true, data: {} })),
    'feedback_context.json': encoder.encode(JSON.stringify({ schema: 'feedback_context.v1', available: true, data: {} })),
  };
  return fullDiagnostics(extra, { diagnostic_schema_version: 3 });
}

async function fullDiagnostics(extra = {}, exportExtra = {}, omit = []) {
  const files = { 'diagnostics.txt': encoder.encode('SYNTHETIC COMPLETE DIAGNOSTICS ONLY'),
    'runtime.json': encoder.encode('{"net_ok":true,"age":1.5}'), ...extra };
  // feedback_ui.json 自 1.4.104 起随包带上；omit 用来模拟更早版本的客户端。
  for (const name of ['network.json', 'startup_health.json', 'feedback.json', 'feedback_ui.json', 'targets.json', 'glass.json',
    'notify.json', 'universe.json', 'calendar.json', 'state.json', 'storage.json']) files[name] ??= encoder.encode('{}');
  for (const name of omit) delete files[name];
  files['export.json'] = encoder.encode(JSON.stringify({ version: state().app_version, issues: [], ...exportExtra,
    files: Object.entries(files).map(([name, bytes]) => ({ name, size_bytes: bytes.length })) }));
  const writer = new ZipWriter(new Uint8ArrayWriter(), { useWebWorkers: false });
  for (const [name, bytes] of Object.entries(files)) await writer.add(name, new Uint8ArrayReader(bytes), { useWebWorkers: false });
  return writer.close();
}

test('diagnostics that carry feedback_ui.json are accepted, and so are older ones without it', async () => {
  // 事故：服务端白名单漏了 feedback_ui.json，1.4.104 起每个完整反馈都被 422 diagnostic_member 拒收。
  const modern = await fullDiagnostics({}, {}, []);
  const legacy = await fullDiagnostics({}, {}, ['feedback_ui.json']);
  for (const [label, diagnostics] of [['with feedback_ui.json', modern], ['without feedback_ui.json', legacy]]) {
    const packet = await archiveFor({ diagnostics });
    await validateArchive(packet.archive, packet.envelope);
    assert.ok(diagnostics.length > 0, label);
  }
  const named = await fullDiagnostics({}, {}, []);
  const reader = new ZipReader(new Uint8ArrayReader(named), { useWebWorkers: false });
  const names = (await reader.getEntries()).map(entry => entry.filename);
  await reader.close();
  assert.ok(names.includes('feedback_ui.json'), 'the default fixture must exercise the file the real client ships');
});

test('full diagnostics preserve content with a distinct policy and old packets remain accepted', async () => {
  const packet = await archiveFor({ diagnostics: await fullDiagnostics({ 'debug.log': encoder.encode('ERROR synthetic only') }) });
  await validateArchive(packet.archive, packet.envelope);
  const env = environment();
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    const result = await worker.fetch(request(packet), env);
    assert.equal(result.status, 200, await result.clone().text());
    const receipt = await result.json();
    assert.equal(receipt.consent_policy_version, FULL_POLICY);
    assert.deepEqual([...remote.assets.values()].find(value => value.bytes.length === packet.archive.length).bytes, packet.archive);
    const legacy = await archiveFor();
    assert.equal((await worker.fetch(request(legacy), env)).status, 200);
  });
  env.DB.native.close();
});

test('payload v3 requires four fixed metadata snapshots while retaining v1/v2 compatibility', async () => {
  const v3 = await archiveFor({ diagnostics: await v3Diagnostics(), payload: 3 });
  await validateArchive(v3.archive, v3.envelope);
  const env = environment();
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    const result = await worker.fetch(request(v3), env);
    assert.equal(result.status, 200, await result.clone().text());
    const v3Receipt = await result.json();
    assert.equal(v3Receipt.consent_policy_version, FULL_V3_POLICY);
    // 客户端对 v2/v3 完整包的回执都要求 validation_scope；缺了它客户端会把一次成功收件
    // 判成「回执不属于当前批准的 GitHub 反馈」（生产合成包实测）。
    assert.equal(v3Receipt.validation_scope, 'container_sha256_and_member_catalog');
    const incomplete = await archiveFor({ diagnostics: await fullDiagnostics(), payload: 3 });
    await assert.rejects(validateArchive(incomplete.archive, incomplete.envelope));
    assert.equal((await worker.fetch(request(incomplete), env)).status, 422);
  });
  env.DB.native.close();
});

test('a historical v3 receipt missing validation scope remains idempotently recoverable', async () => {
  const packet = await archiveFor({ diagnostics: await v3Diagnostics(), payload: 3 });
  const remote = new FakeGitHub();
  const store = new GitHubStore('test-installation-token', '1', remote.fetch);
  const first = await store.publish(packet.envelope, packet.archive);
  const receiptAsset = [...remote.assets.values()].find(asset => asset.name.endsWith('.receipt.json'));
  const legacy = JSON.parse(new TextDecoder().decode(receiptAsset.bytes));
  delete legacy.validation_scope;
  receiptAsset.bytes = encoder.encode(JSON.stringify(legacy));
  const recovered = await store.publish(packet.envelope, packet.archive);
  assert.equal(recovered.validation_scope, 'container_sha256_and_member_catalog');
  assert.equal(remote.uploadCount, 2, 'recovery must reuse both already-stored assets');
});

test('full payload refuses wrong policy, nested traversal, bad JSON and mismatched capture context', async () => {
  const packet = await archiveFor({ diagnostics: await fullDiagnostics() });
  await assert.rejects(unpackRequest(wire({ ...packet, envelope: { ...packet.envelope, consent_policy_version: POLICY } })));
  for (const extra of [{ '../escape.txt': encoder.encode('no') }, { 'runtime.json': encoder.encode('{"x":1,"x":2}') },
    { 'payload.sh': encoder.encode('no') }]) {
    const invalid = await archiveFor({ diagnostics: await fullDiagnostics(extra) });
    await assert.rejects(validateArchive(invalid.archive, invalid.envelope));
  }
});

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
  uploadCount = 0;
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
        this.uploadCount++;
        if (this.assets.size >= 1000) return json({ error: 'release asset limit' }, 422);
        const name = url.searchParams.get('name');
        if ([...this.assets.values()].some(x => x.name === name)) return json({ error: 'exists' }, 422);
        const id = this.assets.size + 1;
        const bytes = new Uint8Array(options.body);
        const value = { id, name, size: bytes.length, bytes };
        this.assets.set(id, value);
        if (this.loseUpload) { this.loseUpload = false; throw new Error('lost response after object stored'); }
        return json({ id, name, size: bytes.length }, 201);
      }
      const page = Number(url.searchParams.get('page') || 1);
      const perPage = Number(url.searchParams.get('per_page') || 100);
      return json([...this.assets.values()].slice((page - 1) * perPage, page * perPage).map(({ bytes, ...rest }) => rest));
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

function fillUnrelatedAssets(remote, total) {
  while (remote.assets.size < total) {
    const id = remote.assets.size + 1;
    remote.assets.set(id, { id, name: `unrelated-${id}.txt`, size: 0, bytes: new Uint8Array() });
  }
}

test('storage needs two asset slots and refuses before creating a partial case', async () => {
  const packet = await archiveFor();
  const remote = new FakeGitHub();
  fillUnrelatedAssets(remote, 999);
  const store = new GitHubStore('test-installation-token', '1', remote.fetch);
  await assert.rejects(() => store.publish(packet.envelope, packet.archive), error => error.code === 'inbox_asset_capacity');
  assert.equal(remote.uploadCount, 0);
  assert.equal(remote.assets.size, 999);
});

test('one remaining slot completes an existing ZIP and full storage still permits receipt readback', async () => {
  const packet = await archiveFor();
  const remote = new FakeGitHub();
  remote.assets.set(1, { id: 1, name: `feedback-${packet.envelope.idempotency_key}.zip`,
    size: packet.archive.length, bytes: packet.archive });
  fillUnrelatedAssets(remote, 999);
  const store = new GitHubStore('test-installation-token', '1', remote.fetch);
  const receipt = await store.publish(packet.envelope, packet.archive);
  assert.equal(remote.assets.size, 1000);
  assert.equal(remote.uploadCount, 1);
  assert.deepEqual(await store.publish(packet.envelope, packet.archive), receipt);
  assert.equal(remote.uploadCount, 1);
});

test('plaintext description credentials are rejected before GitHub or case creation', async () => {
  for (const text of ['-----BEGIN ' + 'PRIVATE KEY-----\ncontrolled-test-secret', 'Authorization=Bearer controlled-test-secret']) {
    const packet = await archiveFor({ text });
    const env = environment();
    const remote = new FakeGitHub();
    try {
      await withGitHub(remote, async () => {
        const response = await worker.fetch(request(packet), env);
        assert.equal(response.status, 422);
        assert.equal((await response.json()).error, 'diagnostic_secret');
      });
      assert.equal(remote.mintCount, 0);
      assert.equal(remote.assets.size, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 0);
    } finally { env.DB.native.close(); }
  }
});

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
    const health = await worker.fetch(new Request('https://receiver.example/health'), { ENABLED: 'false' });
    assert.equal(health.status, 200);
    // /health 声明的版本必须与实际接受的版本一致：v3 已被接受却不声明，外部探测会据此误判。
    assert.deepEqual((await health.json()).payloadVersions, [1, 2, 3]);
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

test('daily byte protection refuses before GitHub and does not leak a reservation', async t => {
  const RealDate = globalThis.Date;
  const fixed = '2026-01-02T12:00:00Z';
  globalThis.Date = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : [fixed])); }
    static now() { return RealDate.parse(fixed); }
  };
  t.after(() => { globalThis.Date = RealDate; });
  const packet = await archiveFor();
  const env = environment();
  const day = new Date().toISOString().slice(0, 10) + ':bytes';
  env.DB.native.prepare('INSERT INTO budget VALUES (?, ?)').run(day, 256 * 1024 * 1024);
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    const refused = await worker.fetch(request(packet), env);
    assert.equal(refused.status, 429);
    assert.equal((await refused.json()).error, 'daily_byte_budget');
    assert.equal(remote.mintCount, 0);
    env.DB.native.prepare('UPDATE budget SET requests=0 WHERE day=?').run(day);
    assert.equal((await worker.fetch(request(packet), env)).status, 200);
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
  // 永久容量指的是“已收件”的行：它们是回执的唯一副本，永远不清理。
  for (let n = 0; n < 1000; n++) env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'received', 0, '', '{}')").run(n.toString(16).padStart(32, '0'), '0'.repeat(64));
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

function requestFrom(packet, ip) {
  const body = wire(packet);
  return new Request('https://receiver.example/v1/feedback', { method: 'POST', body,
    headers: { 'Content-Type': 'application/vnd.skyrocket.feedback-v1', 'Content-Length': String(body.length),
      ...(ip ? { 'CF-Connecting-IP': ip } : {}) } });
}
function garbage(size = 12) {
  const body = new Uint8Array(size).fill(7);
  return new Request('https://receiver.example/v1/feedback', { method: 'POST', body,
    headers: { 'Content-Type': 'application/vnd.skyrocket.feedback-v1', 'Content-Length': String(size), 'CF-Connecting-IP': '203.0.113.9' } });
}
const budgetRow = (env, key) => env.DB.native.prepare('SELECT requests FROM budget WHERE day=?').get(key)?.requests ?? 0;
const today = () => new Date().toISOString().slice(0, 10);

test('a request refused before GitHub gives its global budget back, so garbage cannot burn the day', async () => {
  const env = environment();
  for (let n = 0; n < 5; n++) assert.equal((await worker.fetch(garbage(), env)).status, 422);
  assert.equal(budgetRow(env, today()), 0);
  assert.equal(budgetRow(env, today() + ':bytes'), 0);
  env.DB.native.close();
});

test('one source cannot exhaust the shared budget: its own bucket stops it with Retry-After', async () => {
  const env = environment();
  for (let n = 0; n < 30; n++) assert.equal((await worker.fetch(garbage(), env)).status, 422);
  const refused = await worker.fetch(garbage(), env);
  assert.equal(refused.status, 429);
  assert.equal((await refused.json()).error, 'daily_client_budget');
  assert.ok(Number(refused.headers.get('Retry-After')) > 0);
  assert.ok(Number(refused.headers.get('Retry-After')) <= 86400);
  // 别的来源不受影响，全局额度也没被占着。
  const packet = await archiveFor();
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    assert.equal((await worker.fetch(requestFrom(packet, '198.51.100.4'), env)).status, 200);
  });
  env.DB.native.close();
});

test('the client bucket stores a hash, never the address', async () => {
  const env = environment();
  await worker.fetch(garbage(), env);
  const rows = env.DB.native.prepare('SELECT day FROM budget').all().map(row => row.day);
  assert.ok(rows.some(day => day.includes(':client:')));
  assert.ok(rows.every(day => !day.includes('203.0.113.9')));
  env.DB.native.close();
});

test('once GitHub has been used the charge stays, even if the publication fails', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  remote.loseUpload = true;
  await withGitHub(remote, async () => {
    assert.equal((await worker.fetch(request(packet), env)).status, 503);
  });
  assert.equal(budgetRow(env, today()), 1);
  env.DB.native.close();
});

test('a body that is not a real archive is a 422 the client must not retry, not a 503', async () => {
  const good = await archiveFor();
  const archive = new Uint8Array(200).fill(9);
  const envelope = { ...good.envelope, size_bytes: archive.length, archive_sha256: await digest(archive) };
  const env = environment();
  const refused = await worker.fetch(request({ archive, envelope }), env);
  assert.equal(refused.status, 422);
  const body = await refused.json();
  assert.equal(body.error, 'archive_invalid');
  assert.equal(body.phase, 'archive');
  env.DB.native.close();
});

test('infrastructure failures stay 503 and name the phase that failed', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  remote.loseUpload = true;
  await withGitHub(remote, async () => {
    const failed = await worker.fetch(request(packet), env);
    assert.equal(failed.status, 503);
    const body = await failed.json();
    assert.equal(body.error.startsWith('github_') || body.error === 'submission_unconfirmed', true);
    assert.equal(body.phase, 'github_storage');
  });
  env.DB.native.close();
});

test('daily budget refusals say when to come back', async () => {
  const packet = await archiveFor();
  const env = environment();
  env.DB.native.prepare('INSERT INTO budget VALUES (?,100)').run(today());
  const refused = await worker.fetch(request(packet), env);
  assert.equal(refused.status, 429);
  assert.ok(Number(refused.headers.get('Retry-After')) > 0);
  env.DB.native.close();
});

test('permanent capacity refusals do not pretend to be temporary', async () => {
  const packet = await archiveFor();
  packet.envelope.idempotency_key = 'e'.repeat(32);
  const env = environment();
  for (let n = 0; n < 1000; n++) env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'received', 0, '', '{}')").run(n.toString(16).padStart(32, '0'), '0'.repeat(64));
  const refused = await worker.fetch(request(packet), env);
  assert.equal(refused.status, 429);
  assert.equal((await refused.json()).error, 'case_budget');
  assert.equal(refused.headers.get('Retry-After'), null);
  env.DB.native.close();
});

test('unfinished cases are pruned when the table fills up, received ones never are', async () => {
  const packet = await archiveFor();
  packet.envelope.idempotency_key = 'd'.repeat(32);
  const env = environment();
  for (let n = 0; n < 950; n++) env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'unknown', 0, '', NULL)").run(n.toString(16).padStart(32, '0'), '0'.repeat(64));
  for (let n = 950; n < 1000; n++) env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'received', 0, '', '{}')").run(n.toString(16).padStart(32, '0'), '0'.repeat(64));
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    assert.equal((await worker.fetch(request(packet), env)).status, 200);
  });
  const counts = Object.fromEntries(env.DB.native.prepare('SELECT state, COUNT(*) AS n FROM cases GROUP BY state').all().map(row => [row.state, row.n]));
  assert.equal(counts.received, 51);
  assert.equal(counts.unknown ?? 0, 0);
  env.DB.native.close();
});

test('a case that is still being uploaded is never pruned', async () => {
  const packet = await archiveFor();
  packet.envelope.idempotency_key = 'c'.repeat(32);
  const env = environment();
  for (let n = 0; n < 900; n++) env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'uploading', ?, 'live', NULL)").run(n.toString(16).padStart(32, '0'), '0'.repeat(64), Date.now() + 60000);
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    assert.equal((await worker.fetch(request(packet), env)).status, 200);
  });
  assert.equal(env.DB.native.prepare("SELECT COUNT(*) AS n FROM cases WHERE state='uploading'").get().n, 900);
  env.DB.native.close();
});

test('budget rows older than a week are dropped on the first request of a day', async () => {
  const packet = await archiveFor();
  const env = environment();
  env.DB.native.prepare('INSERT INTO budget VALUES (?, 5)').run('2000-01-01');
  env.DB.native.prepare('INSERT INTO budget VALUES (?, 5)').run('2000-01-01:bytes');
  env.DB.native.prepare('INSERT INTO budget VALUES (?, 5)').run('2000-01-01:client:abc');
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    assert.equal((await worker.fetch(request(packet), env)).status, 200);
  });
  assert.equal(env.DB.native.prepare("SELECT COUNT(*) AS n FROM budget WHERE day LIKE '2000-%'").get().n, 0);
  assert.ok(budgetRow(env, today()) >= 1);
  env.DB.native.close();
});

test('GitHub calls stop at the publish deadline instead of outliving the lease', async () => {
  let calls = 0;
  const store = new GitHubStore('token', '1', async () => { calls += 1; return new Response('{}'); }, Date.now() - 1);
  await assert.rejects(() => store.request(''), error => error.code === 'upstream_timeout' && error.status === 503);
  assert.equal(calls, 0);
});

test('the publish deadline leaves a real margin inside the lease, and the table limits stay ordered', () => {
  assert.ok(PUBLISH_DEADLINE_MS <= LEASE_MS - 10000, '总时限必须比租约短至少 10 秒，否则慢请求写回时租约已被别人拿走');
  assert.ok(CASE_PRUNE_AT < 1000, '清理线必须低于案件表上限');
  assert.ok(CLIENT_DAILY_LIMIT < 100, '单个来源的份额必须小于全局额度');
});

test('retry hints are exact: only temporary refusals carry one', () => {
  assert.equal(retryAfter('receiver_busy'), 30);
  assert.equal(retryAfter('submission_in_progress'), 60);
  for (const code of ['daily_budget', 'daily_byte_budget', 'daily_client_budget']) {
    const wait = retryAfter(code);
    assert.ok(wait >= 1 && wait <= 86400, code);
  }
  for (const code of ['identity_conflict', 'remote_binding', 'lease_superseded', 'case_budget', 'inbox_asset_capacity', 'archive_invalid']) {
    assert.equal(retryAfter(code), null, code + ' 不该让客户端傻等');
  }
});

test('a live lease answers 409 with a Retry-After the client can obey', async () => {
  const packet = await archiveFor();
  const env = environment();
  env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'uploading', ?, 'someone-else', NULL)")
    .run(packet.envelope.idempotency_key, packet.envelope.archive_sha256, Date.now() + 60000);
  const refused = await worker.fetch(request(packet), env);
  assert.equal(refused.status, 409);
  assert.equal((await refused.json()).error, 'submission_in_progress');
  assert.equal(refused.headers.get('Retry-After'), '60');
  env.DB.native.close();
});

test('a digest mismatch on stored data is a 409 with no Retry-After, because waiting cannot fix it', async () => {
  const packet = await archiveFor();
  const env = environment();
  env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'received', 0, '', '{}')")
    .run(packet.envelope.idempotency_key, '0'.repeat(64));
  const refused = await worker.fetch(request(packet), env);
  assert.equal(refused.status, 409);
  assert.equal((await refused.json()).error, 'identity_conflict');
  assert.equal(refused.headers.get('Retry-After'), null);
  env.DB.native.close();
});

test('the old-rows sweep runs once a day, not on every request that happens to see a count of one', async () => {
  const env = environment();
  env.DB.native.prepare('INSERT INTO budget VALUES (?, 5)').run('2000-01-01');
  assert.equal((await worker.fetch(garbage(), env)).status, 422);
  assert.equal(budgetRow(env, '2000-01-01'), 0, '第一笔请求清掉了旧行');
  env.DB.native.prepare('INSERT INTO budget VALUES (?, 5)').run('2000-01-02');
  assert.equal((await worker.fetch(garbage(), env)).status, 422);
  assert.equal(budgetRow(env, '2000-01-02'), 5, '垃圾请求把额度退回去之后，计数回到 1 也不能再触发一次清理');
  env.DB.native.close();
});

test('refunds are counted and capped per day, so spreading garbage over many sources cannot make unlimited writes', async () => {
  const env = environment();
  for (let n = 0; n < 3; n++) assert.equal((await worker.fetch(garbage(), env)).status, 422);
  assert.equal(budgetRow(env, today() + ':refunds'), 3);
  assert.equal(budgetRow(env, today()), 0);
  env.DB.native.prepare('UPDATE budget SET requests=? WHERE day=?').run(REFUND_DAILY_LIMIT, today() + ':refunds');
  assert.equal((await worker.fetch(garbage(), env)).status, 422);
  assert.equal(budgetRow(env, today()), 1, '退款用完之后回到保守行为：垃圾请求照样占全局额度');
  assert.equal(budgetRow(env, today() + ':refunds'), REFUND_DAILY_LIMIT);
  env.DB.native.close();
});

// 让 GitHub 的令牌接口走正常的假实现，其余调用交给 onApi：用来模拟“令牌拿到之后上游出事”。
const afterToken = (remote, onApi) => ({ fetch: async (input, options = {}) =>
  new URL(input).pathname.startsWith('/app/installations/') ? remote.fetch(input, options) : onApi(input, options) });

test('failing before any GitHub contact (bad app configuration) gives the budget back', async () => {
  const packet = await archiveFor();
  const env = { ...environment(), GITHUB_PRIVATE_KEY_PKCS8: 'not a pem' };
  const refused = await worker.fetch(request(packet), env);
  assert.equal(refused.status, 503);
  assert.deepEqual(await refused.json(), { error: 'github_app_not_configured', phase: 'github_auth' });
  assert.equal(budgetRow(env, today()), 0);
  assert.equal(budgetRow(env, today() + ':bytes'), 0);
  env.DB.native.close();
});

test('a token endpoint that answers 500 never stored anything, so the budget comes back too', async () => {
  const packet = await archiveFor();
  const env = environment();
  await withGitHub({ fetch: async () => new Response('{}', { status: 500 }) }, async () => {
    const refused = await worker.fetch(request(packet), env);
    assert.equal(refused.status, 503);
    assert.equal((await refused.json()).error, 'github_app_auth_failed');
  });
  assert.equal(budgetRow(env, today()), 0);
  env.DB.native.close();
});

test('replaying a received case that fails midway keeps it received with its receipt', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  const states = [];
  await withGitHub(remote, async () => assert.equal((await worker.fetch(request(packet), env)).status, 200));
  const row = () => env.DB.native.prepare('SELECT state, receipt, lease_until FROM cases').get();
  await withGitHub(afterToken(remote, async () => { states.push(row().state); throw new Error('upstream went away'); }), async () => {
    assert.equal((await worker.fetch(request(packet), env)).status, 503);
  });
  assert.deepEqual(states, ['received'], '重放进行中也不能把已收件的行改成别的状态');
  const after = row();
  assert.equal(after.state, 'received');
  assert.ok(after.receipt && after.receipt.includes('archive_sha256'));
  assert.equal(after.lease_until, 0);
  env.DB.native.close();
});

const clientRow = async (env, ip) => budgetRow(env, today() + ':client:' + (await digest(encoder.encode(today() + '|' + ip))).slice(0, 24));

test('a server-side failure gives the source its own share back, a refused package does not', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  await withGitHub(afterToken(remote, async () => { throw new Error('upstream went away'); }), async () => {
    assert.equal((await worker.fetch(requestFrom(packet, '198.51.100.8'), env)).status, 503);
  });
  assert.equal(await clientRow(env, '198.51.100.8'), 0, 'GitHub 出故障时用户的重试不该烧掉他当天的份额');
  assert.equal((await worker.fetch(garbage(), env)).status, 422);
  assert.equal(await clientRow(env, '203.0.113.9'), 1, '内容被拒是来源自己的事，份额照扣');
  env.DB.native.close();
});

test('only a timeout during the GitHub phase is reported as an upstream timeout', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  await withGitHub(afterToken(remote, async () => { throw new DOMException('timed out', 'TimeoutError'); }), async () => {
    const refused = await worker.fetch(request(packet), env);
    assert.equal(refused.status, 503);
    assert.deepEqual(await refused.json(), { error: 'upstream_timeout', phase: 'github_storage' });
  });
  // 客户端在读请求体时中断：同样是 AbortError，但上游根本没被碰到，不能怪上游。
  const aborted = requestFrom(await archiveFor(), '198.51.100.9');
  Object.defineProperty(aborted, 'body', { value: { getReader: () => ({
    read: async () => { throw new DOMException('client went away', 'AbortError'); }, releaseLock() {}, cancel: async () => {} }) } });
  const refused = await worker.fetch(aborted, env);
  assert.equal(refused.status, 503);
  assert.deepEqual(await refused.json(), { error: 'submission_unconfirmed', phase: 'body' });
  assert.equal(await clientRow(env, '198.51.100.9'), 1, '读请求体阶段的失败不替来源退份额');
  env.DB.native.close();
});

test('pruning never touches the requester’s own row and starts exactly at the documented line', async () => {
  const packet = await archiveFor();
  packet.envelope.idempotency_key = 'a'.repeat(32);
  const fill = (env, count, skip) => {
    for (let n = 0; n < count; n++) {
      const name = n.toString(16).padStart(32, '0');
      if (name !== skip) env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'unknown', 0, '', NULL)").run(name, '0'.repeat(64));
    }
  };
  // 请求者自己的行（校验和对不上）也在过期未完成的行里：被清掉就等于悄悄绕过了 identity_conflict。
  const own = createdEnv => createdEnv.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'unknown', 0, '', NULL)")
    .run(packet.envelope.idempotency_key, '1'.repeat(64));
  const first = environment();
  fill(first, 899); own(first);
  const conflict = await worker.fetch(request(packet), first);
  assert.equal(conflict.status, 409);
  assert.equal((await conflict.json()).error, 'identity_conflict');
  assert.equal(first.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 1, '到线了：别人的过期行被清，自己的行还在');
  first.DB.native.close();
  const below = environment();
  fill(below, 898); own(below);
  assert.equal((await worker.fetch(request(packet), below)).status, 409);
  assert.equal(below.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 899, '差一行不到线，不清理');
  below.DB.native.close();
});

test('the publish deadline really reaches every GitHub call made for a request', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  const realNow = Date.now;
  let skewed = false;
  // 令牌拿到之后，时钟一下子越过总时限：后续每一次 GitHub 调用都必须在发出请求之前被拦下。
  const clock = { fetch: async (input, options = {}) => {
    const response = await remote.fetch(input, options);
    if (new URL(input).pathname.startsWith('/app/installations/')) skewed = true;
    return response;
  } };
  Date.now = () => realNow() + (skewed ? PUBLISH_DEADLINE_MS + 1000 : 0);
  try {
    await withGitHub(clock, async () => {
      const refused = await worker.fetch(request(packet), env);
      assert.equal(refused.status, 503);
      assert.deepEqual(await refused.json(), { error: 'upstream_timeout', phase: 'github_storage' });
    });
  } finally { Date.now = realNow; }
  assert.equal(remote.assets.size, 0, '总时限过后不能再往 GitHub 写东西');
  env.DB.native.close();
});
