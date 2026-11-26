import test from 'node:test';
import assert from 'node:assert/strict';
import retentionWorker from '../src/retention-worker.mjs';

const key = 'a'.repeat(32);

test('scheduled retention worker starts a bounded prune without a fetch request', async () => {
  const pending = [];
  const env = {
    DB: { prepare() { return { bind() { return { async all() { return { results: [] }; } }; } }; } },
    GITHUB_APP_ID: '1',
    GITHUB_INSTALLATION_ID: '2',
    GITHUB_RELEASE_ID: '3',
    GITHUB_PRIVATE_KEY_PKCS8: ['-----BEGIN', 'PRIVATE KEY-----\nAA==\n-----END', 'PRIVATE KEY-----'].join(' '),
  };
  const ctx = { waitUntil(value) { pending.push(value); } };
  await retentionWorker.scheduled({}, env, ctx);
  assert.equal(pending.length, 1);
  await assert.rejects(pending[0]);
});

test('scheduled retention worker refuses incomplete configuration before starting work', async () => {
  const pending = [];
  await retentionWorker.scheduled({}, { DB: null }, { waitUntil(value) { pending.push(value); } });
  assert.equal(pending.length, 0);
});

test('retention cron runs daily at a stable UTC time', async () => {
  const config = JSON.parse(await import('node:fs/promises').then(({ readFile }) => readFile(new URL('../wrangler.retention.jsonc', import.meta.url), 'utf8')));
  assert.deepEqual(config.triggers.crons, ['17 3 * * *']);
});
