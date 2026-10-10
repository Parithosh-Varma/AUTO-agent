-- D1 schema (mirrors memory.py SCHEMA). Apply: wrangler d1 execute auto_memory --file=cloudflare/schema.sql
-- For upgrades prefer: wrangler d1 migrations apply auto_memory (see migrations/)
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS goals (id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT NOT NULL, novelty REAL DEFAULT 0, feasibility REAL DEFAULT 0, usefulness REAL DEFAULT 0, cost REAL DEFAULT 0, score REAL DEFAULT 0, status TEXT DEFAULT 'proposed', created_at REAL DEFAULT 0, updated_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER DEFAULT 0, name TEXT NOT NULL, repo TEXT DEFAULT '', url TEXT DEFAULT '', status TEXT DEFAULT 'idea', created_at REAL DEFAULT 0, updated_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle INTEGER DEFAULT 0, tool TEXT NOT NULL, op TEXT NOT NULL, input_json TEXT DEFAULT '{}', ok INTEGER DEFAULT 1, output_text TEXT DEFAULT '', created_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS experiments (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, hypothesis TEXT DEFAULT '', result TEXT DEFAULT '', created_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, dodo_payment_id TEXT UNIQUE NOT NULL, amount REAL NOT NULL, currency TEXT DEFAULT 'USD', status TEXT DEFAULT 'unknown', is_test INTEGER DEFAULT 1, is_refunded INTEGER DEFAULT 0, is_self_purchase INTEGER DEFAULT 0, origin TEXT DEFAULT 'manual', raw_json TEXT DEFAULT '{}', verified_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT DEFAULT '{}', status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, dedupe_key TEXT DEFAULT NULL, created_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT UNIQUE NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, next_retry_at REAL DEFAULT 0, created_at REAL DEFAULT 0, sent_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, ts REAL NOT NULL, severity TEXT DEFAULT 'info', category TEXT NOT NULL, goal_id INTEGER DEFAULT 0, task_id INTEGER DEFAULT 0, summary TEXT NOT NULL, details_json TEXT DEFAULT '{}', cycle INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_events_goal ON events(goal_id);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER DEFAULT 0, title TEXT NOT NULL, status TEXT DEFAULT 'pending', evidence TEXT DEFAULT '', created_at REAL DEFAULT 0, updated_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS usage_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle INTEGER DEFAULT 0, usage_json TEXT DEFAULT '{}', created_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS payment_recon (id INTEGER PRIMARY KEY AUTOINCREMENT, gross REAL DEFAULT 0, refunded REAL DEFAULT 0, fee_estimate REAL DEFAULT 0, profit_estimate REAL DEFAULT 0, source TEXT DEFAULT 'dodo_api', basis TEXT DEFAULT 'provider', created_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, context_json TEXT DEFAULT '{}', status TEXT DEFAULT 'pending', created_by TEXT DEFAULT 'agent', decided_by TEXT DEFAULT '', created_at REAL DEFAULT 0, decided_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, command TEXT NOT NULL, ok INTEGER DEFAULT 1, note TEXT DEFAULT '', created_at REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(created_at);
CREATE TABLE IF NOT EXISTS control_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, actor TEXT DEFAULT 'dashboard', status TEXT DEFAULT 'pending', created_at REAL DEFAULT 0);
