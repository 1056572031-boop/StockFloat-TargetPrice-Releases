CREATE TABLE IF NOT EXISTS download_events (
  id TEXT PRIMARY KEY,
  occurred_day TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  route TEXT NOT NULL,
  version TEXT NOT NULL,
  asset TEXT NOT NULL,
  visitor_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS download_events_day_idx ON download_events (occurred_day);
CREATE INDEX IF NOT EXISTS download_events_expires_idx ON download_events (expires_at);
CREATE INDEX IF NOT EXISTS download_events_route_day_idx ON download_events (route, occurred_day);
