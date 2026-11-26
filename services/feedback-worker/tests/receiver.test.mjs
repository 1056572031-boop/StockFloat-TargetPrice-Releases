import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { generateKeyPairSync, randomBytes, verify } from 'node:crypto';
import { ZipReader, ZipWriter, Uint8ArrayWriter, Uint8ArrayReader } from '@zip.js/zip.js';
import worker, { CASE_PRUNE_AT, CLIENT_DAILY_LIMIT, createRetentionIntent, createTimer, FULL_POLICY, FULL_V3_POLICY, GitHubStore, INBOX_TAG, LEASE_MS, POLICY, PUBLISH_DEADLINE_MS, REFUND_DAILY_LIMIT, REPOSITORY, digest, pruneExpiredCases, reconcileRetentionIntents, retryAfter, strictJson, unpackRequest, validateArchive, validateState } from '../src/index.mjs';
import { appJwt, manifest } from '../setup-github-app.mjs';

const encoder = new TextEncoder();
const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const pem = privateKey.export({ type: 'pkcs8', format: 'pem' });
let installationSequence = 10000;
const key = () => randomBytes(16).toString('hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// 整个文件的联网总闸：没有用 withGitHub 换上假 GitHub 的地方，任何对外请求都直接失败并记下来，
// 不能悄悄打到真实的 api.github.com（换令牌现在可能在后台发出，漏掉一处就会真的联网）。
const strayRequests = [];
globalThis.fetch = async input => {
  strayRequests.push(String(input instanceof Request ? input.url : input));
  throw new Error('a test tried to reach the real network');
};
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

function database(schema = readFileSync(new URL('../schema.sql', import.meta.url), 'utf8')) {
  const db = new DatabaseSync(':memory:');
  db.exec(schema);
  return { native: db, prepare(sql) { return { bind(...params) { return {
    async first() { return db.prepare(sql).get(...params) ?? null; },
    async all() { return { results: db.prepare(sql).all(...params) }; },
    async run() { const result = db.prepare(sql).run(...params); return { success: true, meta: { changes: result.changes } }; },
  }; } }; }, async batch(statements) {
    // D1 enables foreign keys by default; run the local test with the same constraint.
    db.exec('PRAGMA foreign_keys = ON');
    db.exec('BEGIN');
    try { const results = []; for (const statement of statements) results.push(await statement.run()); db.exec('COMMIT'); return results; }
    catch (error) { db.exec('ROLLBACK'); throw error; }
  } };
}

class FakeGitHub {
  assets = new Map();
  uploadCount = 0;
  private = true;
  fullName = REPOSITORY;
  releaseTag = INBOX_TAG;
  releaseIdentity = 1;
  loseUpload = false;
  redirect = false;
  mintCount = 0;
  tokenLifetimeMs = 3600000;
  mintDelayMs = 0;
  authFailures = 0;
  authFailureStatus = 401;
  authFailureAfterUploads = null;
  deleteResponseLost = false;
  deleteNoop = false;
  loseBeforeDelete = false;
  deleteCalls = [];
  assetReadFailure = null;
  beforeDelete = null;
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
      if (this.mintDelayMs) await sleep(this.mintDelayMs);
      const jwt = options.headers.Authorization.slice(7);
      const [head, payload, signature] = jwt.split('.');
      assert.ok(verify('RSA-SHA256', Buffer.from(head + '.' + payload), publicKey, Buffer.from(signature, 'base64url')));
      assert.deepEqual(JSON.parse(options.body), { repositories: ['Skyrocket-Private-Feedback'], permissions: { contents: 'write' } });
      return json({ token: 'test-installation-token', expires_at: new Date(Date.now() + this.tokenLifetimeMs).toISOString(),
        permissions: { contents: 'write', metadata: 'read' }, repository_selection: 'selected',
        repositories: [{ full_name: REPOSITORY, private: true,
          ...Object.fromEntries(Array.from({ length: 70 }, (_, n) => ['metadata_' + n, null])) }] }, 201);
    }
    assert.equal(options.headers.Authorization, 'Bearer test-installation-token');
    if (this.authFailureAfterUploads !== null && this.uploadCount >= this.authFailureAfterUploads) {
      this.authFailureAfterUploads = null;
      return json({ error: 'expired token after write' }, this.authFailureStatus);
    }
    if (this.authFailures > 0) {
      this.authFailures--;
      return json({ error: 'expired token' }, this.authFailureStatus);
    }
    if (url.pathname === '/repos/' + REPOSITORY) return json({ private: this.private, full_name: this.fullName,
      ...Object.fromEntries(Array.from({ length: 70 }, (_, n) => ['metadata_' + n, null])) });
    if (url.pathname.endsWith('/releases/1')) return json({ id: this.releaseIdentity, tag_name: this.releaseTag, draft: true });
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
      const id = Number(match[1]);
      if (options.method === 'DELETE') {
        this.deleteCalls.push(id);
        if (this.beforeDelete) await this.beforeDelete(id);
        if (this.loseBeforeDelete) throw new Error('lost delete response before object removed');
        if (this.deleteNoop) return new Response(null, { status: 204 });
        if (!this.assets.delete(id)) return json({ error: 'missing' }, 404);
        if (this.deleteResponseLost) { this.deleteResponseLost = false; throw new Error('lost delete response after object removed'); }
        return new Response(null, { status: 204 });
      }
      if (this.assetReadFailure) {
        const failure = this.assetReadFailure;
        this.assetReadFailure = null;
        return failure();
      }
      if (this.redirect) return new Response(null, { status: 302, headers: { Location: 'https://release-assets.githubusercontent.com/' + match[1] } });
      const asset = this.assets.get(id);
      return asset ? (options.headers?.Accept === 'application/vnd.github+json'
        ? json({ id: asset.id, name: asset.name, size: asset.size })
        : new Response(asset.bytes)) : json({ error: 'missing' }, 404);
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
        await sleep(60); // 换令牌若被提前发出，要等它真的走到假 GitHub 才看得见
      });
      assert.equal(remote.mintCount, 0);
      assert.equal(remote.assets.size, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 0);
    } finally { env.DB.native.close(); }
  }
});

function environment(extra = {}) {
  return { ENABLED: 'true', DB: database(), GITHUB_APP_ID: '1', GITHUB_INSTALLATION_ID: String(++installationSequence),
    GITHUB_PRIVATE_KEY_PKCS8: pem, GITHUB_RELEASE_ID: '1', ...extra };
}
function deleteRequest(key, token) {
  return new Request('https://receiver.example/v1/admin/feedback/' + key, {
    method: 'DELETE', headers: token ? { Authorization: 'Bearer ' + token } : {},
  });
}
function request(packet, path = '/v1/feedback', capability = null) {
  const body = wire(packet);
  const extraHeaders = typeof capability === 'string' ? { 'X-Skyrocket-Retention': capability } : (capability || {});
  return new Request('https://receiver.example' + path, { method: 'POST', body,
    headers: { 'Content-Type': 'application/vnd.skyrocket.feedback-v1', 'Content-Length': String(body.length), ...extraHeaders } });
}
async function withGitHub(remote, action) {
  const original = globalThis.fetch;
  globalThis.fetch = remote.fetch;
  try { return await action(); } finally { globalThis.fetch = original; }
}

// Regression: A stale scan must not overwrite newer cursor progress.
test('concurrent reconciliation cannot overwrite newer cursor progress', async () => {
  for (const wrapped of [false, true]) {
    const env = environment();
    try {
      const now = Date.now();
      const packet = await archiveFor();
      for (let i = 0; i < 51; i++) {
        const envelope = { ...packet.envelope, idempotency_key: i.toString(16).padStart(32, '0') };
        const intent = await createRetentionIntent(env, envelope, true, now - PUBLISH_DEADLINE_MS - 100 + i);
        env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'unknown', ?, 'busy', NULL)")
          .run(intent.idempotency_key, intent.archive_sha256, now + LEASE_MS);
      }
      if (wrapped) env.DB.native.prepare('INSERT INTO retention_queue_cursor(name,created_at,idempotency_key) VALUES (?, ?, ?)')
        .run('reconcile', now - PUBLISH_DEADLINE_MS - 100 + 49, (49).toString(16).padStart(32, '0'));
      let release, arrived;
      const gate = new Promise(resolve => { release = resolve; });
      const waiting = new Promise(resolve => { arrived = resolve; });
      const prepare = env.DB.prepare;
      let paused = false;
      env.DB.prepare = sql => {
        const statement = prepare(sql);
        if (!sql.includes('INSERT INTO retention_queue_cursor')) return statement;
        return { bind(...args) {
          const bound = statement.bind(...args);
          return { ...bound, async run() {
            if (!paused) { paused = true; arrived(); await gate; }
            return bound.run();
          } };
        } };
      };
      const store = new GitHubStore('test-installation-token', '1', new FakeGitHub().fetch);
      const older = reconcileRetentionIntents(env, store, now);
      await waiting;
      await reconcileRetentionIntents(env, store, now);
      await reconcileRetentionIntents(env, store, now);
      const expected = env.DB.native.prepare('SELECT * FROM retention_queue_cursor WHERE name=?').get('reconcile');
      release();
      await older;
      assert.deepEqual(env.DB.native.prepare('SELECT * FROM retention_queue_cursor WHERE name=?').get('reconcile'), expected, `wrapped=${wrapped}`);
    } finally { env.DB.native.close(); }
  }
});

