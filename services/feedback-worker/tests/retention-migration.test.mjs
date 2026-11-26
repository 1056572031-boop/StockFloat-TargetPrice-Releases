import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const sql = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const columns = (db, table) => db.prepare(`PRAGMA table_info(${table})`).all().map(
  ({ name, type, notnull, dflt_value, pk }) => ({ name, type, notnull, dflt_value, pk }),
);
const indexes = (db, table) => db.prepare(`PRAGMA index_list(${table})`).all()
  .filter((index) => index.origin === 'c')
  .map(({ name }) => db.prepare(`PRAGMA index_info(${name})`).all().map(({ name: column }) => column))
  .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));

const requiredRetentionColumns = [
  'retention_until', 'zip_asset_id', 'receipt_asset_id', 'zip_deleted', 'receipt_deleted',
  'cleanup_lease_until', 'cleanup_lease_token',
];

function assertRetentionNotNull(db) {
  const actual = columns(db, 'retention').filter((column) => column.notnull).map((column) => column.name);
  assert.deepEqual(actual, requiredRetentionColumns);
}

function schemaObjects(db) {
  return db.prepare("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")
    .all().map(({ type, name, tbl_name, sql: definition }) => ({
      type, name, tbl_name,
      sql: definition?.match(/'(?:''|[^'])*'|"(?:""|[^"])*"|[A-Za-z_][\w]*|\d+|[^\s]/g)?.join(' '),
    }));
}

function assertSchemaMatches(actual, expected) {
  assert.deepEqual(schemaObjects(actual), schemaObjects(expected));
}

const intentColumns = [
  ['idempotency_key', 'TEXT', 0, null, 1],
  ['case_id', 'TEXT', 1, null, 0],
  ['archive_sha256', 'TEXT', 1, null, 0],
  ['archive_size', 'INTEGER', 1, null, 0],
  ['consent_policy_version', 'TEXT', 1, null, 0],
  ['diagnostic_payload_version', 'INTEGER', 1, null, 0],
  ['zip_name', 'TEXT', 1, null, 0],
  ['receipt_name', 'TEXT', 1, null, 0],
  ['policy', 'TEXT', 1, null, 0],
  ['created_at', 'INTEGER', 1, null, 0],
  ['publish_deadline', 'INTEGER', 1, null, 0],
  ['state', 'TEXT', 1, null, 0],
  ['zip_asset_id', 'INTEGER', 0, null, 0],
  ['receipt_asset_id', 'INTEGER', 0, null, 0],
  ['retention_until', 'INTEGER', 0, null, 0],
  ['reconcile_lease_until', 'INTEGER', 1, '0', 0],
  ['reconcile_lease_token', 'TEXT', 1, "''", 0],
  ['last_error', 'TEXT', 0, null, 0],
].map(([name, type, notnull, dflt_value, pk]) => ({ name, type, notnull, dflt_value, pk }));

