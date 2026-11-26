CREATE TABLE IF NOT EXISTS budget (
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

CREATE INDEX IF NOT EXISTS retention_due ON retention(retention_until);

CREATE TABLE IF NOT EXISTS retention_intents (
  idempotency_key TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  archive_sha256 TEXT NOT NULL,
  archive_size INTEGER NOT NULL,
  consent_policy_version TEXT NOT NULL,
  diagnostic_payload_version INTEGER NOT NULL,
  zip_name TEXT NOT NULL,
  receipt_name TEXT NOT NULL,
  policy TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  publish_deadline INTEGER NOT NULL,
  state TEXT NOT NULL,
  zip_asset_id INTEGER,
  receipt_asset_id INTEGER,
  retention_until INTEGER,
  reconcile_lease_until INTEGER NOT NULL DEFAULT 0,
  reconcile_lease_token TEXT NOT NULL DEFAULT '',
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS retention_intents_state_lease ON retention_intents(state, reconcile_lease_until);
CREATE INDEX IF NOT EXISTS retention_intents_due ON retention_intents(retention_until);

CREATE TABLE IF NOT EXISTS retention_queue_cursor (
  name TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  idempotency_key TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0
);