// Regression: cursor coordinates can return to their old value while its version advances (ABA).
test('concurrent reconciliation rejects stale cursor write after ABA', async () => {
  const env = environment();
  try {
    const now = Date.now();
    const packet = await archiveFor();
    const created = now - PUBLISH_DEADLINE_MS - 100;
    const key = i => i.toString(16).padStart(32, '0');
    for (let i = 0; i < 51; i++) {
      const intent = await createRetentionIntent(env, { ...packet.envelope, idempotency_key: key(i) }, true, created + i);
      env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'unknown', ?, 'busy', NULL)")
        .run(intent.idempotency_key, intent.archive_sha256, now + LEASE_MS);
    }
    env.DB.native.prepare('INSERT INTO retention_queue_cursor(name,created_at,idempotency_key,version) VALUES (?, ?, ?, ?)')
      .run('reconcile', created + 24, key(24), 1);
    const cursor = () => ({ ...env.DB.native.prepare('SELECT created_at,idempotency_key,version FROM retention_queue_cursor WHERE name=?').get('reconcile') });
    const initial = cursor();
    let release, arrived;
    const gate = new Promise(resolve => { release = resolve; });
    const waiting = new Promise(resolve => { arrived = resolve; });
    const prepare = env.DB.prepare;
    let paused = false;
    env.DB.prepare = sql => {
      const statement = prepare(sql);
      if (!sql.includes('INSERT INTO retention_queue_cursor')) return statement;
      return { bind(...args) {
        const bound = statement.bind(...args);
        return { ...bound, async run() {
          if (!paused) { paused = true; arrived(); await gate; }
          return bound.run();
        } };
      } };
    };
    const store = new GitHubStore('test-installation-token', '1', new FakeGitHub().fetch);
    const older = reconcileRetentionIntents(env, store, now);
    await waiting;
    try {
      await reconcileRetentionIntents(env, store, now);
      assert.equal(cursor().idempotency_key, key(49));
      await reconcileRetentionIntents(env, store, now);
      assert.equal(cursor().idempotency_key, key(50));
      await reconcileRetentionIntents(env, store, now);
      const returned = cursor();
      assert.equal(returned.created_at, initial.created_at);
      assert.equal(returned.idempotency_key, initial.idempotency_key);
      assert.equal(returned.version, initial.version + 3);
      release();
      await older;
      assert.deepEqual(cursor(), returned, 'stale write must not replace the returned cursor');
    } finally {
      release();
      await older;
    }
  } finally { env.DB.native.close(); }
});

// Regression: Restarting every scan at the first page starves an eligible case behind 25 busy leases.
test('reconciliation rotates past busy queue heads', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    const now = Date.now();
    const store = new GitHubStore('test-installation-token', '1', remote.fetch);
    for (let i = 0; i < 26; i++) {
      const envelope = { ...packet.envelope, idempotency_key: i.toString(16).padStart(32, '0') };
      const intent = await createRetentionIntent(env, envelope, true, now - PUBLISH_DEADLINE_MS - 100 + i);
      env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'unknown', ?, ?, NULL)")
        .run(intent.idempotency_key, intent.archive_sha256, i < 25 ? now + LEASE_MS : 0, i < 25 ? 'other' : '');
      if (i === 25) {
        const published = await store.publish(envelope, packet.archive, true);
        env.DB.native.prepare('UPDATE retention_intents SET zip_asset_id=?, receipt_asset_id=?, retention_until=?, state=\'pending\' WHERE idempotency_key=?')
          .run(published.asset_id, published._receipt_asset_id, Date.parse(published.retention_until), intent.idempotency_key);
      }
    }
    await reconcileRetentionIntents(env, store, now);
    await reconcileRetentionIntents(env, store, now);
    assert.equal(env.DB.native.prepare("SELECT state FROM retention_intents WHERE idempotency_key=?").get((25).toString(16).padStart(32, '0')).state, 'bound');
    assert.equal(env.DB.native.prepare("SELECT lease_token FROM cases WHERE idempotency_key=?").get('0'.repeat(32)).lease_token, 'other');
  } finally { env.DB.native.close(); }
});

// Regression: Restarting every scan at the first page starves a bindable case behind 25 retryable entries.
test('reconciliation rotates past retryable queue heads', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    const now = Date.now();
    const store = new GitHubStore('test-installation-token', '1', remote.fetch);
    for (let i = 0; i < 26; i++) {
      const envelope = { ...packet.envelope, idempotency_key: i.toString(16).padStart(32, '0') };
      const intent = await createRetentionIntent(env, envelope, true, now - PUBLISH_DEADLINE_MS - 100 + i);
      env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'unknown', 0, '', NULL)").run(intent.idempotency_key, intent.archive_sha256);
      if (i === 25) await store.publish(envelope, packet.archive, true);
    }
    assert.equal((await reconcileRetentionIntents(env, store, now)).retryable, 25);
    assert.equal((await reconcileRetentionIntents(env, store, now)).bound, 1);
  } finally { env.DB.native.close(); }
});

// Regression: Dropping orphan retries, inventing a retention receipt, or allowing replay after cleanup breaks this contract.
test('orphan cleanup retries verified ZIP removal without fabricating a receipt', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    const now = Date.now();
    const intent = await createRetentionIntent(env, packet.envelope, true, now - PUBLISH_DEADLINE_MS);
    remote.assets.set(1, { id: 1, name: intent.zip_name, size: packet.archive.length, bytes: packet.archive });
    const store = new GitHubStore('test-installation-token', '1', remote.fetch);
    assert.equal((await reconcileRetentionIntents(env, store, now)).orphanCleanup, 1);
    remote.loseBeforeDelete = true;
    await reconcileRetentionIntents(env, store, Date.now());
    assert.equal(remote.assets.size, 1);
    remote.loseBeforeDelete = false;
    await reconcileRetentionIntents(env, store, Date.now());
    assert.equal(remote.assets.size, 0, 'subsequent scan must consume the verified orphan');
    assert.ok(remote.deleteCalls.includes(1));
    assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
    assert.equal(env.DB.native.prepare('SELECT state FROM retention_intents').get().state, 'manual_review');
    await withGitHub(remote, async () => assert.equal((await worker.fetch(request(packet), env)).status, 409));
  } finally { env.DB.native.close(); }
});

// Regression: Removing ZIP hash, duplicate/receipt identity, or private repository checks can delete an unverified asset or allow replay.
test('orphan cleanup fails closed when ZIP identity or bytes change and blocks replay', async () => {
  for (const fault of ['hash', 'duplicate', 'receipt', 'repository']) {
    const packet = await archiveFor();
    const env = environment();
    const remote = new FakeGitHub();
    try {
      const now = Date.now();
      const intent = await createRetentionIntent(env, packet.envelope, true, now - PUBLISH_DEADLINE_MS);
      remote.assets.set(1, { id: 1, name: intent.zip_name, size: packet.archive.length, bytes: packet.archive });
      const store = new GitHubStore('test-installation-token', '1', remote.fetch);
      assert.equal((await reconcileRetentionIntents(env, store, now)).orphanCleanup, 1);
      if (fault === 'hash') remote.assets.get(1).bytes = new Uint8Array(packet.archive.length);
      if (fault === 'duplicate') remote.assets.set(2, { ...remote.assets.get(1), id: 2 });
      if (fault === 'receipt') remote.assets.set(2, { id: 2, name: intent.receipt_name, size: 2, bytes: encoder.encode('{}') });
      if (fault === 'repository') remote.private = false;
      await reconcileRetentionIntents(env, store, Date.now());
      assert.equal(remote.deleteCalls.length, 0, fault);
      const row = env.DB.native.prepare('SELECT state,last_error FROM retention_intents').get();
      assert.equal(row.state, 'orphan_cleanup', fault);
      assert.ok(row.last_error, fault);
      await withGitHub(remote, async () => {
        assert.equal((await worker.fetch(request(packet), env)).status, 409, fault);
      });
      assert.equal(remote.uploadCount, 0, fault);
    } finally { env.DB.native.close(); }
  }
});

// A completed prune cannot exclude a late POST; the tombstone must require manual review.
// This does not assert that an automatic scan discovers or removes the late receipt.
test('late receipt POST after completed cleanup requires manual review', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  let late;
  try {
    await withGitHub(remote, async () => {
      assert.equal((await worker.fetch(request(packet, '/v1/feedback', { 'X-Skyrocket-Retention': 'auto-delete-v1' }), env)).status, 200);
      remote.assets.delete(2);
      const fetch = remote.fetch;
      remote.fetch = async (input, options = {}) => {
        const url = new URL(input);
        if (options.method === 'POST' && url.searchParams.get('name')?.endsWith('.receipt.json')) {
          late = { id: 2, name: url.searchParams.get('name'), bytes: new Uint8Array(options.body) };
          late.size = late.bytes.length;
          throw new DOMException('timed out', 'TimeoutError');
        }
        return fetch(input, options);
      };
      globalThis.fetch = remote.fetch;
      assert.equal((await worker.fetch(request(packet), env)).status, 503);
      assert.ok(late);
      const key = packet.envelope.idempotency_key;
      const result = await pruneExpiredCases(env, new GitHubStore('test-installation-token', '1', remote.fetch), Date.now() + 31 * 86400000);
      assert.deepEqual(result, { deletedCaseIds: [key], failed: 0, busy: 0, backlog: 0 });
      assert.ok(env.DB.native.prepare('SELECT deleted_at FROM deleted_cases WHERE idempotency_key=?').get(key)?.deleted_at);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention WHERE idempotency_key=?').get(key).n, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases WHERE idempotency_key=?').get(key).n, 0);
      assert.equal(remote.assets.size, 0);
      assert.ok(remote.deleteCalls.includes(1));
      const intent = env.DB.native.prepare('SELECT state,last_error FROM retention_intents WHERE idempotency_key=?').get(key);
      assert.deepEqual({ ...intent }, { state: 'manual_review', last_error: 'cleanup_late_post_unproven' });
      remote.assets.set(late.id, late);
      assert.equal(remote.assets.size, 1);
      assert.equal(remote.assets.get(late.id), late);
      const secret = 'm'.repeat(32);
      const deletion = await worker.fetch(deleteRequest(key, secret), { ...env, FEEDBACK_ADMIN_DELETE_TOKEN: secret });
      assert.equal(deletion.status, 503);
      assert.equal((await deletion.json()).error, 'retention_manual_review_required');
      assert.equal(remote.assets.get(late.id), late);
    });
  } finally { env.DB.native.close(); }
});

// Regression: Treating metadata authorization errors or timeouts as 404 can falsely finalize deletion.
test('GET 403 or timeout is not treated as a confirmed missing asset', async () => {
  for (const failure of [
    () => new Response(JSON.stringify({ error: 'forbidden' }), { status: 403 }),
    () => { throw new TypeError('upstream timeout'); },
  ]) {
    const packet = await archiveFor();
    const env = environment();
    const remote = new FakeGitHub();
    remote.deleteNoop = true;
    try {
      await withGitHub(remote, async () => {
        assert.equal((await worker.fetch(request(packet, '/v1/feedback', { 'X-Skyrocket-Retention': 'auto-delete-v1' }), env)).status, 200);
        const store = new GitHubStore('test-installation-token', '1', remote.fetch);
        // Independently verify the 404 classification: later list readback is a
        // second defense and would otherwise mask a misclassified GET failure.
        remote.assetReadFailure = failure;
        await assert.rejects(() => store.readAssetMetadata(1));
        // Fail the *post-delete* GET, not the identity preflight. The first read
        // must succeed so this probes confirmation rather than an earlier guard.
        remote.beforeDelete = () => { remote.assetReadFailure = failure; };
        const result = await pruneExpiredCases(env, store, Date.now() + 31 * 86400000);
        assert.deepEqual(result.deletedCaseIds, []);
        assert.equal(remote.deleteCalls.length, 1);
        assert.equal(remote.assets.size, 2);
        const row = env.DB.native.prepare('SELECT zip_deleted, receipt_deleted FROM retention').get();
        assert.equal(row.zip_deleted, 0);
        assert.equal(row.receipt_deleted, 0);
        assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 0);
      });
    } finally { env.DB.native.close(); }
  }
});

