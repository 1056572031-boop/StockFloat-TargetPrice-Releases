CREATE TABLE IF NOT EXISTS deleted_cases (
  idempotency_key TEXT PRIMARY KEY,
  deleted_at INTEGER NOT NULL
);
