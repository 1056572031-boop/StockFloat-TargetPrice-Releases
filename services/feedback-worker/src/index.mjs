import { ZipReader, Uint8ArrayReader } from '@zip.js/zip.js';
import { visit } from 'jsonc-parser';

export const REPOSITORY = '1056572031-boop/Skyrocket-Private-Feedback';
export const INBOX_TAG = 'feedback-inbox-v1';
export const POLICY = 'github-private-feedback-v1';
export const FULL_POLICY = 'github-private-feedback-full-v2';
export const FULL_V3_POLICY = 'github-private-feedback-full-v3';
export const V3_DIAGNOSTIC_FILES = new Set([
  'environment.json', 'permissions.json', 'feedback_preferences.json', 'feedback_context.json',
]);
export const MAX_UPLOAD = 32 * 1024 * 1024;
export const MAX_RELEASE_ASSETS = 1000;
const MAX_HEADER = 64 * 1024;
const KEY = /^[a-f0-9]{32}$/;
const HASH = /^[a-f0-9]{64}$/;
const utf8 = new TextDecoder('utf-8', { fatal: true });
let activeBodyBytes = 0;
export const LEASE_MS = 120000;
// GitHub 阶段的总时限必须短于租约：慢到租约过期才写回，会被别的重试顶掉，回执白白丢失。
export const PUBLISH_DEADLINE_MS = 100000;
// 单个来源每天最多的请求数。全局额度被一个来源灌满，别的用户当天就全部失败。
export const CLIENT_DAILY_LIMIT = 30;
export const CASE_PRUNE_AT = 900;
// 每天最多退还多少次全局额度。退款让垃圾请求不再烧光当天额度，但每次退款都是一笔数据库写入；
// 不设上限的话，换着来源地址发垃圾包的人能无限放大写入。超过上限就不再退，回到“垃圾也占额度”的保守行为。
export const REFUND_DAILY_LIMIT = 500;
// 这几个阶段失败说明请求本身有问题或者还没真正开始处理，不属于服务端的错，也不该替来源退额度。
const CLIENT_SIDE_PHASES = ['headers', 'budget', 'body'];

function secondsToUtcMidnight() {
  const now = new Date();
  const next = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1);
  return Math.max(1, Math.ceil((next - now.getTime()) / 1000));
}

// 429 才有意义的“多久后再来”；其余状态不带，避免客户端对永久错误傻等。
export function retryAfter(code) {
  if (code === 'receiver_busy') return 30;
  if (code === 'submission_in_progress') return 60;
  if (['daily_budget', 'daily_byte_budget', 'daily_client_budget'].includes(code)) return secondsToUtcMidnight();
  return null;
}

export class Rejection extends Error {
  constructor(code, status = 422) { super(code); this.code = code; this.status = status; }
}
const reject = (code, status) => { throw new Rejection(code, status); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, names) => object(value) && Object.keys(value).sort().join(',') === [...names].sort().join(',');
const hex = value => [...new Uint8Array(value)].map(x => x.toString(16).padStart(2, '0')).join('');
export const digest = async bytes => hex(await crypto.subtle.digest('SHA-256', bytes));