// Regression: Marking a lost DELETE response as success without readback falsely deletes retained state.
test('delete response loss while asset remains does not mark the asset deleted', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  remote.loseBeforeDelete = true;
  try {
    await withGitHub(remote, async () => {
      assert.equal((await worker.fetch(request(packet, '/v1/feedback', { 'X-Skyrocket-Retention': 'auto-delete-v1' }), env)).status, 200);
      const result = await pruneExpiredCases(env, new GitHubStore('test-installation-token', '1', remote.fetch), Date.now() + 31 * 86400000);
      assert.deepEqual(result.deletedCaseIds, []);
      const row = env.DB.native.prepare('SELECT zip_deleted, receipt_deleted, cleanup_error FROM retention').get();
      assert.equal(row.zip_deleted, 0);
      assert.equal(row.receipt_deleted, 0);
      assert.equal(row.cleanup_error, 'asset_delete_unconfirmed');
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 0);
    });
  } finally { env.DB.native.close(); }
});

// Regression: treating a genuine pre-intent automatic receipt as safe to delete can lose the late-POST fence.
test('historical pre-intent schema and receipt require review after migration', async () => {
  // Verbatim schema and receipt shape from mainline 26c98d5216e7146ae56c5dd2fa46b0a4d0455e60:
  // its publish path stored receiptForClient (without _receipt_asset_id) and inserted the four retention fields.
  const historicalSchema = `CREATE TABLE IF NOT EXISTS budget (
  day TEXT PRIMARY KEY,
  requests INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cases (
  idempotency_key TEXT PRIMARY KEY,
  archive_sha256 TEXT NOT NULL,
  state TEXT NOT NULL,
  lease_until INTEGER NOT NULL,
  lease_token TEXT NOT NULL,
  receipt TEXT
);
CREATE TABLE IF NOT EXISTS retention (
  idempotency_key TEXT PRIMARY KEY,
  retention_until INTEGER NOT NULL,
  zip_asset_id INTEGER NOT NULL,
  receipt_asset_id INTEGER NOT NULL,
  zip_deleted INTEGER NOT NULL DEFAULT 0,
  receipt_deleted INTEGER NOT NULL DEFAULT 0,
  cleanup_error TEXT,
  cleanup_lease_until INTEGER NOT NULL DEFAULT 0,
  cleanup_lease_token TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (idempotency_key) REFERENCES cases(idempotency_key)
);
CREATE TABLE IF NOT EXISTS deleted_cases (
  idempotency_key TEXT PRIMARY KEY,
  deleted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS retention_due ON retention(retention_until);`;
  const packet = await archiveFor();
  const env = environment();
  env.DB.native.close();
  env.DB = database(historicalSchema);
  const remote = new FakeGitHub();
  const k = packet.envelope.idempotency_key;
  const receipt = { case_id: `feedback-${k}`, archive_sha256: packet.envelope.archive_sha256, size_bytes: packet.archive.length,
    received_at: '2026-01-01T00:00:00.000Z', storage_status: 'stored', validation_status: 'validated',
    retention_until: '2026-01-31T00:00:00.000Z', retention_policy: 'auto_delete', repository: REPOSITORY,
    asset_id: 1, consent_policy_version: POLICY };
  const receiptBytes = encoder.encode(JSON.stringify(receipt));
  try {
    env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'received', 0, ?, ?)")
      .run(k, packet.envelope.archive_sha256, 'historical-lease', JSON.stringify(receipt));
    env.DB.native.prepare('INSERT INTO retention(idempotency_key, retention_until, zip_asset_id, receipt_asset_id) VALUES (?, ?, ?, ?)')
      .run(k, Date.parse(receipt.retention_until), 1, 2);
    remote.assets.set(1, { id: 1, name: `${receipt.case_id}.zip`, size: packet.archive.length, bytes: packet.archive });
    remote.assets.set(2, { id: 2, name: `${receipt.case_id}.receipt.json`, size: receiptBytes.length, bytes: receiptBytes });
    assert.equal(env.DB.native.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='retention_intents'").get().n, 0);
    for (const migration of ['0003_retention_intents.sql', '0004_retention_queue_cursor.sql', '0005_retention_cursor_version.sql']) {
      env.DB.native.exec(readFileSync(new URL(`../migrations/${migration}`, import.meta.url), 'utf8'));
    }
    const store = new GitHubStore('test-installation-token', '1', remote.fetch);
    const result = await pruneExpiredCases(env, store, Date.parse('2026-02-01T00:00:00.000Z'));
    assert.deepEqual(result.deletedCaseIds, []);
    assert.equal(result.failed, 1);
    assert.equal(remote.deleteCalls.length, 0);
    assert.equal(remote.assets.size, 2);
    assert.equal(env.DB.native.prepare('SELECT receipt FROM cases WHERE idempotency_key=?').get(k).receipt, JSON.stringify(receipt));
    assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 0);
    const intent = env.DB.native.prepare('SELECT state, last_error FROM retention_intents WHERE idempotency_key=?').get(k);
    assert.deepEqual({ ...intent }, { state: 'manual_review', last_error: 'legacy_retention_review' });
    await withGitHub(remote, async () => assert.equal((await worker.fetch(request(packet), env)).status, 409));
    assert.equal(remote.uploadCount, 0);
  } finally { env.DB.native.close(); }
});

test('legacy clients receive manual deletion and negotiated clients receive automatic retention', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  const legacy = await archiveFor();
  const modern = await archiveFor();
  try {
    await withGitHub(remote, async () => {
      const firstResponse = await worker.fetch(request(legacy), env);
      assert.equal(firstResponse.status, 200);
      const first = await firstResponse.json();
      assert.equal(first.retention_policy, 'manual_deletion');
      assert.equal(first.retention_until, null);

      const legacyRetry = await worker.fetch(request(legacy, '/v1/feedback', 'auto-delete-v1'), env);
      assert.deepEqual(await legacyRetry.json(), first);

      const automaticResponse = await worker.fetch(request(modern, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(automaticResponse.status, 200);
      const automatic = await automaticResponse.json();
      assert.equal(automatic.retention_policy, 'auto_delete');
      assert.equal(Date.parse(automatic.retention_until) - Date.parse(automatic.received_at), 30 * 86400000);
      const reverseRetry = await worker.fetch(request(modern), env);
      assert.equal(reverseRetry.status, 200);
      assert.deepEqual(await reverseRetry.json(), automatic);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 1);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention WHERE idempotency_key=?').get(legacy.envelope.idempotency_key).n, 0);
    });
  } finally { env.DB.native.close(); }
});

test('a pre-existing manual receipt stays manual when retry now advertises capability', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  const store = new GitHubStore('test-installation-token', '1', remote.fetch);
  const original = await store.publish(packet.envelope, packet.archive);
  const receipt = { ...original };
  delete receipt._receipt_asset_id;
  try {
    await withGitHub(remote, async () => {
      const response = await worker.fetch(request(packet, '/v1/feedback', { 'X-Skyrocket-Retention': 'auto-delete-v1' }), env);
      assert.equal(response.status, 200, await response.clone().text());
      assert.deepEqual(await response.json(), receipt);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
    });
  } finally { env.DB.native.close(); }
});

test('legacy cleanup fences intent before any remote delete even when the response is lost', async () => {
  for (const admin of [false, true]) {
    const packet = await archiveFor();
    const env = environment({ FEEDBACK_ADMIN_DELETE_TOKEN: 'a'.repeat(32) });
    const remote = new FakeGitHub();
    try {
      await withGitHub(remote, async () => {
        const accepted = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
        const receipt = await accepted.json();
        env.DB.native.prepare('DELETE FROM retention_intents WHERE idempotency_key=?').run(packet.envelope.idempotency_key);
        remote.deleteResponseLost = true;
        remote.beforeDelete = () => {
          const intent = env.DB.native.prepare('SELECT state FROM retention_intents WHERE idempotency_key=?').get(packet.envelope.idempotency_key);
          assert.ok(intent && ['manual_review', 'cleanup_watch'].includes(intent.state), 'durable fence must precede DELETE');
        };
        const store = new GitHubStore('test-installation-token', '1', remote.fetch);
        if (admin) await worker.fetch(deleteRequest(packet.envelope.idempotency_key, 'a'.repeat(32)), env);
        else await pruneExpiredCases(env, store, Date.parse(receipt.retention_until) + 1);
        const intent = env.DB.native.prepare('SELECT state FROM retention_intents WHERE idempotency_key=?').get(packet.envelope.idempotency_key);
        const retained = env.DB.native.prepare('SELECT cleanup_error FROM retention WHERE idempotency_key=?').get(packet.envelope.idempotency_key);
        assert.ok(intent && ['manual_review', 'cleanup_watch'].includes(intent.state) || remote.deleteCalls.length === 0 && retained?.cleanup_error, 'legacy must be fenced or visibly fail closed');
      });
    } finally { env.DB.native.close(); }
  }
});

test('legacy receipt conflict remains visible without rewriting the local receipt', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const response = await worker.fetch(request(packet), env);
      const original = await response.json();
      env.DB.native.prepare('DELETE FROM retention_intents').run();
      const before = env.DB.native.prepare('SELECT receipt FROM cases').get().receipt;
      const asset = [...remote.assets.values()].find(item => item.name.endsWith('.receipt.json'));
      asset.bytes = encoder.encode(JSON.stringify({ ...original, retention_policy: 'auto_delete', retention_until: new Date(Date.parse(original.received_at) + 30 * 86400000).toISOString() }));
      const conflict = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(conflict.status, 409);
      assert.equal((await conflict.json()).error, 'retention_receipt_conflict');
      assert.equal(env.DB.native.prepare('SELECT receipt FROM cases').get().receipt, before);
      const intent = env.DB.native.prepare('SELECT state, last_error FROM retention_intents').get();
      assert.equal(intent.state, 'manual_review');
      assert.equal(intent.last_error, 'retention_receipt_conflict');
      const retry = await worker.fetch(request(packet), env);
      assert.notEqual(retry.status, 200);
      assert.equal(env.DB.native.prepare('SELECT last_error FROM retention_intents').get().last_error, 'retention_receipt_conflict');
    });
  } finally { env.DB.native.close(); }
});

