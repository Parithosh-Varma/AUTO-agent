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
import threading
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
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key TEXT UNIQUE NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  next_retry_at REAL DEFAULT 0,
  created_at REAL DEFAULT 0,
  sent_at REAL DEFAULT 0
);
-- Observability: append-only event stream (D1-compatible). History is preserved;
-- duplicates collapse on idempotency_key, rows are never updated in place.
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
"""


class _LockedConn:
    """Serialize cross-thread SQLite access (dashboard server threads).

    The agent loop stays single-threaded; the lock only engages when the
    local dashboard API serves requests from worker threads.
    """
    def __init__(self, conn):
        self._conn = conn
        self._lock = threading.RLock()

    def __getattr__(self, name):
        attr = getattr(self._conn, name)
        if callable(attr):
            def wrapper(*a, **k):
                with self._lock:
                    return attr(*a, **k)
            return wrapper
        return attr


class Store:
    def __init__(self, data_dir="./data"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(os.path.join(data_dir, "artifacts"), exist_ok=True)
        self.db_path = os.path.join(data_dir, "agent.db")
        raw = sqlite3.connect(self.db_path, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        self.conn = _LockedConn(raw)
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

    # --- notifications outbox (Telegram; delivery confirmed only by API) ---
    def queue_notification(self, dedupe_key, event_type, message):
        now = time.time()
        try:
            cur = self.conn.execute(
                "INSERT INTO notifications(dedupe_key,event_type,message,status,attempts,"
                " next_retry_at,created_at,sent_at) VALUES(?,?,?, 'pending',0,?, ?,0)",
                (dedupe_key, event_type, message, now, now),
            )
            self.conn.commit()
            return cur.lastrowid, True
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "SELECT id, status FROM notifications WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            return (row["id"], False) if row else (None, False)

    def due_notifications(self, now=None, limit=10):
        now = time.time() if now is None else now
        rows = self.conn.execute(
            "SELECT * FROM notifications WHERE status IN ('pending','failed')"
            " AND next_retry_at<=? ORDER BY id ASC LIMIT ?", (now, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_notification_sent(self, nid):
        self.conn.execute(
            "UPDATE notifications SET status='sent', sent_at=? WHERE id=?", (time.time(), nid))
        self.conn.commit()

    def mark_notification_failed(self, nid, attempts, next_retry_at):
        self.conn.execute(
            "UPDATE notifications SET status='failed', attempts=?, next_retry_at=? WHERE id=?",
            (attempts, next_retry_at, nid))
        self.conn.commit()

    def notification_counts(self):
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM notifications GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    # --- observability events (append-only; idempotent; redacted) ---
    def record_event(self, category, summary, severity="info", goal_id=0, task_id=0,
                     details=None, cycle=0, idempotency_key=None):
        try:
            from dashboard.redact import scrub, scrub_dict
            summary = scrub(summary)
            details = scrub_dict(details or {})
        except Exception:
            details = details or {}
        now = time.time()
        if severity not in ("info", "warn", "error"):
            severity = "info"
        if idempotency_key is None:
            raw = f"{cycle}:{category}:{goal_id}:{task_id}:{summary}"
            idempotency_key = hashlib.sha256(raw.encode()).hexdigest()[:32]
        event_id = f"ev_{int(now * 1000)}_{idempotency_key[:8]}"
        try:
            cur = self.conn.execute(
                "INSERT INTO events(event_id,idempotency_key,ts,severity,category,goal_id,"
                " task_id,summary,details_json,cycle) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, idempotency_key, now, severity, category, goal_id, task_id,
                 summary[:500], json.dumps(details)[:4000], cycle),
            )
            self.conn.commit()
            return cur.lastrowid, True
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "SELECT id FROM events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                return row["id"], False
            # event_id collision (same ms): retry with unique suffix
            event_id = f"ev_{int(now * 1000)}_{os.urandom(4).hex()}"
            cur = self.conn.execute(
                "INSERT INTO events(event_id,idempotency_key,ts,severity,category,goal_id,"
                " task_id,summary,details_json,cycle) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, idempotency_key, now, severity, category, goal_id, task_id,
                 summary[:500], json.dumps(details)[:4000], cycle),
            )
            self.conn.commit()
            return cur.lastrowid, True

    def list_events(self, severity=None, category=None, goal_id=None, since=None,
                    search=None, page=1, per_page=50):
        per_page = max(1, min(int(per_page or 50), 200))
        page = max(1, int(page or 1))
        conds, args = [], []
        if severity in ("info", "warn", "error"):
            conds.append("severity=?")
            args.append(severity)
        if category:
            conds.append("category=?")
            args.append(category)
        if goal_id is not None:
            conds.append("goal_id=?")
            args.append(int(goal_id))
        if since:
            conds.append("ts>=?")
            args.append(float(since))
        if search:
            conds.append("(summary LIKE ?)")
            args.append(f"%{search[:80]}%")
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = self.conn.execute(f"SELECT COUNT(*) AS c FROM events{where}", args).fetchone()["c"]
        rows = self.conn.execute(
            f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [per_page, (page - 1) * per_page]).fetchall()
        return [dict(r) for r in rows], int(total)

    # --- tasks / subtasks ---
    def add_task(self, goal_id, title, status="pending", evidence=""):
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO tasks(goal_id,title,status,evidence,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)", (goal_id, title[:300], status, evidence[:1000], now, now))
        self.conn.commit()
        return cur.lastrowid

    def update_task(self, task_id, status=None, evidence=None):
        t = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            return False
        d = dict(t)
        if status:
            d["status"] = status
        if evidence is not None:
            d["evidence"] = evidence[:1000]
        d["updated_at"] = time.time()
        self.conn.execute("UPDATE tasks SET status=?, evidence=?, updated_at=? WHERE id=?",
                          (d["status"], d["evidence"], d["updated_at"], task_id))
        self.conn.commit()
        return True

    def list_tasks(self, goal_id=None, status=None):
        conds, args = [], []
        if goal_id is not None:
            conds.append("goal_id=?")
            args.append(int(goal_id))
        if status:
            conds.append("status=?")
            args.append(status)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = self.conn.execute(f"SELECT * FROM tasks{where} ORDER BY id DESC LIMIT 200", args).fetchall()
        return [dict(r) for r in rows]

    # --- usage + reconciliation snapshots ---
    def snapshot_usage(self, cycle, usage):
        self.conn.execute("INSERT INTO usage_snapshots(cycle,usage_json,created_at) VALUES(?,?,?)",
                          (cycle, json.dumps(usage)[:2000], time.time()))
        self.conn.commit()

    def latest_usage(self):
        row = self.conn.execute("SELECT * FROM usage_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def add_recon(self, gross, refunded, fee_estimate, profit_estimate,
                  source="dodo_api", basis="provider"):
        cur = self.conn.execute(
            "INSERT INTO payment_recon(gross,refunded,fee_estimate,profit_estimate,source,basis,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (gross, refunded, fee_estimate, profit_estimate, source, basis, time.time()))
        self.conn.commit()
        return cur.lastrowid

    def latest_recon(self):
        row = self.conn.execute("SELECT * FROM payment_recon ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # --- human approvals ---
    def request_approval(self, kind, context=None, created_by="agent"):
        try:
            from dashboard.redact import scrub_dict
            context = scrub_dict(context or {})
        except Exception:
            context = context or {}
        cur = self.conn.execute(
            "INSERT INTO approvals(kind,context_json,status,created_by,decided_by,created_at,decided_at)"
            " VALUES(?,?, 'pending',?, '',?,0)",
            (kind[:80], json.dumps(context)[:2000], created_by[:80], time.time()))
        self.conn.commit()
        return cur.lastrowid

    def decide_approval(self, approval_id, approve, decided_by):
        a = self.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not a or dict(a)["status"] != "pending":
            return None
        status = "approved" if approve else "denied"
        self.conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                          (status, decided_by[:80], time.time(), approval_id))
        self.conn.commit()
        return status

    def pending_approvals(self):
        rows = self.conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    # --- independent audit trail for operator commands ---
    def audit_log(self, actor, command, ok=True, note=""):
        try:
            from dashboard.redact import scrub
            note = scrub(note)
        except Exception:
            pass
        cur = self.conn.execute("INSERT INTO audit(actor,command,ok,note,created_at) VALUES(?,?,?,?,?)",
                                (actor[:80], command[:80], 1 if ok else 0, note[:1000], time.time()))
        self.conn.commit()
        return cur.lastrowid

    def list_audit(self, page=1, per_page=50):
        per_page = max(1, min(int(per_page or 50), 200))
        page = max(1, int(page or 1))
        total = self.conn.execute("SELECT COUNT(*) AS c FROM audit").fetchone()["c"]
        rows = self.conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?",
                                 (per_page, (page - 1) * per_page)).fetchall()
        return [dict(r) for r in rows], int(total)

    # --- sync cursors (runner -> D1; never moves the deadline) ---
    def events_since(self, last_id=0, limit=200):
        rows = self.conn.execute(
            "SELECT * FROM events WHERE id>? ORDER BY id ASC LIMIT ?",
            (int(last_id or 0), max(1, min(int(limit or 200), 500)))).fetchall()
        return [dict(r) for r in rows]

    def audit_since(self, last_id=0, limit=200):
        rows = self.conn.execute(
            "SELECT * FROM audit WHERE id>? ORDER BY id ASC LIMIT ?",
            (int(last_id or 0), max(1, min(int(limit or 200), 500)))).fetchall()
        return [dict(r) for r in rows]

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
