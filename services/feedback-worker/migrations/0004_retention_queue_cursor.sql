CREATE TABLE IF NOT EXISTS retention_queue_cursor (
  name TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  idempotency_key TEXT NOT NULL
);