test('legacy admin deletion preserves deletion flags and requires manual review on retry', async () => {
  const packet = await archiveFor();
  const env = environment({ FEEDBACK_ADMIN_DELETE_TOKEN: 'a'.repeat(32) });
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const accepted = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(accepted.status, 200);
      env.DB.native.prepare('DELETE FROM retention_intents').run();
      env.DB.native.prepare('UPDATE retention SET zip_deleted=1').run();
      for (let i = 0; i < 2; i++) {
        const response = await worker.fetch(deleteRequest(packet.envelope.idempotency_key, 'a'.repeat(32)), env);
        assert.deepEqual(await response.json(), { error: 'retention_manual_review_required' });
      }
      assert.equal(remote.deleteCalls.length, 0);
      assert.equal(env.DB.native.prepare('SELECT zip_deleted FROM retention').get().zip_deleted, 1);
      assert.equal(env.DB.native.prepare('SELECT state FROM retention_intents').get().state, 'manual_review');
    });
  } finally { env.DB.native.close(); }
});

test('legacy received retention without an intent requires manual review', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const accepted = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(accepted.status, 200, await accepted.clone().text());
      const receipt = await accepted.json();
      env.DB.native.prepare('DELETE FROM retention_intents WHERE idempotency_key=?').run(packet.envelope.idempotency_key);
      const store = new GitHubStore('test-installation-token', '1', remote.fetch);
      const result = await pruneExpiredCases(env, store, Date.parse(receipt.retention_until) + 1);
      assert.deepEqual(result.deletedCaseIds, []);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 1);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 0);
      assert.equal(env.DB.native.prepare('SELECT state FROM retention_intents').get().state, 'manual_review');
    });
  } finally { env.DB.native.close(); }
});

test('a finalized retention row keeps its asset identity and cleanup flags during intent binding', async () => {
  const { bindRetentionIntent } = await import('../src/index.mjs');
  const packet = await archiveFor({ diagnostics: await fullDiagnostics() });
  const env = environment();
  const now = Date.now() - 1000;
  const lease = 'binding-lease';
  const caseId = 'feedback-' + packet.envelope.idempotency_key;
  const zipName = caseId + '.zip';
  const receiptName = caseId + '.receipt.json';
  const retentionUntil = new Date(now + 30 * 86400000).toISOString();
  const intent = { ...packet.envelope, state: 'pending', case_id: caseId, archive_size: packet.archive.length,
    zip_name: zipName, receipt_name: receiptName, policy: 'auto_delete', created_at: now,
    publish_deadline: now - 1, reconcile_lease_until: now + 120000, reconcile_lease_token: 'reconcile-lease' };
  const receipt = { case_id: caseId, archive_sha256: packet.envelope.archive_sha256, size_bytes: packet.archive.length,
    received_at: new Date(now).toISOString(), retention_until: retentionUntil, retention_policy: 'auto_delete', repository: REPOSITORY,
    storage_status: 'stored', validation_status: 'validated', asset_id: 101,
    consent_policy_version: FULL_POLICY, validation_scope: 'container_sha256_and_member_catalog', _receipt_asset_id: 202 };
  env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'received', 0, '', NULL)").run(packet.envelope.idempotency_key, packet.envelope.archive_sha256);
  env.DB.native.prepare("INSERT INTO retention VALUES (?, ?, ?, ?, 1, 1, NULL, 0, '')")
    .run(packet.envelope.idempotency_key, Date.parse(retentionUntil), 101, 202);
  env.DB.native.prepare(`INSERT INTO retention_intents
    (idempotency_key, case_id, archive_sha256, archive_size, consent_policy_version, diagnostic_payload_version, zip_name, receipt_name, policy, created_at, publish_deadline, state, reconcile_lease_until, reconcile_lease_token)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)`)
    .run(packet.envelope.idempotency_key, caseId, packet.envelope.archive_sha256, packet.archive.length, FULL_POLICY, 2,
      zipName, receiptName, 'auto_delete', now, now - 1, now + 120000, 'reconcile-lease');
  await bindRetentionIntent(env, intent, receipt, now);
  const retained = env.DB.native.prepare('SELECT retention_until, zip_asset_id, receipt_asset_id, zip_deleted, receipt_deleted FROM retention').get();
  assert.deepEqual({ ...retained }, { retention_until: Date.parse(retentionUntil), zip_asset_id: 101, receipt_asset_id: 202, zip_deleted: 1, receipt_deleted: 1 });
  env.DB.native.close();
});

test('the retention worker reconciles pending intent before pruning expired retention', async () => {
  const { default: retentionWorker } = await import('../src/retention-worker.mjs');
  const packet = await archiveFor({ diagnostics: await fullDiagnostics() });
  const env = environment();
  const remote = new FakeGitHub();
  const now = Date.now();
  const caseId = 'feedback-' + packet.envelope.idempotency_key;
  const zipName = caseId + '.zip';
  const receiptName = caseId + '.receipt.json';
  const receivedAt = new Date(now - 31 * 86400000).toISOString();
  const receipt = { case_id: caseId, archive_sha256: packet.envelope.archive_sha256, size_bytes: packet.archive.length,
    received_at: receivedAt, retention_until: new Date(now - 86400000).toISOString(), retention_policy: 'auto_delete',
    repository: REPOSITORY, storage_status: 'stored', validation_status: 'validated', asset_id: 1,
    consent_policy_version: FULL_POLICY, validation_scope: 'container_sha256_and_member_catalog' };
  remote.assets.set(1, { id: 1, name: zipName, size: packet.archive.length, bytes: packet.archive });
  remote.assets.set(2, { id: 2, name: receiptName, size: encoder.encode(JSON.stringify(receipt)).length, bytes: encoder.encode(JSON.stringify(receipt)) });
  const events = [];
  const originalFetch = remote.fetch;
  remote.fetch = async (input, options = {}) => {
    const url = new URL(input);
    events.push((options.method || 'GET') + ' ' + url.pathname);
    return originalFetch(input, options);
  };
  env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'new', 0, '', NULL)").run(packet.envelope.idempotency_key, packet.envelope.archive_sha256);
  env.DB.native.prepare(`INSERT INTO retention_intents
    (idempotency_key, case_id, archive_sha256, archive_size, consent_policy_version, diagnostic_payload_version, zip_name, receipt_name, policy, created_at, publish_deadline, state)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto_delete', ?, 0, 'pending')`)
    .run(packet.envelope.idempotency_key, caseId, packet.envelope.archive_sha256, packet.archive.length, FULL_POLICY, 2,
      zipName, receiptName, now - 31 * 86400000);
  try {
    await withGitHub(remote, async () => {
      let pending;
      const ctx = { waitUntil(value) { pending = value; } };
      await retentionWorker.scheduled({}, env, ctx);
      await pending;
    });
    const firstDelete = events.findIndex(event => event.startsWith('DELETE '));
    const firstReceiptRead = events.findIndex(event => event.includes('/releases/assets/2'));
    assert.ok(firstReceiptRead >= 0 && firstDelete > firstReceiptRead, JSON.stringify(events));
    assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 1);
  } finally { env.DB.native.close(); }
});
test('an orphaned remote receipt retains its policy when the retry changes capability', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  env.DB.native.exec("CREATE TRIGGER reject_case_receipt BEFORE UPDATE OF receipt ON cases WHEN NEW.receipt IS NOT NULL BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END");
  try {
    await withGitHub(remote, async () => {
      const failed = await worker.fetch(request(packet), env);
      assert.equal(failed.status, 503);
      assert.equal(remote.assets.size, 2);
      const original = JSON.parse(new TextDecoder().decode([...remote.assets.values()].find(asset => asset.name.endsWith('.receipt.json')).bytes));
      assert.equal(original.retention_policy, 'manual_deletion');
      env.DB.native.exec('DROP TRIGGER reject_case_receipt');
      const retry = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(retry.status, 200, await retry.clone().text());
      assert.deepEqual(await retry.json(), original);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
      assert.equal(remote.uploadCount, 2);
    });
  } finally { env.DB.native.close(); }
});

test('a recovered stored case fences a conflicting remote policy', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const response = await worker.fetch(request(packet), env);
      assert.equal(response.status, 200);
      const first = await response.json();
      const asset = [...remote.assets.values()].find(item => item.name.endsWith('.receipt.json'));
      asset.bytes = encoder.encode(JSON.stringify({ ...first, retention_policy: 'auto_delete',
        retention_until: new Date(Date.parse(first.received_at) + 30 * 86400000).toISOString() }));
      const retry = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(retry.status, 409);
      assert.equal((await retry.json()).error, 'retention_receipt_conflict');
      assert.deepEqual(JSON.parse(env.DB.native.prepare('SELECT receipt FROM cases').get().receipt).retention_policy, first.retention_policy);
      const intent = env.DB.native.prepare('SELECT state, last_error FROM retention_intents').get();
      assert.equal(intent.state, 'manual_review');
      assert.equal(intent.last_error, 'retention_receipt_conflict');
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
    });
  } finally { env.DB.native.close(); }
});

test('retention registration is rejected after its case lease is superseded', async t => {
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
    const pending = worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
    await waiting;
    env.DB.native.prepare("UPDATE cases SET lease_token='replacement', lease_until=?").run(Date.now() + LEASE_MS);
    release();
    const result = await pending;
    assert.equal(result.status, 409);
    assert.equal((await result.json()).error, 'lease_superseded');
    assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
    assert.equal(env.DB.native.prepare('SELECT receipt FROM cases').get().receipt, null);
  });
});

test('failed retention registration rolls back receipt and retries the original remote receipt', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  const packet = await archiveFor();
  env.DB.native.exec("CREATE TRIGGER reject_retention BEFORE INSERT ON retention BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END");
  try {
    await withGitHub(remote, async () => {
      const failed = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(failed.status, 503);
      const row = env.DB.native.prepare('SELECT state, receipt FROM cases').get();
      assert.notEqual(row.state, 'received');
      assert.equal(row.receipt, null);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
      assert.equal(remote.assets.size, 2);
      const original = JSON.parse(new TextDecoder().decode([...remote.assets.values()].find(asset => asset.name.endsWith('.receipt.json')).bytes));
      env.DB.native.exec('DROP TRIGGER reject_retention');
      const retried = await worker.fetch(request(packet), env);
      assert.equal(retried.status, 200, await retried.clone().text());
      assert.deepEqual(await retried.json(), original);
      assert.equal(remote.uploadCount, 2);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 1);
    });
  } finally { env.DB.native.close(); }
});

