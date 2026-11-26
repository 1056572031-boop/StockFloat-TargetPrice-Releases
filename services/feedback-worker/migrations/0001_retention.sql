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

CREATE INDEX IF NOT EXISTS retention_due ON retention(retention_until);
