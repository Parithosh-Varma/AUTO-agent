-- 0001_observability.sql — D1 migration for the AUTO-agent observability dashboard.
-- Apply: wrangler d1 migrations apply auto_memory
-- (or: wrangler d1 execute auto_memory --file=cloudflare/migrations/0001_observability.sql)
-- Idempotent: safe to run on fresh or existing databases. History is preserved;
-- duplicates collapse on idempotency keys; rows are never updated in place
-- (except terminal-state transitions and approval decisions).
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT UNIQUE NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL,
  ts REAL NOT NULL,
  severity TEXT DEFAULT 'info',
  category TEXT NOT NULL,
  goal_id INTEGER DEFAULT 0,
  task_id INTEGER DEFAULT 0,
  summary TEXT NOT NULL,
  details_json TEXT DEFAULT '{}',
  cycle INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_events_goal ON events(goal_id);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER DEFAULT 0,
  title TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  evidence TEXT DEFAULT '',
  created_at REAL DEFAULT 0,
  updated_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle INTEGER DEFAULT 0,
  usage_json TEXT DEFAULT '{}',
  created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payment_recon (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gross REAL DEFAULT 0,
  refunded REAL DEFAULT 0,
  fee_estimate REAL DEFAULT 0,
  profit_estimate REAL DEFAULT 0,
  source TEXT DEFAULT 'dodo_api',
  basis TEXT DEFAULT 'provider',
  created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  context_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'pending',
  created_by TEXT DEFAULT 'agent',
  decided_by TEXT DEFAULT '',
  created_at REAL DEFAULT 0,
  decided_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  command TEXT NOT NULL,
  ok INTEGER DEFAULT 1,
  note TEXT DEFAULT '',
  created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(created_at);
-- Operator control intents queued by the dashboard/worker, drained by the runner.
-- The runner applies them exclusively via Supervisor (pause/resume/stop).
CREATE TABLE IF NOT EXISTS control_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  actor TEXT DEFAULT 'dashboard',
  status TEXT DEFAULT 'pending',
  created_at REAL DEFAULT 0
);