test('zero-row retention insert rolls back the receipt and retry does not claim success', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  const packet = await archiveFor();
  env.DB.native.exec("CREATE TRIGGER ignore_retention BEFORE INSERT ON retention BEGIN SELECT RAISE(IGNORE); END");
  try {
    await withGitHub(remote, async () => {
      for (let attempt = 0; attempt < 2; attempt++) {
        const response = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
        assert.notEqual(response.status, 200);
        assert.equal(env.DB.native.prepare('SELECT receipt FROM cases').get().receipt, null);
        assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
      }
    });
  } finally { env.DB.native.close(); }
});

test('zero-row cleanup delete rolls back tombstone and retry does not claim deletion', async () => {
  const env = environment({ FEEDBACK_ADMIN_DELETE_TOKEN: 'a'.repeat(32) });
  const remote = new FakeGitHub();
  const packet = await archiveFor();
  try {
    await withGitHub(remote, async () => {
      assert.equal((await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env)).status, 200);
      env.DB.native.exec("CREATE TRIGGER ignore_retention_delete BEFORE DELETE ON retention BEGIN SELECT RAISE(IGNORE); END");
      for (let attempt = 0; attempt < 2; attempt++) {
        const response = await worker.fetch(deleteRequest(packet.envelope.idempotency_key, 'a'.repeat(32)), env);
        assert.notEqual(response.status, 200);
        assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 0);
        assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 1);
        assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 1);
        env.DB.native.prepare('UPDATE retention SET cleanup_lease_until=0').run();
      }
    });
  } finally { env.DB.native.close(); }
});

test('zero-row case delete rolls back retention deletion and tombstone', async () => {
  const env = environment({ FEEDBACK_ADMIN_DELETE_TOKEN: 'a'.repeat(32) });
  const remote = new FakeGitHub();
  const packet = await archiveFor();
  try {
    await withGitHub(remote, async () => {
      assert.equal((await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env)).status, 200);
      env.DB.native.exec("CREATE TRIGGER ignore_case_delete BEFORE DELETE ON cases BEGIN SELECT RAISE(IGNORE); END");
      for (let attempt = 0; attempt < 2; attempt++) {
        const response = await worker.fetch(deleteRequest(packet.envelope.idempotency_key, 'a'.repeat(32)), env);
        assert.notEqual(response.status, 200);
        assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 0);
        assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 1);
        assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 1);
        env.DB.native.prepare('UPDATE retention SET cleanup_lease_until=0').run();
      }
    });
  } finally { env.DB.native.close(); }
});

test('prune reports a failed deletion and leaves it retryable', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  const packet = await archiveFor();
  try {
    await withGitHub(remote, async () => {
      const response = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      const receipt = await response.json();
      const store = { async deleteAndConfirmAsset() { throw new Error('synthetic delete failure'); } };
      const result = await pruneExpiredCases(env, store, Date.parse(receipt.retention_until) + 1);
      assert.equal(result.failed, 1);
      assert.equal(result.deletedCaseIds.length, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 1);
    });
  } finally { env.DB.native.close(); }
});

test('expired retention deletes both remote assets before deleting the D1 case', async () => {
  const packet = await archiveFor({ payload: 2, diagnostics: await fullDiagnostics() });
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const response = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(response.status, 200);
      const receipt = await response.json();
      const store = new GitHubStore('test-installation-token', '1', remote.fetch);
      const retained = env.DB.native.prepare('SELECT retention_until FROM retention WHERE idempotency_key=?').get(packet.envelope.idempotency_key);
      assert.equal(retained.retention_until, Date.parse(receipt.retention_until));
      const result = await pruneExpiredCases(env, store, Date.parse(receipt.retention_until) + 1);
      assert.deepEqual(result.deletedCaseIds, [packet.envelope.idempotency_key]);
      assert.equal(remote.assets.size, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 1);
      const replay = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(replay.status, 409);
      assert.equal(remote.assets.size, 0);
    });
  } finally { env.DB.native.close(); }
});

test('admin deletion needs its secret and removes the retained case only after both assets', async () => {
  const packet = await archiveFor();
  const secret = 'a'.repeat(32);
  const env = environment({ FEEDBACK_ADMIN_DELETE_TOKEN: secret });
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const unknown = await worker.fetch(deleteRequest('b'.repeat(32), secret), env);
      assert.equal(unknown.status, 404);
      assert.deepEqual(await unknown.json(), { error: 'retention_case_not_found' });
      assert.equal((await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env)).status, 200);
      const denied = await worker.fetch(deleteRequest(packet.envelope.idempotency_key, 'wrong'), env);
      assert.equal(denied.status, 401);
      assert.equal(remote.assets.size, 2);
      const deleted = await worker.fetch(deleteRequest(packet.envelope.idempotency_key, secret), env);
      assert.equal(deleted.status, 503);
      assert.deepEqual(await deleted.json(), { error: 'retention_manual_review_required' });
      const repeated = await worker.fetch(deleteRequest(packet.envelope.idempotency_key, secret), env);
      assert.equal(repeated.status, 503);
      assert.deepEqual(await repeated.json(), { error: 'retention_manual_review_required' });
      assert.equal(remote.assets.size, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 1);
    });
  } finally { env.DB.native.close(); }
});


test('cleanup does not race a live upload or allow an upload during cleanup', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const response = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(response.status, 200);
      const receipt = await response.json();
      const store = new GitHubStore('test-installation-token', '1', remote.fetch);
      const due = Date.parse(receipt.retention_until) + 1;
      env.DB.native.prepare('UPDATE cases SET lease_until=?').run(Date.now() + LEASE_MS);
      const result = await pruneExpiredCases(env, store, due);
      assert.equal(result.busy, 1);
      assert.equal(result.deletedCaseIds.length, 0);
      assert.equal(remote.assets.size, 2);
      env.DB.native.prepare('UPDATE cases SET lease_until=0').run();
      env.DB.native.prepare("UPDATE retention SET cleanup_lease_until=?, cleanup_lease_token='other'").run(Date.now() + LEASE_MS);
      const retry = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(retry.status, 409);
      assert.equal(remote.assets.size, 2);
    });
  } finally { env.DB.native.close(); }
});

test('a lost delete response is retried through a 404 and still records both markers', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const accepted = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(accepted.status, 200);
      const receipt = await accepted.json();
      const originalFetch = remote.fetch;
      let lost = true;
      const unreliable = async (input, options = {}) => {
        if (options.method === 'DELETE' && lost) { lost = false; throw new Error('response lost'); }
        return originalFetch(input, options);
      };
      const store = new GitHubStore('test-installation-token', '1', unreliable);
      const due = Date.parse(receipt.retention_until) + 1;
      const result = await pruneExpiredCases(env, store, due);
      assert.deepEqual(result.deletedCaseIds, []);
      assert.equal(env.DB.native.prepare('SELECT zip_deleted FROM retention').get().zip_deleted, 0);
      env.DB.native.prepare('UPDATE retention SET cleanup_lease_until=0').run();
      assert.deepEqual((await pruneExpiredCases(env, store, due)).deletedCaseIds, [packet.envelope.idempotency_key]);
      assert.equal(remote.assets.size, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 1);
    });
  } finally { env.DB.native.close(); }
});

test('cleanup retry preserves deletion markers and atomically removes rows with tombstone', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const accepted = await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env);
      assert.equal(accepted.status, 200, await accepted.clone().text());
      const receipt = await accepted.json();
      const store = new GitHubStore('test-installation-token', '1', remote.fetch);
      env.DB.native.exec("CREATE TRIGGER reject_tombstone BEFORE INSERT ON deleted_cases BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END");
      const due = Date.parse(receipt.retention_until) + 1;
      const first = await pruneExpiredCases(env, store, due);
      assert.deepEqual(first.deletedCaseIds, []);
      const retained = env.DB.native.prepare('SELECT zip_deleted, receipt_deleted FROM retention').get();
      assert.equal(retained.zip_deleted, 1);
      assert.equal(retained.receipt_deleted, 1);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 1);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 0);
      env.DB.native.exec('DROP TRIGGER reject_tombstone');
      env.DB.native.prepare('UPDATE retention SET cleanup_lease_until=0').run();
      assert.deepEqual((await pruneExpiredCases(env, store, due)).deletedCaseIds, [packet.envelope.idempotency_key]);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM cases').get().n, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 0);
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM deleted_cases').get().n, 1);
    });
  } finally { env.DB.native.close(); }
});

test('cleanup stops when its fenced update loses the lease', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      assert.equal((await worker.fetch(request(packet, '/v1/feedback', 'auto-delete-v1'), env)).status, 200);
      const original = env.DB.prepare;
      let fencedUpdate = false;
      let cleanupErrorSql = '';
      let cleanupErrorParams = null;
      env.DB.prepare = sql => {
        const statement = original(sql);
        if (sql.includes('UPDATE retention SET cleanup_error')) {
          cleanupErrorSql = sql;
          return {
            bind(...params) {
              cleanupErrorParams = params;
              return statement.bind(...params);
            },
          };
        }
        if (sql.includes('zip_deleted=1')) {
          fencedUpdate = true;
          return {
            bind(...params) {
              const bound = statement.bind(...params);
              return { async run() { return { success: true, meta: { changes: 0 } }; } };
            },
          };
        }
        return statement;
      };
      const store = new GitHubStore('test-installation-token', '1', remote.fetch);
      const result = await pruneExpiredCases(env, store, Date.now() + 31 * 86400000);
      assert.deepEqual(result.deletedCaseIds, []);
      assert.equal(fencedUpdate, true);
      assert.equal(cleanupErrorSql, '');
      assert.equal(cleanupErrorParams, null);
      assert.equal(remote.assets.size, 1, 'receipt asset must remain when lease ownership is lost');
      assert.equal(env.DB.native.prepare('SELECT COUNT(*) AS n FROM retention').get().n, 1);
    });
  } finally { env.DB.native.close(); }
});

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

// 包一层假 GitHub：每次请求先登记“开始”，让出一次事件循环，再转给真实实现并登记“结束”。
// 串行实现里第二个“开始”必须排在第一个“结束”之后；同时发出时头三条记录都是“开始”。
function recordingFetcher(remote, overrides = {}) {
  const events = [];
  const fetch = async (input, options = {}) => {
    const url = new URL(input);
    const label = options.method === 'POST' ? 'POST ' + url.pathname : url.pathname + (url.searchParams.get('page') ? '?page=' + url.searchParams.get('page') : '');
    events.push('start ' + label);
    await new Promise(resolve => setImmediate(resolve));
    const replaced = overrides[url.pathname];
    const response = replaced ? replaced() : await remote.fetch(input, options);
    events.push('end ' + label);
    return response;
  };
  return { fetch, events };
}

