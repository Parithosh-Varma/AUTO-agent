"""Persistent memory. SQLite locally, same schema as Cloudflare D1 (see cloudflare/schema.sql).

Tables: state, goals, projects, actions, experiments, payments, queue.
Revenue truth comes ONLY from Dodo API verification, never from editing this DB.

Seven-day rules:
- run_started_at is written once and never reset by restarts.
- payments.origin='dodo_api' is required for verified_gross (manual DB edits never count).
- queue supports dedupe_key so restarts cannot create duplicates.
"""
import hashlib
import json
import os
import sqlite3
import time

# Agent code must never write these directly; only Supervisor/loop internals may.
PROTECTED_STATE_KEYS = frozenset({
    "run_started_at", "caps", "usage", "experiment_status", "deadline_at",
})

TERMINAL_STATUSES = frozenset({
    "completed_target", "deadline_expired", "stopped_kill_switch", "stopped_limits",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  description TEXT NOT NULL,
  novelty REAL DEFAULT 0,
  feasibility REAL DEFAULT 0,
  usefulness REAL DEFAULT 0,
  cost REAL DEFAULT 0,
  score REAL DEFAULT 0,
  status TEXT DEFAULT 'proposed',
  created_at REAL DEFAULT 0,
  updated_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER DEFAULT 0,
  name TEXT NOT NULL,
  repo TEXT DEFAULT '',
  url TEXT DEFAULT '',
  status TEXT DEFAULT 'idea',
  created_at REAL DEFAULT 0,
  updated_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle INTEGER DEFAULT 0,
  tool TEXT NOT NULL,
  op TEXT NOT NULL,
  input_json TEXT DEFAULT '{}',
  ok INTEGER DEFAULT 1,
  output_text TEXT DEFAULT '',
  created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  hypothesis TEXT DEFAULT '',
  result TEXT DEFAULT '',
  created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dodo_payment_id TEXT UNIQUE NOT NULL,
  amount REAL NOT NULL,
  currency TEXT DEFAULT 'USD',
  status TEXT DEFAULT 'unknown',
  is_test INTEGER DEFAULT 1,
  is_refunded INTEGER DEFAULT 0,
  is_self_purchase INTEGER DEFAULT 0,
  origin TEXT DEFAULT 'manual',
  raw_json TEXT DEFAULT '{}',
  verified_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT DEFAULT '{}',
  status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  dedupe_key TEXT DEFAULT NULL,
  created_at REAL DEFAULT 0
);
"""


class Store:
    def __init__(self, data_dir="./data"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(os.path.join(data_dir, "artifacts"), exist_ok=True)
        self.db_path = os.path.join(data_dir, "agent.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        # Backward-compatible migrations for DBs created before the 7-day hardening.
        cols_pay = {r[1] for r in self.conn.execute("PRAGMA table_info(payments)").fetchall()}
        if "origin" not in cols_pay:
            self.conn.execute("ALTER TABLE payments ADD COLUMN origin TEXT DEFAULT 'manual'")
        cols_q = {r[1] for r in self.conn.execute("PRAGMA table_info(queue)").fetchall()}
        if "dedupe_key" not in cols_q:
            self.conn.execute("ALTER TABLE queue ADD COLUMN dedupe_key TEXT DEFAULT NULL")
        self.conn.commit()

    # --- kv state ---
    def get_state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def set_state(self, key, value):
        if key in PROTECTED_STATE_KEYS:
            raise PermissionError(f"state key '{key}' is supervisor-protected; agent code may not write it")
        self.set_state_internal(key, value)

    def set_state_internal(self, key, value):
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    # --- goals ---
    def add_goal(self, description, novelty=0, feasibility=0, usefulness=0, cost=0, score=0):
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO goals(description,novelty,feasibility,usefulness,cost,score,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?, 'proposed',?,?)",
            (description, novelty, feasibility, usefulness, cost, score, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_goals(self, status=None):
        if status:
            rows = self.conn.execute("SELECT * FROM goals WHERE status=? ORDER BY score DESC", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM goals ORDER BY score DESC").fetchall()
        return [dict(r) for r in rows]

    def update_goal(self, goal_id, status=None, score=None):
        g = self.conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not g:
            return
        d = dict(g)
        if status:
            d["status"] = status
        if score is not None:
            d["score"] = score
        d["updated_at"] = time.time()
        self.conn.execute(
            "UPDATE goals SET status=?, score=?, updated_at=? WHERE id=?",
            (d["status"], d["score"], d["updated_at"], goal_id),
        )
        self.conn.commit()

    def active_goal(self):
        rows = self.conn.execute("SELECT * FROM goals WHERE status='active' ORDER BY id DESC LIMIT 1").fetchall()
        return dict(rows[0]) if rows else None

    # --- projects ---
    def add_project(self, goal_id, name, repo="", url="", status="idea"):
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO projects(goal_id,name,repo,url,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (goal_id, name, repo, url, status, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_project(self, pid, status=None, url=None, repo=None):
        p = self.conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            return
        d = dict(p)
        if status:
            d["status"] = status
        if url is not None:
            d["url"] = url
        if repo is not None:
            d["repo"] = repo
        d["updated_at"] = time.time()
        self.conn.execute(
            "UPDATE projects SET status=?, url=?, repo=?, updated_at=? WHERE id=?",
            (d["status"], d["url"], d["repo"], d["updated_at"], pid),
        )
        self.conn.commit()

    # --- audit ---
    def log_action(self, cycle, tool, op, input_json, ok, output_text):
        self.conn.execute(
            "INSERT INTO actions(cycle,tool,op,input_json,ok,output_text,created_at) VALUES(?,?,?,?,?,?,?)",
            (cycle, tool, op, json.dumps(input_json)[:4000], 1 if ok else 0, str(output_text)[:4000], time.time()),
        )
        self.conn.commit()

    def recent_actions(self, limit=20):
        rows = self.conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def consecutive_failures(self, limit=5):
        rows = self.conn.execute("SELECT ok FROM actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        n = 0
        for r in rows:
            if r["ok"] == 0:
                n += 1
            else:
                break
        return n

    # --- queue (local equiv of Cloudflare Queues; idempotent) ---
    def enqueue(self, kind, payload, dedupe_key=None):
        if dedupe_key is None and isinstance(payload, dict) and payload:
            # stable default dedupe so identical retries do not duplicate
            raw = kind + ":" + json.dumps(payload, sort_keys=True)
            dedupe_key = hashlib.sha256(raw.encode()).hexdigest()[:32]
        if dedupe_key:
            existing = self.conn.execute(
                "SELECT id FROM queue WHERE dedupe_key=? AND status IN ('pending','claimed') LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            if existing:
                return existing["id"]
        cur = self.conn.execute(
            "INSERT INTO queue(kind,payload,status,attempts,dedupe_key,created_at)"
            " VALUES(?,?, 'pending',0,?,?)",
            (kind, json.dumps(payload), dedupe_key, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def dequeue(self):
        row = self.conn.execute(
            "SELECT * FROM queue WHERE status='pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self.conn.execute("UPDATE queue SET status='claimed', attempts=attempts+1 WHERE id=?", (row["id"],))
        self.conn.commit()
        d = dict(row)
        d["payload"] = json.loads(d["payload"] or "{}")
        return d

    def requeue_stuck(self, max_attempts=5):
        """Crash recovery: claimed jobs left by a dead process go back to pending.

        Returns number of jobs requeued. Jobs past max_attempts are marked failed.
        """
        stuck = self.conn.execute(
            "SELECT id, attempts FROM queue WHERE status='claimed'"
        ).fetchall()
        n = 0
        for row in stuck:
            if row["attempts"] >= max_attempts:
                self.conn.execute("UPDATE queue SET status='failed' WHERE id=?", (row["id"],))
            else:
                self.conn.execute("UPDATE queue SET status='pending' WHERE id=?", (row["id"],))
                n += 1
        self.conn.commit()
        return n

    def pending_count(self):
        row = self.conn.execute("SELECT COUNT(*) AS c FROM queue WHERE status='pending'").fetchone()
        return int(row["c"] or 0)

    def complete_job(self, job_id, ok=True):
        self.conn.execute(
            "UPDATE queue SET status=? WHERE id=?", ("done" if ok else "failed", job_id)
        )
        self.conn.commit()

    def total_failures(self):
        row = self.conn.execute("SELECT COUNT(*) AS c FROM actions WHERE ok=0").fetchone()
        return int(row["c"] or 0)

    # --- payments (mirror of verified Dodo records) ---
    def upsert_payment(self, dodo_payment_id, amount, currency="USD", status="unknown",
                       is_test=1, is_refunded=0, is_self_purchase=0, raw=None,
                       origin="manual"):
        if origin not in ("dodo_api", "manual"):
            origin = "manual"
        self.conn.execute(
            "INSERT INTO payments(dodo_payment_id,amount,currency,status,is_test,is_refunded,"
            " is_self_purchase,origin,raw_json,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(dodo_payment_id) DO UPDATE SET amount=excluded.amount, status=excluded.status,"
            " is_test=excluded.is_test, is_refunded=excluded.is_refunded,"
            " is_self_purchase=excluded.is_self_purchase, origin=excluded.origin,"
            " raw_json=excluded.raw_json, verified_at=excluded.verified_at",
            (dodo_payment_id, amount, currency, status, is_test, is_refunded,
             is_self_purchase, origin, json.dumps(raw or {})[:4000], time.time()),
        )
        self.conn.commit()

    def verified_gross(self):
        # Authenticated provider revenue only: origin must be dodo_api, USD,
        # succeeded, live, non-refunded/disputed, non-self-purchase.
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM payments"
            " WHERE origin='dodo_api' AND status='succeeded' AND currency='USD'"
            " AND is_test=0 AND is_refunded=0 AND is_self_purchase=0"
        ).fetchone()
        return float(row["total"] or 0.0)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