function checkDiagnosticPrivacy(text) {
  if (/-----(?:BEGIN|END) (?:[A-Z]+ )?PRIVATE KEY-----/.test(text)
      || /\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/.test(text)) reject('diagnostic_secret');
  const credential = /(?:^|[^\w])["']?(password|passwd|token|api[_-]?key|private[_-]?key|cookie|authorization)["']?\s*[:=]\s*(?:Bearer\s+)?("[^"\r\n]*"?|'[^'\r\n]*'?|[^\s,;&"'{}()\[\]\\]+)/gim;
  for (const match of text.matchAll(credential)) {
    const value = match[2].replace(/^["']|["']$/g, '');
    if (match[1].toLowerCase() === 'authorization' && ['authorized', 'denied', 'notDetermined', 'provisional', 'ephemeral', 'unknown', 'no_bundle'].includes(value)) continue;
    if (!['<REDACTED>', '***REDACTED***', '', 'null', 'None'].includes(value)) reject('diagnostic_secret');
  }
}

export function strictJson(bytes, diagnostic = false) {
  const text = typeof bytes === 'string' ? bytes : utf8.decode(bytes);
  const stack = [];
  visit(text, {
    onObjectBegin() { stack.push(new Set()); if (stack.length > (diagnostic ? 32 : 8)) reject('json_depth'); },
    onObjectProperty(name) {
      const keys = stack.at(-1);
      if (!name || name.length > (diagnostic ? 4096 : 96) || keys.has(name) || keys.size >= (diagnostic ? 4096 : 64)) reject('json_keys');
      keys.add(name);
    },
    onObjectEnd() { stack.pop(); },
    onArrayBegin() { stack.push(null); if (stack.length > (diagnostic ? 32 : 8)) reject('json_depth'); },
    onArrayEnd() { stack.pop(); },
    onLiteralValue(value) {
      if (typeof value === 'string' && [...value].length > (diagnostic ? 256 * 1024 : 4096)) reject('json_string');
      if (typeof value === 'number' && (!Number.isFinite(value) || !diagnostic && !Number.isSafeInteger(value))) reject('json_number');
    },
    onError() { reject('invalid_json'); },
  }, { disallowComments: true, allowTrailingComma: false, allowEmptyContent: false });
  return JSON.parse(text);
}

export async function readBounded(response, maximum) {
  if (!response.body) return new Uint8Array();
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > maximum) { await reader.cancel(); reject('body_limit', 413); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let position = 0;
  for (const chunk of chunks) { bytes.set(chunk, position); position += chunk.length; }
  return bytes;
}

export async function unpackRequest(bytes) {
  if (bytes.length < 5) reject('short_envelope');
  const length = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0);
  if (!length || length > MAX_HEADER || 4 + length >= bytes.length) reject('envelope_limit');
  const envelope = strictJson(bytes.subarray(4, 4 + length));
  const names = ['schema_version', 'diagnostic_payload_version', 'idempotency_key', 'archive_sha256',
    'size_bytes', 'consent_policy_version', 'app_version', 'outbox_id'];
  const expectedPolicy = envelope.diagnostic_payload_version === 1 ? POLICY
    : envelope.diagnostic_payload_version === 2 ? FULL_POLICY
    : envelope.diagnostic_payload_version === 3 ? FULL_V3_POLICY : null;
  if (!exact(envelope, names) || envelope.schema_version !== 1 || ![1, 2, 3].includes(envelope.diagnostic_payload_version)
      || !KEY.test(envelope.idempotency_key) || !KEY.test(envelope.outbox_id) || !HASH.test(envelope.archive_sha256)
      || envelope.consent_policy_version !== expectedPolicy
      || !/^\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(envelope.app_version)) reject('envelope_schema');
  const archive = bytes.subarray(4 + length);
  if (archive.length > (envelope.diagnostic_payload_version >= 2 ? MAX_UPLOAD : 256 * 1024) || envelope.size_bytes !== archive.length
      || await digest(archive) !== envelope.archive_sha256) reject('archive_binding');
  return { envelope, archive };
}

export function validateState(state, envelope) {
  const names = ['app_version', 'bundle_id', 'run_id', 'phase', 'capture_started_at',
    'capture_completed_at', 'health', 'counts', 'summary', 'omitted', 'truncated'];
  if (!exact(state, names) || state.app_version !== envelope.app_version || state.bundle_id !== 'com.skyrocket.app'
      || state.phase !== 'user_feedback' || state.health !== null || !/^[a-f0-9]{6}$/.test(state.run_id)) reject('state_schema');
  for (const key of ['capture_started_at', 'capture_completed_at']) {
    if (typeof state[key] !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(state[key]) || !Number.isFinite(Date.parse(state[key]))) reject('capture_time');
  }
  if (!object(state.counts) || Object.entries(state.counts).some(([name, value]) =>
    !/^[A-Za-z0-9_.:#-]{1,96}$/.test(name) || (typeof value !== 'boolean' && !Number.isSafeInteger(value)))) reject('counter_schema');
  const summaryKeys = new Set(['capture_issue_count', 'loaded_modules', 'net_ok', 'minute_stale', 'price_is_fallback', 'pct_unknown']);
  if (!object(state.summary) || Object.entries(state.summary).some(([name, value]) =>
    !summaryKeys.has(name) || (typeof value !== 'boolean' && !Number.isSafeInteger(value)))) reject('summary_schema');
  const omitted = new Set(['raw_logs', 'config', 'quotes', 'targets', 'camera', 'screenshots', 'all_real_user_data']);
  if (!Array.isArray(state.omitted) || state.omitted.some(x => !omitted.has(x))
      || !Array.isArray(state.truncated) || state.truncated.some(x => !['automatic_collection', 'counters', 'diagnostic_attachments'].includes(x))) reject('omission_schema');
  if (envelope.diagnostic_payload_version !== 2 && state.truncated.includes('diagnostic_attachments')) reject('omission_schema');
}

function checkV3Snapshot(name, value) {
  const schemas = {
    'environment.json': 'environment.v1',
    'permissions.json': 'permissions.v1',
    'feedback_preferences.json': 'feedback_preferences.v1',
    'feedback_context.json': 'feedback_context.v1',
  };
  const reason = new Set(['query_failed', 'unsupported', 'no_bundle', 'not_applicable']);
  if (!object(value) || value.schema !== schemas[name] || typeof value.available !== 'boolean') reject('diagnostic_v3_snapshot');
  if (value.available) {
    if (!exact(value, ['schema', 'available', 'data']) || !object(value.data)) reject('diagnostic_v3_snapshot');
  } else if (!exact(value, ['schema', 'available', 'reason']) || !reason.has(value.reason)) reject('diagnostic_v3_snapshot');
}

export async function validateDiagnostics(archive, deep = true, payload = 2) {
  const jsonNames = new Set(['network.json', 'startup_health.json', 'feedback.json', 'feedback_ui.json', 'targets.json',
    'runtime.json', 'glass.json', 'notify.json', 'universe.json', 'calendar.json', 'state.json',
    'storage.json', 'config.json', 'last_failures.json', 'export.json', ...V3_DIAGNOSTIC_FILES]);
  const reader = new ZipReader(new Uint8ArrayReader(archive), { useWebWorkers: false });
  const inventory = new Map();
  let expanded = 0, exported;
  try {
    const entries = [];
    for await (const entry of reader.getEntriesGenerator()) {
      entries.push(entry);
      if (entries.length > 40) reject('diagnostic_members');
    }
    if (!entries.length || entries.length > 40 || reader.warnings?.length) reject('diagnostic_members');
    const folded = new Set();
    for (const entry of entries) {
      const name = entry.filename;
      const log = /^debug\.log(?:\.[1-3])?$/.test(name);
      const text = ['diagnostics.txt', 'last_failures.invalid.txt'].includes(name)
        || /^crash_reports\/[^/\\\x00]{1,180}\.txt$/u.test(name);
      const cap = log ? 5 * 1024 * 1024 : jsonNames.has(name) || text ? 256 * 1024 : 0;
      expanded += entry.uncompressedSize;
      if (!cap || name.startsWith('.') || name.startsWith('/') || name.split('/').includes('..')
          || folded.has(name.normalize('NFC').toLowerCase()) || entry.directory || entry.encrypted || entry.symlink
          || ![0, 8].includes(entry.compressionMethod) || entry.executable
          || entry.warnings?.length || entry.uncompressedSize > cap || expanded > MAX_UPLOAD) reject('diagnostic_member');
      folded.add(name.normalize('NFC').toLowerCase());
      inventory.set(name, entry.uncompressedSize);
      if (!deep) {
        await entry.getData(new WritableStream(), { checkOverlappingEntryOnly: true,
          checkLocalDirectory: true, checkLocalFilename: true, useWebWorkers: false });
        if (entry.warnings?.length) reject('diagnostic_local_header');
        continue;
      }
      let size = 0;
      const chunks = [];
      await entry.getData(new WritableStream({ write(chunk) {
        size += chunk.length;
        if (size > cap) reject('diagnostic_inflated_limit');
        chunks.push(new Uint8Array(chunk));
      } }), { checkSignature: true, useWebWorkers: false });
      if (size !== entry.uncompressedSize) reject('diagnostic_size');
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      checkDiagnosticPrivacy(utf8.decode(bytes));
      if (jsonNames.has(name)) {
        const value = strictJson(bytes, true);
        if (name === 'export.json') exported = value;
        if (V3_DIAGNOSTIC_FILES.has(name)) checkV3Snapshot(name, value);
      } else utf8.decode(bytes);
    }
  } finally { await reader.close(); }
  if (!deep) {
    if (!inventory.has('diagnostics.txt') || !inventory.has('export.json')) reject('diagnostic_export');
    if (payload === 3 && [...V3_DIAGNOSTIC_FILES].some(name => !inventory.has(name))) reject('diagnostic_v3_missing');
    if (payload !== 3 && [...V3_DIAGNOSTIC_FILES].some(name => inventory.has(name))) reject('diagnostic_v3_mixed');
    return null;
  }
  if (!inventory.has('diagnostics.txt') || !object(exported) || !Array.isArray(exported.issues)
      || !Array.isArray(exported.files) || exported.files.length !== inventory.size - 1) reject('diagnostic_export');
  const v3 = exported.diagnostic_schema_version === 3;
  if (v3 && [...V3_DIAGNOSTIC_FILES].some(name => !inventory.has(name))) reject('diagnostic_v3_missing');
  if (!v3 && [...V3_DIAGNOSTIC_FILES].some(name => inventory.has(name))) reject('diagnostic_v3_mixed');
  for (const name of jsonNames) {
    // feedback_ui.json 自 1.4.104 起才随包带上；旧版客户端没有它，不能因此被拒。
    if (['config.json', 'last_failures.json', 'feedback_ui.json', ...V3_DIAGNOSTIC_FILES].includes(name)) continue;
    if (!inventory.has(name) && !exported.issues.some(issue => typeof issue === 'string' && issue.includes(name))) reject('diagnostic_missing_category');
  }
  const declared = new Set();
  for (const item of exported.files) {
    if (!object(item) || item.name === 'export.json' || declared.has(item.name)
        || inventory.get(item.name) !== item.size_bytes) reject('diagnostic_inventory');
    declared.add(item.name);
  }
  return exported;
}

export async function validateArchive(archive, envelope, deepDiagnostics = true) {
  const reader = new ZipReader(new Uint8ArrayReader(archive), { useWebWorkers: false });
  const files = new Map();
  const caps = { 'manifest.json': 16 * 1024, 'state.json': 32 * 1024, 'problem.txt': 16 * 1024 };
  const full = envelope.diagnostic_payload_version >= 2;
  if (full) caps['diagnostics.zip'] = MAX_UPLOAD;
  try {
    const entries = [];
    for await (const entry of reader.getEntriesGenerator()) {
      entries.push(entry);
      if (entries.length > Object.keys(caps).length) reject('zip_members');
    }
    if (entries.length !== Object.keys(caps).length || reader.warnings?.length) reject('zip_members');
    for (const entry of entries) {
      if (!Object.hasOwn(caps, entry.filename)) reject('zip_member');
      const cap = caps[entry.filename];
      if (!cap || files.has(entry.filename) || entry.directory || entry.encrypted || entry.symlink
          || entry.warnings?.length || entry.uncompressedSize > cap || entry.compressedSize > MAX_UPLOAD) reject('zip_member');
      if (full && entry.filename === 'diagnostics.zip' && entry.compressionMethod !== 0) reject('diagnostic_must_be_stored');
      if (entry.filename === 'diagnostics.zip') {
        await entry.getData(new WritableStream(), { checkOverlappingEntryOnly: true,
          checkLocalDirectory: true, checkLocalFilename: true, useWebWorkers: false });
        const offset = entry.localDirectory?.dataOffset;
        if (!Number.isSafeInteger(offset) || entry.compressedSize !== entry.uncompressedSize
            || offset + entry.uncompressedSize > archive.length || entry.warnings?.length) reject('diagnostic_local_header');
        files.set(entry.filename, archive.subarray(offset, offset + entry.uncompressedSize));
        continue;
      }
      let size = 0;
      const data = new Uint8Array(entry.uncompressedSize);
      const writer = new WritableStream({ write(chunk) {
        if (size + chunk.length > data.length) reject('inflated_limit');
        data.set(chunk, size);
        size += chunk.length;
      } });
      await entry.getData(writer, { checkSignature: true, checkOverlappingEntry: true, useWebWorkers: false });
      if (size !== entry.uncompressedSize) reject('zip_size');
      files.set(entry.filename, data);
    }
  } finally { await reader.close(); }
  const manifest = strictJson(files.get('manifest.json'));
  const names = ['schema_version', 'diagnostic_payload_version', 'app_version', 'bundle_id', 'run_id',
    'capture_started_at', 'capture_completed_at', 'files', 'omitted', 'truncated'];
  if (!exact(manifest, names) || manifest.schema_version !== 1 || manifest.diagnostic_payload_version !== envelope.diagnostic_payload_version
      || manifest.app_version !== envelope.app_version || manifest.bundle_id !== 'com.skyrocket.app'
      || !Array.isArray(manifest.files) || manifest.files.length !== (full ? 3 : 2)) reject('manifest_schema');
  const declared = new Set();
  for (const item of manifest.files) {
    if (!exact(item, ['name', 'size_bytes', 'sha256']) || !Object.hasOwn(caps, item.name) || item.name === 'manifest.json'
        || declared.has(item.name)) reject('manifest_members');
    declared.add(item.name);
    const data = files.get(item.name);
    if (item.size_bytes !== data.length || item.sha256 !== await digest(data)) reject('member_binding');
  }
  const state = strictJson(files.get('state.json'));
  validateState(state, envelope);
  if (manifest.run_id !== state.run_id) reject('manifest_session');
  for (const name of ['capture_started_at', 'capture_completed_at', 'omitted', 'truncated']) {
    if (JSON.stringify(manifest[name]) !== JSON.stringify(state[name])) reject('manifest_context');
  }
  if (full) {
    const exported = await validateDiagnostics(files.get('diagnostics.zip'), deepDiagnostics, envelope.diagnostic_payload_version);
    if (exported && exported.version !== envelope.app_version) reject('diagnostic_version');
    if (exported && ((envelope.diagnostic_payload_version === 3) !== (exported.diagnostic_schema_version === 3))) reject('diagnostic_payload_schema');
  }
  const description = utf8.decode(files.get('problem.txt'));
  checkDiagnosticPrivacy(description);
  if ([...description].length > 4000) reject('description_limit');
  return state;
}

let tokenCache;
const base64url = bytes => btoa(String.fromCharCode(...bytes)).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');

export async function installationToken(env, fetcher = fetch, deadline = Infinity) {
  if (tokenCache && tokenCache.installation === env.GITHUB_INSTALLATION_ID && tokenCache.until > Date.now()) return tokenCache.value;
  const pem = env.GITHUB_PRIVATE_KEY_PKCS8;
  const pemHeader = ['-----BEGIN', 'PRIVATE KEY-----'].join(' ');
  if (!pem?.startsWith(pemHeader) || !/^\d+$/.test(env.GITHUB_APP_ID)
      || !/^\d+$/.test(env.GITHUB_INSTALLATION_ID)) reject('github_app_not_configured', 503);
  const der = Uint8Array.from(atob(pem.replace(/-----[A-Z ]+-----|\s/g, '')), x => x.charCodeAt(0));
  const key = await crypto.subtle.importKey('pkcs8', der, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['sign']);
  const encoder = new TextEncoder();
  const now = Math.floor(Date.now() / 1000);
  const payload = base64url(encoder.encode(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))) + '.'
    + base64url(encoder.encode(JSON.stringify({ iat: now - 60, exp: now + 540, iss: env.GITHUB_APP_ID })));
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, encoder.encode(payload));
  const response = await fetcher(`https://api.github.com/app/installations/${env.GITHUB_INSTALLATION_ID}/access_tokens`, {
    method: 'POST', redirect: 'manual', signal: AbortSignal.timeout(Math.max(1, Math.min(30000, deadline - Date.now()))),
    headers: { Authorization: `Bearer ${payload}.${base64url(new Uint8Array(signature))}`,
      Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Skyrocket-Feedback/1' },
    body: JSON.stringify({ repositories: ['Skyrocket-Private-Feedback'], permissions: { contents: 'write' } }),
  });
  if (response.status !== 201) reject('github_app_auth_failed', 503);
  const result = JSON.parse(utf8.decode(await readBounded(response, 64 * 1024)));
  if (typeof result.token !== 'string' || !Number.isFinite(Date.parse(result.expires_at))
      || result.permissions?.contents !== 'write' || result.repository_selection !== 'selected'
      || !Array.isArray(result.repositories) || result.repositories.length !== 1
      || result.repositories[0].full_name !== REPOSITORY || result.repositories[0].private !== true
      || Object.keys(result.permissions).some(name => !['contents', 'metadata'].includes(name))) reject('github_app_token_invalid', 503);
  tokenCache = { installation: env.GITHUB_INSTALLATION_ID, value: result.token, until: Date.parse(result.expires_at) - 60000 };
  return result.token;
}

export class GitHubStore {
  constructor(token, releaseId, fetcher = fetch, deadline = Infinity) {
    this.token = token; this.releaseId = releaseId; this.fetcher = fetcher.bind(globalThis); this.deadline = deadline;
  }
  // 每次上游调用的超时 = min(30 秒, 距总时限还剩多少)；总时限已过就不再发请求。
  budget() {
    const left = this.deadline - Date.now();
    if (left <= 0) reject('upstream_timeout', 503);
    return Math.min(30000, left);
  }
  async request(path, { method = 'GET', body, binary = false, upload = false, maximum = 2 * 1024 * 1024 } = {}) {
    const host = upload ? 'uploads.github.com' : 'api.github.com';
    let response = await this.fetcher(`https://${host}/repos/${REPOSITORY}${path}`, {
      method, body, redirect: 'manual', signal: AbortSignal.timeout(this.budget()),
      headers: { Authorization: `Bearer ${this.token}`, 'User-Agent': 'Skyrocket-Feedback/1',
        Accept: binary ? 'application/octet-stream' : 'application/vnd.github+json',
        'Content-Type': upload ? 'application/octet-stream' : 'application/json', 'X-GitHub-Api-Version': '2022-11-28' },
    });
    if (binary && [301, 302, 307].includes(response.status)) {
      const target = new URL(response.headers.get('Location'));
      if (target.protocol !== 'https:' || target.hostname !== 'release-assets.githubusercontent.com' || target.username || target.password) reject('asset_redirect', 503);
      response = await this.fetcher(target, { redirect: 'manual', signal: AbortSignal.timeout(this.budget()) });
    }
    if (!response.ok) reject(`github_http_${response.status}`, 503);
    const bytes = await readBounded(response, maximum);
    return binary ? bytes : JSON.parse(utf8.decode(bytes));
  }
  async publish(envelope, archive) {
    const repo = await this.request('');
    if (repo.private !== true || repo.full_name !== REPOSITORY) reject('repository_not_private', 503);
    const release = await this.request(`/releases/${this.releaseId}`);
    if (release.tag_name !== INBOX_TAG) reject('inbox_identity', 503);
    const assets = new Map();
    for (let page = 1; page <= 22; page++) {
      const rows = await this.request(`/releases/${this.releaseId}/assets?per_page=100&page=${page}`);
      for (const row of rows) { if (assets.has(row.name)) reject('duplicate_assets', 503); assets.set(row.name, row); }
      if (rows.length < 100) break;
      if (page === 22) reject('inbox_capacity', 503);
    }
    const caseId = `feedback-${envelope.idempotency_key}`;
    let zip = assets.get(caseId + '.zip');
    let receiptAsset = assets.get(caseId + '.receipt.json');
    if (!zip && receiptAsset) reject('storage_missing', 503);
    const neededAssets = Number(!zip) + Number(!receiptAsset);
    if (assets.size + neededAssets > MAX_RELEASE_ASSETS) reject('inbox_asset_capacity', 429);
    if (!zip) {
      if ([...assets.keys()].filter(x => x.endsWith('.zip')).length >= 1000) reject('inbox_capacity', 503);
      zip = await this.request(`/releases/${this.releaseId}/assets?name=${caseId}.zip`, { method: 'POST', body: archive, upload: true });
    }
    const stored = await this.request(`/releases/assets/${zip.id}`, { binary: true, maximum: MAX_UPLOAD });
    if (stored.length !== archive.length || await digest(stored) !== envelope.archive_sha256) reject('remote_binding', 409);
    if (!receiptAsset) {
      const receipt = { case_id: caseId, archive_sha256: envelope.archive_sha256, size_bytes: archive.length,
        received_at: new Date().toISOString(), storage_status: 'stored', validation_status: 'validated',
        retention_until: null, retention_policy: 'manual_deletion', repository: REPOSITORY,
        asset_id: zip.id, consent_policy_version: envelope.consent_policy_version };
      if (envelope.diagnostic_payload_version >= 2) receipt.validation_scope = 'container_sha256_and_member_catalog';
      receiptAsset = await this.request(`/releases/${this.releaseId}/assets?name=${caseId}.receipt.json`, {
        method: 'POST', body: new TextEncoder().encode(JSON.stringify(receipt)), upload: true });
    }
    const receipt = strictJson(await this.request(`/releases/assets/${receiptAsset.id}`, { binary: true, maximum: 64 * 1024 }));
    // The first deployed v3 receiver stored an otherwise bound receipt without this
    // field. Preserve idempotency for that already-stored package: all data needed to
    // derive the scope is immutable and has just been re-read from the authoritative ZIP.
    if (envelope.diagnostic_payload_version >= 2 && receipt.validation_scope === undefined) {
      receipt.validation_scope = 'container_sha256_and_member_catalog';
    }
    if (receipt.case_id !== caseId || receipt.archive_sha256 !== envelope.archive_sha256 || receipt.size_bytes !== archive.length
        || receipt.asset_id !== zip.id || receipt.repository !== REPOSITORY || receipt.storage_status !== 'stored'
        || receipt.validation_status !== 'validated' || receipt.consent_policy_version !== envelope.consent_policy_version) reject('receipt_binding', 503);
    if (envelope.diagnostic_payload_version >= 2 && receipt.validation_scope !== 'container_sha256_and_member_catalog') reject('receipt_validation_scope', 503);
    return receipt;
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const configured = env.ENABLED === 'true' && env.DB && env.GITHUB_APP_ID && env.GITHUB_INSTALLATION_ID
      && env.GITHUB_PRIVATE_KEY_PKCS8 && /^\d+$/.test(env.GITHUB_RELEASE_ID);
    const json = (data, status = 200) => new Response(JSON.stringify(data), { status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
    if (request.method === 'GET' && url.pathname === '/health') return json({ ready: Boolean(configured), protocol: 1,
      payloadVersions: [1, 2, 3], maxUploadBytes: MAX_UPLOAD, freeOnly: true });
    if (request.method !== 'POST' || url.pathname !== '/v1/feedback' || url.search) return json({ error: 'not_found' }, 404);
    if (!configured) return json({ error: 'receiver_not_ready' }, 503);
    let phase = 'headers';
    let reservedBytes = 0;
    const started = Date.now();
    const day = new Date().toISOString().slice(0, 10);
    // 记下每一笔已经扣掉的额度：没走到 GitHub 就被拒的请求要把全局额度退回去，
    // 否则一批垃圾请求（或一次白名单漏洞导致的反复重试）就能把当天的额度烧光。
    const charged = { request: false, bytes: 0, clientKey: '' };
    let reachedGitHub = false;
    // 全局额度：没走到 GitHub 就退（有每日上限）。来源额度：只在服务端自己出错（5xx）时退，
    // 否则 GitHub 故障期间用户的正常重试会把自己当天的 30 次烧光，界面还说“明天再来”，盖住真正的故障。
    const refund = async serverFault => {
      try {
        if (!reachedGitHub && (charged.request || charged.bytes)) {
          const room = await env.DB.prepare('INSERT INTO budget(day, requests) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET requests=requests+1 WHERE requests<? RETURNING requests')
            .bind(day + ':refunds', REFUND_DAILY_LIMIT).first();
          if (room) {
            if (charged.request) await env.DB.prepare('UPDATE budget SET requests=MAX(requests-1,0) WHERE day=?').bind(day).run();
            if (charged.bytes) await env.DB.prepare('UPDATE budget SET requests=MAX(requests-?,0) WHERE day=?').bind(charged.bytes, day + ':bytes').run();
          }
        }
        if (serverFault && charged.clientKey) {
          await env.DB.prepare('UPDATE budget SET requests=MAX(requests-1,0) WHERE day=?').bind(charged.clientKey).run();
        }
      } catch (refundError) {
        console.error(JSON.stringify({ event: 'feedback_refund_failed', phase }));
      }
    };
    try {
      const size = Number(request.headers.get('Content-Length'));
      if (!Number.isSafeInteger(size) || size <= 4 || size > MAX_UPLOAD + MAX_HEADER + 4
          || request.headers.has('Transfer-Encoding')
          || request.headers.get('Content-Type') !== 'application/vnd.skyrocket.feedback-v1') reject('request_headers', 400);
      if (activeBodyBytes + size > MAX_UPLOAD + MAX_HEADER + 4) reject('receiver_busy', 429);
      activeBodyBytes += size;
      reservedBytes = size;
      phase = 'budget';
      // 先按来源限流：单个来源把自己的份额用完只影响它自己，不会拖垮全局额度。
      // 只存“日期+来源”的哈希，不落真实地址；日期进了哈希，次日自然换桶。
      const source = await digest(new TextEncoder().encode(day + '|' + (request.headers.get('CF-Connecting-IP') || 'unknown')));
      const client = await env.DB.prepare('INSERT INTO budget(day, requests) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET requests=requests+1 WHERE requests<? RETURNING requests')
        .bind(day + ':client:' + source.slice(0, 24), CLIENT_DAILY_LIMIT).first();
      if (!client) reject('daily_client_budget', 429);
      charged.clientKey = day + ':client:' + source.slice(0, 24);
      const allowed = await env.DB.prepare('INSERT INTO budget(day, requests) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET requests=requests+1 WHERE requests<100 RETURNING requests').bind(day).first();
      if (!allowed) reject('daily_budget', 429);
      charged.request = true;
      // 每天只清一次一周前的额度行（来源桶让这张表每天都在长）。判据不能用“今天第一笔额度”：
      // 垃圾请求会把额度退回去，计数反复回到 1，每个请求都会触发一次全表扫描删除。
      // 用一行当天的标记：只有真正插入成功的那一次才返回一行，退款碰不到它。
      const firstOfDay = await env.DB.prepare('INSERT INTO budget(day, requests) VALUES (?, 1) ON CONFLICT(day) DO NOTHING RETURNING requests')
        .bind(day + ':sweep').first();
      if (firstOfDay) {
        const cutoff = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);
        await env.DB.prepare('DELETE FROM budget WHERE substr(day, 1, 10) < ?').bind(cutoff).run();
      }
      phase = 'body';
      const bytes = await readBounded(request, MAX_UPLOAD + MAX_HEADER + 4);
      if (bytes.length !== size) reject('request_size');
      phase = 'byte_budget';
      const byteBudget = await env.DB.prepare('INSERT INTO budget(day, requests) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET requests=requests+excluded.requests WHERE requests+excluded.requests<=268435456 RETURNING requests')
        .bind(day + ':bytes', bytes.length).first();
      if (!byteBudget) reject('daily_byte_budget', 429);
      charged.bytes = bytes.length;
      phase = 'unpack';
      const { envelope, archive } = await unpackRequest(bytes);
      phase = 'archive';
      await validateArchive(archive, envelope, false);
      phase = 'case';
      const total = await env.DB.prepare('SELECT COUNT(*) AS n FROM cases').bind().first();
      if (total && total.n >= CASE_PRUNE_AT) {
        // 案件表有硬上限。从未走完的行（失败、被中断、没有回执）租约一过期就没有保留价值：
        // 重传会按同一幂等键重新建行，GitHub 侧按资产名幂等、校验和不符会被 409 拦住。
        // 已收件的行是回执的唯一本地副本，永远不清。
        // 注意这只清“没走完”的行，管不到真正的容量天花板：每份反馈在发布里占 zip 和回执两个资产，
        // 发布的资产上限是 MAX_RELEASE_ASSETS(1000)，所以实际最多收约 500 份，之后只会得到
        // inbox_asset_capacity。收件资产不自动删（保留策略是人工删除），要腾位置得维护者处理。
        await env.DB.prepare("DELETE FROM cases WHERE state != 'received' AND lease_until <= ? AND idempotency_key != ?")
          .bind(Date.now(), envelope.idempotency_key).run();
      }
      await env.DB.prepare("INSERT OR IGNORE INTO cases SELECT ?, ?, 'new', 0, '', NULL WHERE (SELECT COUNT(*) FROM cases)<1000")
        .bind(envelope.idempotency_key, envelope.archive_sha256).run();
      const row = await env.DB.prepare('SELECT * FROM cases WHERE idempotency_key=?').bind(envelope.idempotency_key).first();
      if (!row) reject('case_budget', 429);
      if (row.archive_sha256 !== envelope.archive_sha256) reject('identity_conflict', 409);
      const now = Date.now();
      const lease = crypto.randomUUID();
      phase = 'lease';
      // 已经有回执的行（重放一次已收件的案件）保持“已收件”，不能被临时改成 uploading：
      // 重放中途失败会让它变成可被清理的状态，而回执的本地副本只在这一行里。
      const owned = await env.DB.prepare("UPDATE cases SET state=CASE WHEN receipt IS NOT NULL THEN state ELSE 'uploading' END, lease_until=?, lease_token=? WHERE idempotency_key=? AND lease_until<=? RETURNING idempotency_key")
        .bind(now + LEASE_MS, lease, envelope.idempotency_key, now).first();
      if (!owned) reject('submission_in_progress', 409);
      try {
        phase = 'github_auth';
        const deadline = now + PUBLISH_DEADLINE_MS;
        const token = await installationToken(env, fetch, deadline);
        // 拿到令牌才算真的用了上游：配置错误、证书导入失败这类没碰到 GitHub 的失败仍然退额度。
        reachedGitHub = true;
        phase = 'github_storage';
        const receipt = await new GitHubStore(token, env.GITHUB_RELEASE_ID, fetch, deadline).publish(envelope, archive);
        phase = 'receipt_commit';
        const saved = await env.DB.prepare("UPDATE cases SET state='received', lease_until=0, receipt=? WHERE idempotency_key=? AND lease_token=? RETURNING idempotency_key")
          .bind(JSON.stringify(receipt), envelope.idempotency_key, lease).first();
        if (!saved) reject('lease_superseded', 409);
        return json(receipt);
      } catch (error) {
        await env.DB.prepare("UPDATE cases SET lease_until=0, state=CASE WHEN receipt IS NOT NULL THEN state ELSE 'unknown' END WHERE idempotency_key=? AND lease_token=?")
          .bind(envelope.idempotency_key, lease).run();
        throw error;
      }
    } catch (error) {
      // 解包和校验阶段抛出的非 Rejection 错误（坏 zip、坏 UTF-8、坏 JSON）说明请求内容本身有问题，
      // 重试不会改变结果，应回 422；原先一律回 503，客户端会当成“服务暂时不可用”反复重试并烧额度。
      const badContent = !(error instanceof Rejection) && ['unpack', 'archive'].includes(phase);
      // 只有 GitHub 阶段的超时才算“上游超时”；客户端在读请求体时中断也会抛 AbortError，那不是上游的错。
      const timedOut = !(error instanceof Rejection) && ['github_auth', 'github_storage'].includes(phase)
        && ['TimeoutError', 'AbortError'].includes(error?.name);
      const code = error instanceof Rejection ? error.code : badContent ? 'archive_invalid'
        : timedOut ? 'upstream_timeout' : 'submission_unconfirmed';
      const status = error instanceof Rejection ? error.status : badContent ? 422 : 503;
      await refund(status >= 500 && !CLIENT_SIDE_PHASES.includes(phase));
      console.error(JSON.stringify({ event: 'feedback_unconfirmed', phase, code, status,
        elapsed_ms: Date.now() - started,
        category: error instanceof Rejection ? error.code : error instanceof TypeError ? 'type_error' : 'unexpected_error' }));
      const wait = status === 429 || status === 409 ? retryAfter(code) : null;
      const body = new Response(JSON.stringify({ error: code, phase }), { status,
        headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store',
          ...(wait ? { 'Retry-After': String(wait) } : {}) } });
      return body;
    } finally {
      activeBodyBytes -= reservedBytes;
    }
  },
};