test('the three independent preflight reads are issued together, not one after another', async () => {
  const remote = new FakeGitHub();
  const { fetch, events } = recordingFetcher(remote);
  const packet = await archiveFor();
  await new GitHubStore('test-installation-token', '1', fetch).publish(packet.envelope, packet.archive);
  assert.deepEqual(events.slice(0, 3), [
    'start /repos/' + REPOSITORY,
    'start /repos/' + REPOSITORY + '/releases/1',
    'start /repos/' + REPOSITORY + '/releases/1/assets?page=1',
  ], '仓库、发布、第一页资产必须在任何一个返回之前就全部发出');
});

test('preflight failures keep the original precedence even though the reads run together', async () => {
  const packet = await archiveFor();
  const releasePath = '/repos/' + REPOSITORY + '/releases/1';
  const failing = () => new Response('{}', { status: 500 });
  // 仓库不是私有、发布读取也失败：仍然先报仓库。
  let remote = new FakeGitHub();
  remote.private = false;
  let { fetch } = recordingFetcher(remote, { [releasePath]: failing });
  await assert.rejects(() => new GitHubStore('test-installation-token', '1', fetch).publish(packet.envelope, packet.archive),
    error => error.code === 'repository_not_private');
  // 仓库没问题、发布读取失败：报发布读取的错误。
  remote = new FakeGitHub();
  ({ fetch } = recordingFetcher(remote, { [releasePath]: failing }));
  await assert.rejects(() => new GitHubStore('test-installation-token', '1', fetch).publish(packet.envelope, packet.archive),
    error => error.code === 'github_http_500');
  // 仓库、发布都没问题、第一页资产读取失败：报资产读取的错误。
  remote = new FakeGitHub();
  ({ fetch } = recordingFetcher(remote, { [releasePath + '/assets']: failing }));
  await assert.rejects(() => new GitHubStore('test-installation-token', '1', fetch).publish(packet.envelope, packet.archive),
    error => error.code === 'github_http_500');
});

test('a repository that is not private never receives a write, whatever the other reads returned', async () => {
  const remote = new FakeGitHub();
  remote.private = false;
  const { fetch, events } = recordingFetcher(remote);
  const packet = await archiveFor();
  await assert.rejects(() => new GitHubStore('test-installation-token', '1', fetch).publish(packet.envelope, packet.archive),
    error => error.code === 'repository_not_private');
  assert.equal(remote.uploadCount, 0);
  assert.equal(remote.assets.size, 0);
  assert.equal(events.some(event => event.includes('POST')), false);
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
  const fixed = new RealDate().toISOString();
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
    await sleep(60);
    assert.equal(remote.mintCount, 0);
    env.DB.native.prepare('UPDATE budget SET requests=0 WHERE day=?').run(day);
    env.DB.native.prepare('UPDATE cases SET lease_until=0, lease_token=\'\'').run();
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
    await sleep(60);
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
  // 租约被拒时，同时发出的换令牌请求会在后台继续跑：必须接到假 GitHub 上，不能真的联网。
  await withGitHub(new FakeGitHub(), async () => {
    const refused = await worker.fetch(request(packet), env);
    assert.equal(refused.status, 409);
    assert.equal((await refused.json()).error, 'submission_in_progress');
    assert.equal(refused.headers.get('Retry-After'), '60');
    await sleep(60); // 让后台那路换令牌在假 GitHub 还挂着的时候跑完
  });
  env.DB.native.close();
});

test('a digest mismatch on stored data is a 409 with no Retry-After, because waiting cannot fix it', async () => {
  const packet = await archiveFor();
  const env = environment();
  env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'received', 0, '', '{}')")
    .run(packet.envelope.idempotency_key, '0'.repeat(64));
  const remote = new FakeGitHub();
  await withGitHub(remote, async () => {
    const refused = await worker.fetch(request(packet), env);
    assert.equal(refused.status, 409);
    assert.equal((await refused.json()).error, 'identity_conflict');
    assert.equal(refused.headers.get('Retry-After'), null);
    await sleep(60);
  });
  assert.equal(remote.mintCount, 0, '身份冲突在任何 GitHub 请求之前就被拒，一次令牌都不能换');
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
  // 预检读取是同时发出的，钩子会被触发多次；要守的是每一次观察到的状态都还是 received，而不是触发了几次。
  assert.ok(states.length >= 1, '钩子必须至少被触发一次，否则这条用例什么也没观察');
  assert.ok(states.every(value => value === 'received'), '重放进行中也不能把已收件的行改成别的状态：' + states.join(','));
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

// ---- Server-Timing：只在请求带 X-Skyrocket-Timing: 1 时返回，不带时响应与以前完全一样 ----
function timedRequest(packet, headers = { 'X-Skyrocket-Timing': '1' }) {
  const body = wire(packet);
  return new Request('https://receiver.example/v1/feedback', { method: 'POST', body,
    headers: { 'Content-Type': 'application/vnd.skyrocket.feedback-v1', 'Content-Length': String(body.length), ...headers } });
}
function parseTiming(value) {
  return value.split(', ').map(part => {
    const [name, ...params] = part.split(';');
    const dur = params.map(item => /^dur=(\d+)$/.exec(item)).find(Boolean);
    return { name, dur: dur ? Number(dur[1]) : null };
  });
}
const durations = entries => Object.fromEntries(entries.filter(item => item.dur !== null).map(item => [item.name, item.dur]));
const SEQUENCE = ['headers', 'budget', 'body', 'byte_budget', 'unpack', 'archive', 'case', 'lease', 'github_auth', 'github_storage', 'receipt_commit'];
// gh_token_mint 是换令牌自己的耗时：它和写租约同时进行，所以不属于顺序阶段，先于任何仓库读取记下。
const INNER = ['gh_token_mint', 'gh_preflight', 'gh_read_repo', 'gh_read_release', 'gh_read_assets', 'gh_preflight_serial_sum'];

// 虚拟时钟：只在“数据库操作”和“GitHub 的几个慢路由”上前进固定毫秒数，其余步骤不动。
// 这样每个阶段的耗时是确定的，名字记错、漏记、重复计入都会让精确数字对不上。
function virtualClock(start = Date.now()) {
  const real = Date.now;
  const clock = { now: start, tick(ms) { clock.now += ms; },
    install() { Date.now = () => clock.now; }, restore() { Date.now = real; } };
  return clock;
}
function slowDatabase(env, clock, perOperation = 3) {
  const inner = env.DB;
  env.DB = { native: inner.native, async batch(statements) { clock.tick(perOperation); return inner.batch(statements); }, prepare(sql) { const prepared = inner.prepare(sql); return { bind(...params) {
    const bound = prepared.bind(...params);
    return { async first() { clock.tick(perOperation); return bound.first(); },
      async run() { clock.tick(perOperation); return bound.run(); } };
  } }; } };
}
function slowGitHub(remote, clock, { failZipReadback = false } = {}) {
  let readbacks = 0;
  return { fetch: async (input, options = {}) => {
    const url = new URL(input);
    const method = options.method ?? 'GET';
    const name = url.searchParams.get('name') ?? '';
    if (url.pathname.startsWith('/app/installations/')) clock.tick(50);
    else if (method === 'POST' && name.endsWith('.zip')) clock.tick(200);
    else if (method === 'POST' && name.endsWith('.receipt.json')) clock.tick(120);
    else if (/\/releases\/assets\/\d+$/.test(url.pathname)) {
      readbacks++;
      clock.tick(readbacks === 1 ? 80 : 40);
      if (failZipReadback && readbacks === 1) return new Response('{}', { status: 500 });
    }
    return remote.fetch(input, options);
  } };
}
test('the timer closes each phase when the next one starts and keeps inner entries out of the total', () => {
  let now = 100;
  const timer = createTimer(() => now);
  now += 7; timer.enter('budget');
  now += 11; timer.inner('gh_read_repo', 99);
  now += 5; timer.enter('body');
  now += 2;
  const first = timer.header('isolate;desc=warm');
  assert.equal(first, 'headers;dur=7, budget;dur=16, body;dur=2, gh_read_repo;dur=99, isolate;desc=warm, total;dur=25');
  assert.equal(timer.header('isolate;desc=warm'), first, '再取一次必须得到同样的头，不能重复计入');
});

test('a clock that runs backwards never produces a negative duration, and non-finite readings stay out of the header', () => {
  // 每次读时钟都比上一次早 10：所有区间的原始差值都是负数。必须钳成 0，而且直接比对头的原文——
  // 不能用只认非负整数的解析器去查，它遇到 "-10" 会解析成空值而放行。
  let tick = 50;
  const clamped = createTimer(() => (tick -= 10));
  clamped.enter('budget');
  assert.equal(clamped.header(), 'headers;dur=0, budget;dur=0, total;dur=0');
  const timer = createTimer(() => 0);
  timer.inner('gh_read_repo', Number.NaN);
  timer.inner('gh_read_release', undefined);
  timer.inner('gh_read_assets', -5);
  assert.equal(timer.header(), 'headers;dur=0, gh_read_assets;dur=0, total;dur=0');
});

test('without the opt-in header the response carries no Server-Timing at all', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  try {
    await withGitHub(remote, async () => {
      const response = await worker.fetch(timedRequest(packet, {}), env);
      assert.equal(response.status, 200);
      assert.deepEqual([...response.headers.keys()].sort(), ['cache-control', 'content-type']);
    });
    const bad = new Request('https://receiver.example/v1/feedback', { method: 'POST', body: 'x',
      headers: { 'Content-Type': 'text/plain', 'Content-Length': '1' } });
    const refused = await worker.fetch(bad, env);
    assert.equal(refused.status, 400);
    assert.equal(refused.headers.get('Server-Timing'), null);
  } finally { env.DB.native.close(); }
});