function legacyDatabase() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE budget (day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE cases (idempotency_key TEXT PRIMARY KEY, archive_sha256 TEXT NOT NULL,
      state TEXT NOT NULL, lease_until INTEGER NOT NULL, lease_token TEXT NOT NULL, receipt TEXT);`);
  db.exec(sql('../migrations/0001_retention.sql'));
  db.exec(sql('../migrations/0002_deleted_cases.sql'));
  return db;
}

function assertIntentShape(db) {
  assert.deepEqual(columns(db, 'retention_intents'), intentColumns);
  const indexColumns = indexes(db, 'retention_intents');
  assert.ok(indexColumns.some((cols) => cols[0] === 'state' && cols[1] === 'reconcile_lease_until'));
  assert.ok(indexColumns.some((cols) => cols[0] === 'retention_until'));
}

test('migration adds intents without changing legacy schema or data', () => {
  const db = legacyDatabase();
  try {
    db.exec(`INSERT INTO cases VALUES ('key', 'hash', 'done', 0, '', NULL);
      INSERT INTO retention (idempotency_key, retention_until, zip_asset_id, receipt_asset_id)
      VALUES ('key', 123, 11, 12);
      INSERT INTO deleted_cases VALUES ('deleted', 456);`);
    const before = db.prepare("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").all();
    db.exec(sql('../migrations/0003_retention_intents.sql'));
    db.exec(sql('../migrations/0003_retention_intents.sql'));
    assertIntentShape(db);
    const after = db.prepare("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND tbl_name != 'retention_intents' ORDER BY type, name").all();
    assert.deepEqual(after, before);
    assert.deepEqual({ ...db.prepare('SELECT * FROM retention').get() }, {
      idempotency_key: 'key', retention_until: 123, zip_asset_id: 11, receipt_asset_id: 12,
      zip_deleted: 0, receipt_deleted: 0, cleanup_error: null, cleanup_lease_until: 0, cleanup_lease_token: '',
    });
    assert.deepEqual({ ...db.prepare('SELECT * FROM deleted_cases').get() }, { idempotency_key: 'deleted', deleted_at: 456 });
  } finally {
    db.close();
  }
});

test('queue cursor migration upgrades an existing intent database without losing data', () => {
  const db = legacyDatabase();
  try {
    db.exec(sql('../migrations/0003_retention_intents.sql'));
    db.exec(`INSERT INTO cases VALUES ('key', 'hash', 'unknown', 0, '', NULL);
      INSERT INTO retention_intents (idempotency_key,case_id,archive_sha256,archive_size,consent_policy_version,diagnostic_payload_version,zip_name,receipt_name,policy,created_at,publish_deadline,state,last_error)
      VALUES ('key','case','hash',1,'policy',1,'zip','receipt','auto_delete',1,2,'unresolved','remote_pair_incomplete');`);
    const before = db.prepare('SELECT * FROM retention_intents').get();
    db.exec(sql('../migrations/0004_retention_queue_cursor.sql'));
    db.exec(sql('../migrations/0004_retention_queue_cursor.sql'));
    assert.deepEqual(db.prepare('SELECT * FROM retention_intents').get(), before);
    db.prepare('INSERT INTO retention_queue_cursor VALUES (?,?,?)').run('reconcile', 1, 'key');
    assert.equal(db.prepare('SELECT idempotency_key FROM retention_queue_cursor').get().idempotency_key, 'key');
    db.exec(sql('../migrations/0005_retention_cursor_version.sql'));
    assert.deepEqual({ ...db.prepare('SELECT * FROM retention_queue_cursor').get() },
      { name: 'reconcile', created_at: 1, idempotency_key: 'key', version: 0 });
    assert.deepEqual(db.prepare('SELECT * FROM retention_intents').get(), before);
  } finally { db.close(); }
});

test('schema comparison rejects missing or changed legacy objects', () => {
  const migrated = legacyDatabase();
  const fresh = new DatabaseSync(':memory:');
  try {
    migrated.exec(sql('../migrations/0003_retention_intents.sql'));
    migrated.exec(sql('../migrations/0004_retention_queue_cursor.sql'));
    migrated.exec(sql('../migrations/0005_retention_cursor_version.sql'));
    fresh.exec(sql('../schema.sql'));
    assert.doesNotThrow(() => assertSchemaMatches(fresh, migrated));
    fresh.exec('DROP INDEX retention_due');
    assert.throws(() => assertSchemaMatches(fresh, migrated), assert.AssertionError);
    fresh.exec('CREATE INDEX retention_due ON retention(zip_asset_id)');
    assert.throws(() => assertSchemaMatches(fresh, migrated), assert.AssertionError);
    fresh.exec('DROP INDEX retention_due; CREATE INDEX retention_due ON retention(retention_until)');
    fresh.exec('ALTER TABLE cases ADD COLUMN unexpected TEXT');
    assert.throws(() => assertSchemaMatches(fresh, migrated), assert.AssertionError);
    fresh.exec('CREATE TABLE unexpected (id INTEGER)');
    assert.throws(() => assertSchemaMatches(fresh, migrated), assert.AssertionError);
  } finally {
    migrated.close();
    fresh.close();
  }
});

test('retention contract rejects removal of each legacy NOT NULL constraint', () => {
  const required = ['retention_until', 'zip_asset_id', 'receipt_asset_id', 'zip_deleted',
    'receipt_deleted', 'cleanup_lease_until', 'cleanup_lease_token'];
  const migration = sql('../migrations/0001_retention.sql');
  for (const name of required) {
    const db = new DatabaseSync(':memory:');
    try {
      const pattern = new RegExp(`(${name}\\s+\\w+) NOT NULL`);
      assert.match(migration, pattern, name);
      db.exec(migration.replace(pattern, '$1'));
      assert.throws(() => assertRetentionNotNull(db), assert.AssertionError, name);
    } finally {
      db.close();
    }
  }
});

test('fresh schema matches migrated intent shape and preserves required retention columns', () => {
  const migrated = legacyDatabase();
  const fresh = new DatabaseSync(':memory:');
  try {
    fresh.exec(sql('../schema.sql'));
    assertIntentShape(fresh);
    migrated.exec(sql('../migrations/0003_retention_intents.sql'));
    migrated.exec(sql('../migrations/0004_retention_queue_cursor.sql'));
    migrated.exec(sql('../migrations/0005_retention_cursor_version.sql'));
    assert.deepEqual(columns(fresh, 'retention_intents'), columns(migrated, 'retention_intents'));
    assert.deepEqual(indexes(fresh, 'retention_intents'), indexes(migrated, 'retention_intents'));
    assertSchemaMatches(fresh, migrated);
    for (const db of [migrated, fresh]) {
      assertRetentionNotNull(db);
    }
    assert.deepEqual(columns(fresh, 'retention'), columns(migrated, 'retention'));
    assert.deepEqual(columns(fresh, 'deleted_cases'), columns(migrated, 'deleted_cases'));
  } finally {
    migrated.close();
    fresh.close();
  }
});
