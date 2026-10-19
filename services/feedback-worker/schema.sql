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