test('a successful upload reports every sequential phase, and each phase is charged to its own step', async () => {
  const packet = await archiveFor();
  const env = environment();
  const clock = virtualClock();
  slowDatabase(env, clock);
  const remote = new FakeGitHub();
  clock.install();
  try {
    await withGitHub(slowGitHub(remote, clock), async () => {
      const response = await worker.fetch(timedRequest(packet), env);
      assert.equal(response.status, 200);
      assert.ok((await response.json()).case_id.startsWith('feedback-'));
      const entries = parseTiming(response.headers.get('Server-Timing'));
      const names = entries.map(item => item.name);
      assert.deepEqual(names.filter(name => SEQUENCE.includes(name)), SEQUENCE, '顺序阶段必须齐全且按处理先后排列');
      assert.deepEqual(names.filter(name => name.startsWith('gh_')),
        [...INNER, 'gh_zip_upload', 'gh_zip_readback', 'gh_receipt_upload', 'gh_receipt_readback']);
      const spent = durations(entries);
      assert.equal(spent.github_auth, 50);
      assert.equal(spent.github_storage, 200 + 80 + 120 + 40);
      assert.equal(spent.gh_zip_upload, 200);
      assert.equal(spent.gh_zip_readback, 80);
      assert.equal(spent.gh_receipt_upload, 120);
      assert.equal(spent.gh_receipt_readback, 40);
      assert.equal(spent.lease, 9);
      assert.equal(spent.receipt_commit, 21);
      // 数据库操作数（每次 3 毫秒）按处理流程逐步数出来：budget 4 次（来源额度、全局额度、当天清扫标记，
      // 全新库里这是当天第一个请求所以还会执行一次清扫删除）；byte_budget 1 次；case 3 次（数案件、插入、读回）。
      assert.equal(spent.budget, 12);
      assert.equal(spent.byte_budget, 3);
      assert.equal(spent.case, 12);
      assert.equal(spent.body + spent.unpack + spent.archive, 0, '纯计算和读请求体不推进虚拟时钟');
      const total = SEQUENCE.reduce((sum, name) => sum + spent[name], 0);
      assert.equal(spent.total, total, '总数必须等于各顺序阶段之和，不能有悄悄消失的时间');
      assert.match(response.headers.get('Server-Timing'), /isolate;desc=(cold|warm)/);
      assert.doesNotMatch(response.headers.get('Server-Timing'), /request_\d/, '不对外给出累计请求数');
    });
  } finally { clock.restore(); env.DB.native.close(); }
});
test('a failure partway through storage still reports the phases it reached, plus the cleanup it cost', async () => {
  const packet = await archiveFor();
  const env = environment();
  const clock = virtualClock();
  slowDatabase(env, clock);
  const remote = new FakeGitHub();
  clock.install();
  try {
    await withGitHub(slowGitHub(remote, clock, { failZipReadback: true }), async () => {
      const response = await worker.fetch(timedRequest(packet), env);
      assert.equal(response.status, 503);
      assert.deepEqual(await response.json(), { error: 'github_http_500', phase: 'github_storage' });
      const entries = parseTiming(response.headers.get('Server-Timing'));
      const names = entries.map(item => item.name);
      assert.deepEqual(names.filter(name => !name.startsWith('gh_') && name !== 'isolate' && name !== 'total'),
        [...SEQUENCE.slice(0, SEQUENCE.indexOf('github_storage') + 1), 'lease_release', 'error_cleanup'],
        '失败后只有走到的阶段，外加释放租约和退额度两步');
      const spent = durations(entries);
      assert.equal(spent.github_storage, 200 + 80, '读回失败时上传和读回的耗时都要算进去');
      assert.equal(spent.gh_zip_readback, 80, '抛错的那一步也要留下耗时');
      assert.equal(spent.lease_release, 3);
      assert.ok(spent.error_cleanup >= 3, '退额度至少有一次数据库写入');
      assert.equal(spent.total, [...SEQUENCE.slice(0, SEQUENCE.indexOf('github_storage') + 1), 'lease_release', 'error_cleanup']
        .reduce((sum, name) => sum + spent[name], 0));
      assert.equal(names.includes('receipt_commit'), false);
    });
  } finally { clock.restore(); env.DB.native.close(); }
});

test('a request refused at the door still answers with timing when asked, and only its own phases', async () => {
  const env = environment();
  try {
    const refused = await worker.fetch(new Request('https://receiver.example/v1/feedback', { method: 'POST', body: 'x',
      headers: { 'Content-Type': 'text/plain', 'Content-Length': '1', 'X-Skyrocket-Timing': '1' } }), env);
    assert.equal(refused.status, 400);
    assert.deepEqual(await refused.json(), { error: 'request_headers', phase: 'headers' });
    const names = parseTiming(refused.headers.get('Server-Timing')).map(item => item.name);
    assert.deepEqual(names, ['headers', 'error_cleanup', 'isolate', 'total']);
    // 这条请求是合法上传，会一路走到 GitHub，所以必须挂假 GitHub，不能让测试真的去访问线上接口。
    const remote = new FakeGitHub();
    await withGitHub(remote, async () => {
      const other = await worker.fetch(timedRequest(await archiveFor(), { 'X-Skyrocket-Timing': 'yes' }), env);
      assert.equal(other.status, 200);
      assert.equal(other.headers.get('Server-Timing'), null, '只有精确的 1 才算打开计时');
    });
    assert.equal(remote.assets.size, 2, '上传确实走完了，只是没有计时头');
  } finally { env.DB.native.close(); }
});

test('the three preflight reads overlap in real time: preflight takes about the slowest read, not the sum', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  const base = '/repos/' + REPOSITORY;
  const delays = new Map([[base, 100], [base + '/releases/1', 200], [base + '/releases/1/assets', 300]]);
  const delayed = { fetch: async (input, options = {}) => {
    const url = new URL(input);
    const wait = (options.method ?? 'GET') === 'GET' ? delays.get(url.pathname) : undefined;
    if (wait) await new Promise(resolve => setTimeout(resolve, wait));
    return remote.fetch(input, options);
  } };
  try {
    await withGitHub(delayed, async () => {
      const response = await worker.fetch(timedRequest(packet), env);
      assert.equal(response.status, 200);
      const spent = durations(parseTiming(response.headers.get('Server-Timing')));
      assert.ok(spent.gh_read_repo >= 90 && spent.gh_read_release >= 190 && spent.gh_read_assets >= 290, JSON.stringify(spent));
      // 用同一次请求里的相对关系判断，不依赖机器忙闲：并发时预检约等于最慢那项（300），串行才是三项之和（600）。
      // 两者差 300 毫秒，留 150 毫秒的余量给定时器抖动，仍然分得开。
      assert.ok(spent.gh_preflight_serial_sum - spent.gh_preflight >= 150,
        `并发应当省下至少 150 毫秒：预检 ${spent.gh_preflight}，串行之和 ${spent.gh_preflight_serial_sum}`);
      assert.ok(spent.gh_preflight >= spent.gh_read_assets - 5, '预检不可能比最慢的那项还短');
    });
  } finally { env.DB.native.close(); }
});

test('the isolate is labelled cold only for its first upload request and never reveals a running count', async () => {
  const env = environment();
  try {
    const labels = [];
    for (let attempt = 0; attempt < 3; attempt++) {
      const response = await worker.fetch(new Request('https://receiver.example/v1/feedback', { method: 'POST', body: 'x',
        headers: { 'Content-Type': 'text/plain', 'Content-Length': '1', 'X-Skyrocket-Timing': '1' } }), env);
      const header = response.headers.get('Server-Timing');
      labels.push(/isolate;desc=(cold|warm)/.exec(header)[1]);
      assert.doesNotMatch(header, /request_\d|\d+;desc/, '头里不能带累计数字');
    }
    // 本文件里别的用例已经让这个运行实例处理过上传请求，所以这里三次都是 warm；cold 只可能出现在整个进程的第 1 个请求。
    assert.deepEqual(labels, ['warm', 'warm', 'warm']);
  } finally { env.DB.native.close(); }
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

// ---- 安装令牌的实例内缓存：同一个运行实例里不该每个请求都向 GitHub 换一次 ----
async function uploadsOnOneInstance(remote, env, count) {
  const statuses = [];
  await withGitHub(remote, async () => {
    for (let n = 0; n < count; n++) statuses.push((await worker.fetch(request(await archiveFor({ text: 'SYNTHETIC TEST ONLY ' + n })), env)).status);
  });
  return statuses;
}

test('two different uploads on one instance share one installation token', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  try {
    assert.deepEqual(await uploadsOnOneInstance(remote, env, 2), [200, 200]);
    assert.equal(remote.assets.size, 4, '两份反馈各占 zip 和回执两个资产，说明确实是两个不同的案件');
    assert.equal(remote.mintCount, 1, '第二个请求必须复用缓存的令牌，不能再向 GitHub 换');
  } finally { env.DB.native.close(); }
});

test('two concurrent uploads on one instance share the in-flight installation token request', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  const slowRemote = { fetch: async (input, options = {}) => {
    if (new URL(input).pathname.startsWith('/app/installations/')) await sleep(150);
    return remote.fetch(input, options);
  } };
  try {
    const packets = await Promise.all([archiveFor({ text: 'CONCURRENT SYNTHETIC TEST 1' }), archiveFor({ text: 'CONCURRENT SYNTHETIC TEST 2' })]);
    const statuses = await withGitHub(slowRemote, () => Promise.all(packets.map(packet => worker.fetch(request(packet), env).then(response => response.status))));
    assert.deepEqual(statuses.sort(), [200, 200]);
    assert.equal(remote.mintCount, 1, '并发请求必须等待同一个进行中的换令牌请求');
  } finally { env.DB.native.close(); }
});

test('a failed in-flight token request is cleared before the next request retries', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  let failed = true;
  const flakyRemote = { fetch: async (input, options = {}) => {
    if (new URL(input).pathname.startsWith('/app/installations/') && failed) {
      failed = false;
      return new Response('{}', { status: 500 });
    }
    return remote.fetch(input, options);
  } };
  try {
    const packet = await archiveFor({ text: 'RETRY SYNTHETIC TEST' });
    await withGitHub(flakyRemote, async () => {
      const refused = await worker.fetch(request(packet), env);
      assert.equal(refused.status, 503);
      const accepted = await worker.fetch(request(packet), env);
      assert.equal(accepted.status, 200);
    });
    assert.equal(remote.mintCount, 1, '第一次换令牌失败后，下一次请求必须重新换令牌');
  } finally { env.DB.native.close(); }
});

 test('a token that is about to expire is not reused', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  remote.tokenLifetimeMs = 30000; // 短于提前作废的一分钟余量：缓存里的令牌一拿到就已经算过期
  try {
    assert.deepEqual(await uploadsOnOneInstance(remote, env, 2), [200, 200]);
    assert.equal(remote.mintCount, 2, '快到期的令牌不能复用：每个请求都要换新的');
  } finally { env.DB.native.close(); }
});

// ---- 换令牌与写租约同时进行：令牌不依赖租约的结果，没必要排在它后面 ----
function delayLeaseWrite(env, ms) {
  const inner = env.DB;
  env.DB = { native: inner.native, async batch(statements) { return inner.batch(statements); }, prepare(sql) { const prepared = inner.prepare(sql); return { bind(...params) {
    const bound = prepared.bind(...params);
    const lag = sql.startsWith('UPDATE cases SET state=CASE') ? ms : 0;
    return { async first() { if (lag) await sleep(lag); return bound.first(); }, async run() { return bound.run(); } };
  } }; } };
}

