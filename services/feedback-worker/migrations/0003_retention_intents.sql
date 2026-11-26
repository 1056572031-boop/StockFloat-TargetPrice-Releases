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