test('the token request and the lease write overlap in real time: the token wait is hidden behind the lease', async () => {
  const packet = await archiveFor();
  const env = environment();
  delayLeaseWrite(env, 300);
  const remote = new FakeGitHub();
  const slowToken = { fetch: async (input, options = {}) => {
    if (new URL(input).pathname.startsWith('/app/installations/')) await sleep(300);
    return remote.fetch(input, options);
  } };
  try {
    await withGitHub(slowToken, async () => {
      const response = await worker.fetch(timedRequest(packet), env);
      assert.equal(response.status, 200);
      const spent = durations(parseTiming(response.headers.get('Server-Timing')));
      assert.ok(spent.lease >= 290, '租约写入被拖慢了 300 毫秒：' + JSON.stringify(spent));
      assert.ok(spent.gh_token_mint >= 290, '换令牌本身要 300 毫秒：' + JSON.stringify(spent));
      // 同一次请求里的相对关系：串行时 github_auth 约等于换令牌的 300，并行时只剩换令牌与租约之间的差。
      assert.ok(spent.gh_token_mint - spent.github_auth >= 150,
        `换令牌应当被租约写入盖住至少 150 毫秒：换令牌 ${spent.gh_token_mint}，github_auth ${spent.github_auth}`);
    });
  } finally { env.DB.native.close(); }
});

test('the overlapped token request is bound by the publish deadline too', async () => {
  const packet = await archiveFor();
  const env = environment();
  const realNow = Date.now;
  let skewed = false;
  // 租约写入的那一刻（换令牌已发出、请求还没真正出门）时钟越过总时限：换令牌必须按“已超时”失败，
  // 而不是拿一个默认的 30 秒上限继续等。假接口只在收到中止信号时才失败，所以没传时限就会照常成功。
  const inner = env.DB;
  env.DB = { native: inner.native, prepare(sql) { const prepared = inner.prepare(sql); return { bind(...params) {
    const bound = prepared.bind(...params);
    return { async first() { if (sql.startsWith('UPDATE cases SET state=CASE')) skewed = true; return bound.first(); },
      async run() { return bound.run(); } };
  } }; } };
  const remote = new FakeGitHub();
  const abortable = { fetch: async (input, options = {}) => {
    if (new URL(input).pathname.startsWith('/app/installations/')) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, 150);
        options.signal.addEventListener('abort', () => { clearTimeout(timer); reject(options.signal.reason); });
      });
    }
    return remote.fetch(input, options);
  } };
  Date.now = () => realNow() + (skewed ? PUBLISH_DEADLINE_MS + 1000 : 0);
  try {
    await withGitHub(abortable, async () => {
      const refused = await worker.fetch(request(packet), env);
      assert.equal(refused.status, 503);
      assert.deepEqual(await refused.json(), { error: 'upstream_timeout', phase: 'github_auth' });
    });
  } finally { Date.now = realNow; env.DB.native.close(); }
  assert.equal(remote.assets.size, 0);
});

test('a refused lease answers at once and a failing overlapped token request leaves no unhandled rejection', async () => {
  const packet = await archiveFor();
  const env = environment();
  env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'uploading', ?, ?, NULL)")
    .run(packet.envelope.idempotency_key, packet.envelope.archive_sha256, Date.now() + 120000, 'someone-else');
  const rejections = [];
  const record = reason => rejections.push(reason);
  process.on('unhandledRejection', record);
  let mintFinished = false;
  try {
    await withGitHub({ fetch: async () => { await sleep(150); mintFinished = true; return new Response('{}', { status: 500 }); } }, async () => {
      const refused = await worker.fetch(request(packet), env);
      assert.equal(refused.status, 409);
      assert.equal((await refused.json()).error, 'submission_in_progress');
      assert.equal(mintFinished, false, '租约被拒时直接回应，不能等一个已经没用的令牌请求');
      await sleep(300);
      assert.equal(mintFinished, true, '换令牌那一路确实在后台跑完并失败了，否则这条用例什么也没证明');
    });
  } finally { process.off('unhandledRejection', record); env.DB.native.close(); }
  assert.deepEqual(rejections, []);
});

test('a token minted for one installation is never used for another', async () => {
  const first = environment();
  const second = environment();
  const remote = new FakeGitHub();
  try {
    assert.deepEqual(await uploadsOnOneInstance(remote, first, 1), [200]);
    assert.equal(remote.mintCount, 1);
    assert.deepEqual(await uploadsOnOneInstance(remote, second, 1), [200]);
    assert.equal(remote.mintCount, 2, '换了安装号就是另一个身份，必须重新换令牌');
  } finally { first.DB.native.close(); second.DB.native.close(); }
});

test('cold-cache uploads on one installation share one in-flight token request', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  remote.mintDelayMs = 120;
  const first = await archiveFor({ text: 'SYNTHETIC CONCURRENT ONE' });
  const second = await archiveFor({ text: 'SYNTHETIC CONCURRENT TWO' });
  try {
    await withGitHub(remote, async () => {
      const results = await Promise.all([worker.fetch(request(first), env), worker.fetch(request(second), env)]);
      assert.deepEqual(results.map(result => result.status), [200, 200]);
    });
    assert.equal(remote.mintCount, 1, '冷缓存并发上传也只能向 GitHub 换一次令牌');
    assert.equal(remote.assets.size, 4, '两份不同反馈都正常收件，不能因为合并令牌请求而丢一份');
  } finally { env.DB.native.close(); }
});

test('a burst of refused leases shares one background token request instead of multiplying GitHub auth calls', async () => {
  const packet = await archiveFor();
  const env = environment();
  const remote = new FakeGitHub();
  remote.mintDelayMs = 120;
  env.DB.native.prepare("INSERT INTO cases VALUES (?, ?, 'uploading', ?, ?, NULL)")
    .run(packet.envelope.idempotency_key, packet.envelope.archive_sha256, Date.now() + 120000, 'someone-else');
  try {
    await withGitHub(remote, async () => {
      const results = await Promise.all(Array.from({ length: 6 }, () => worker.fetch(request(packet), env)));
      assert.deepEqual(results.map(result => result.status), [409, 409, 409, 409, 409, 409]);
      await sleep(220);
    });
    assert.equal(remote.mintCount, 1, '租约都被拒也不能把 6 次重试放大成 6 次 GitHub 认证');
    assert.equal(remote.assets.size, 0);
  } finally { env.DB.native.close(); }
});

test('a GitHub 401 or 403 invalidates the cached token and retries the whole publication once', async () => {
  for (const status of [401, 403]) {
    const env = environment();
    const remote = new FakeGitHub();
    remote.authFailures = 1;
    remote.authFailureStatus = status;
    const logged = [];
    const originalError = console.error;
    console.error = (line) => { logged.push(String(line)); };
    try {
      await withGitHub(remote, async () => {
        const response = await worker.fetch(request(await archiveFor({ text: 'SYNTHETIC AUTH RETRY ' + status })), env);
        assert.equal(response.status, 200, '认证失败后应从预检开始完整重试一次：' + status);
      });
      assert.equal(remote.mintCount, 2, '旧令牌清掉后只换一次新令牌：' + status);
      assert.equal(remote.assets.size, 2, '重试必须利用资产名幂等，不重复写入：' + status);
      const retried = logged.map((line) => { try { return JSON.parse(line); } catch { return null; } })
        .filter((entry) => entry?.event === 'feedback_auth_retried');
      assert.equal(retried.length, 1, '重试成功也要留一条日志，否则令牌被撤销过无从查起：' + status);
      assert.equal(retried[0].code, 'github_http_' + status);
      assert.ok(!JSON.stringify(retried[0]).includes('ghs_'), '日志里不许出现令牌');
    } finally { console.error = originalError; env.DB.native.close(); }
  }
});

test('a second GitHub 401 or 403 is returned after one retry rather than looping or keeping the bad cache', async () => {
  for (const status of [401, 403]) {
    const env = environment();
    const remote = new FakeGitHub();
    remote.authFailures = 9;
    remote.authFailureStatus = status;
    try {
      await withGitHub(remote, async () => {
        const response = await worker.fetch(request(await archiveFor({ text: 'SYNTHETIC AUTH STOPS ' + status })), env);
        assert.equal(response.status, 503);
        assert.equal((await response.json()).error, 'github_http_' + status);
      });
      assert.equal(remote.mintCount, 2, '最多刷新一次令牌，不能无限重试：' + status);
      assert.equal(remote.assets.size, 0);
    } finally { env.DB.native.close(); }
  }
});

test('an auth failure after the ZIP write retries from the preflight and reuses the already stored ZIP', async () => {
  const env = environment();
  const remote = new FakeGitHub();
  remote.authFailureAfterUploads = 1;
  try {
    await withGitHub(remote, async () => {
      const response = await worker.fetch(request(await archiveFor({ text: 'SYNTHETIC AUTH AFTER ZIP' })), env);
      assert.equal(response.status, 200);
    });
    assert.equal(remote.mintCount, 2, '认证失效后只刷新一次令牌');
    assert.equal(remote.uploadCount, 2, 'ZIP 已写入，重试只补回执，不能上传第二份同名 ZIP');
    assert.equal(remote.assets.size, 2);
  } finally { env.DB.native.close(); }
});

test('different installations can mint tokens concurrently without sharing in-flight state', async () => {
  const first = environment();
  const second = environment();
  const remote = new FakeGitHub();
  const slowRemote = { fetch: async (input, options = {}) => {
    if (new URL(input).pathname.startsWith('/app/installations/')) await sleep(100);
    return remote.fetch(input, options);
  } };
  try {
    const packets = await Promise.all([archiveFor({ text: 'INSTALLATION A' }), archiveFor({ text: 'INSTALLATION B' })]);
    const statuses = await withGitHub(slowRemote, () => Promise.all([
      worker.fetch(request(packets[0]), first).then(response => response.status),
      worker.fetch(request(packets[1]), second).then(response => response.status),
    ]));
    assert.deepEqual(statuses.sort(), [200, 200]);
    assert.equal(remote.mintCount, 2, '不同安装号必须各自换令牌');
  } finally { first.DB.native.close(); second.DB.native.close(); }
});

test('no test in this file reached the real network', async () => {
  await sleep(100); // 后台那路换令牌可能在前面某条用例结束后才发出，给它们落地的时间
  assert.deepEqual(strayRequests, []);
});
