"""Dashboard tests: auth, approvals, ingestion, redaction, pagination, revenue,
deadline display, supervisor controls, sync safety. No live credentials."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory import Store
from config import get_config
from supervisor import Supervisor
from dashboard import api as dash_api
from dashboard.redact import scrub, scrub_dict

TOKEN = "dash-test-token-xyz"


class EnvGuard:
    def __init__(self):
        self.saved = dict(os.environ)

    def restore(self):
        os.environ.clear()
        os.environ.update(self.saved)


def fresh_env(data_dir, **overrides):
    for k in ("DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE", "DASHBOARD_API_URL",
              "DODO_FEE_PCT", "DATA_DIR", "KILL_SWITCH_FILE"):
        os.environ.pop(k, None)
    os.environ["DATA_DIR"] = data_dir
    os.environ["KILL_SWITCH_FILE"] = os.path.join(data_dir, "STOP")
    os.environ["CYCLE_SLEEP_SECONDS"] = "0"
    defaults = {
        "MAX_RUNTIME_HOURS": "168", "MAX_ITERATIONS": "50",
        "MAX_LLM_CALLS": "1000", "MAX_TOOL_CALLS": "5000",
        "MAX_LLM_SPEND_USD": "10.0", "MAX_INFRA_SPEND_USD": "25.0",
        "MAX_SPEND_USD": "50.0", "MAX_CONSECUTIVE_FAILURES": "25",
        "SANDBOX_MODE": "1", "APPROVE_LIVE_PAYMENTS": "0",
        "DASHBOARD_TOKEN": TOKEN,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        os.environ[k] = v


def make_stack(data_dir):
    cfg = get_config()
    store = Store(data_dir)
    sup = Supervisor(cfg, store)
    return cfg, store, sup


class LiveServer:
    def __init__(self, cfg, store, sup):
        self.srv = dash_api.serve(cfg, store, sup, port=0)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path, token=TOKEN):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def post(self, path, body, token=TOKEN):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}


class TestDashboardAuth(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()
        self.d = tempfile.mkdtemp()
        fresh_env(self.d)
        self.cfg, self.store, self.sup = make_stack(self.d)
        self.srv = LiveServer(self.cfg, self.store, self.sup)

    def tearDown(self):
        self.srv.stop()
        self.g.restore()

    def test_health_public(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}/api/health")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_overview_requires_token(self):
        code, _ = self.srv.get("/api/overview", token="wrong")
        self.assertEqual(code, 401)

    def test_overview_ok_with_token(self):
        code, body = self.srv.get("/api/overview")
        self.assertEqual(code, 200)
        self.assertIn("verified_gross_usd", body)
        dumped = json.dumps(body)
        self.assertNotIn(TOKEN, dumped)

    def test_fail_closed_without_token(self):
        os.environ.pop("DASHBOARD_TOKEN", None)
        cfg2 = get_config()
        self.assertFalse(dash_api.authenticate({"Authorization": "Bearer x"}, cfg2))


class TestControlsAuthorization(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()
        self.d = tempfile.mkdtemp()
        fresh_env(self.d)
        self.cfg, self.store, self.sup = make_stack(self.d)
        self.srv = LiveServer(self.cfg, self.store, self.sup)

    def tearDown(self):
        self.srv.stop()
        self.g.restore()

    def test_pause_resume_shutdown_flow(self):
        code, body = self.srv.post("/api/controls/pause", {"actor": "tester"})
        self.assertEqual(code, 200)
        self.assertTrue(os.path.exists(os.path.join(self.d, "STOP")))
        code, body = self.srv.post("/api/controls/resume", {"actor": "tester"})
        self.assertEqual(code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.d, "STOP")))
        code, body = self.srv.post("/api/controls/shutdown", {"actor": "tester"})
        self.assertEqual(code, 200)
        self.assertEqual(self.sup.experiment_status(), "stopped_kill_switch")

    def test_unknown_action_rejected(self):
        code, _ = self.srv.post("/api/controls/format", {"actor": "tester"})
        self.assertEqual(code, 400)

    def test_controls_cannot_touch_caps_deadline_or_revenue(self):
        before_caps = dict(self.store.get_state("caps"))
        before_start = self.sup.started_at
        before_pay = self.store.conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"]
        for action in ("pause", "resume", "diagnostic"):
            self.srv.post(f"/api/controls/{action}", {"actor": "tester"})
        self.assertEqual(self.store.get_state("caps"), before_caps)
        self.assertEqual(self.sup.started_at, before_start)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"], before_pay)
        # no payment write endpoint exists
        code, _ = self.srv.post("/api/payments", {})
        self.assertEqual(code, 404)

    def test_approval_flow(self):
        aid = self.store.request_approval("live_spend", {"amount": 5})
        code, body = self.srv.get("/api/approvals")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["pending"]), 1)
        code, body = self.srv.post(f"/api/approvals/{aid}/decide", {"approve": True, "actor": "op"})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "approved")
        code, _ = self.srv.post(f"/api/approvals/{aid}/decide", {"approve": False, "actor": "op"})
        self.assertEqual(code, 409)

    def test_audit_trail_records_controls(self):
        self.srv.post("/api/controls/pause", {"actor": "tester"})
        rows, total = self.store.list_audit()
        self.assertGreaterEqual(total, 1)
        self.assertTrue(any("pause" in r["command"] for r in rows))


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_idempotent_ingestion(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        id1, new1 = s.record_event("tool", "x ok", idempotency_key="dup-key-1")
        id2, new2 = s.record_event("tool", "x ok", idempotency_key="dup-key-1")
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(id1, id2)

    def test_pagination_stable(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        for i in range(7):
            s.record_event("tool", f"event {i}", idempotency_key=f"pg-{i}")
        p1, total = s.list_events(page=1, per_page=3)
        p2, _ = s.list_events(page=2, per_page=3)
        p3, _ = s.list_events(page=3, per_page=3)
        self.assertEqual(total, 7)
        ids1 = {r["id"] for r in p1}
        self.assertTrue(ids1.isdisjoint({r["id"] for r in p2}))
        self.assertEqual(len(p3), 1)

    def test_per_page_clamped(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        rows, _ = s.list_events(page=1, per_page=9999)
        self.assertIsInstance(rows, list)

    def test_redaction_in_events(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        get_config()  # ensure env read path exercised
        s = Store(d)
        secret = "sk-live-SECRET123"
        os.environ["LLM_API_KEY"] = secret
        s.record_event("error", f"failed with {secret} for a@b.com",
                       details={"args": {"api_key": secret}}, idempotency_key="red-1")
        rows, _ = s.list_events()
        dumped = json.dumps(rows)
        self.assertNotIn(secret, dumped)
        self.assertNotIn("a@b.com", dumped)

    def test_scrub_dict_redacts_keys(self):
        out = scrub_dict({"api_key": "abc", "nested": {"token": "def"}, "safe": "ok"})
        self.assertEqual(out["api_key"], "***")
        self.assertEqual(out["nested"]["token"], "***")
        self.assertEqual(out["safe"], "ok")


class TestRevenueIntegrity(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_only_provider_rows_count(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        cfg, store, _ = make_stack(d)
        store.upsert_payment("g1", 20, "USD", "succeeded", 0, 0, 0, {}, origin="dodo_api")
        store.upsert_payment("m1", 99, "USD", "succeeded", 0, 0, 0, {}, origin="manual")
        store.upsert_payment("t1", 50, "USD", "succeeded", 1, 0, 0, {}, origin="dodo_api")
        store.upsert_payment("r1", 5, "USD", "refunded", 0, 1, 0, {}, origin="dodo_api")
        rev = dash_api.revenue_summary(cfg, store)
        self.assertEqual(rev["gross_usd"], 20.0)
        self.assertEqual(rev["refunded_usd"], 5.0)
        self.assertEqual(rev["basis"], "provider")
        self.assertFalse(any(t["id"] == "m1" and t["genuine"] for t in rev["transactions"]))

    def test_fees_unavailable_not_fabricated(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        cfg, store, _ = make_stack(d)
        rev = dash_api.revenue_summary(cfg, store)
        self.assertEqual(rev["fees_basis"], "unavailable")
        self.assertEqual(rev["profit_basis"], "estimate")

    def test_fee_estimate_labeled(self):
        d = tempfile.mkdtemp()
        fresh_env(d, DODO_FEE_PCT="5")
        cfg, store, _ = make_stack(d)
        store.upsert_payment("g2", 100, "USD", "succeeded", 0, 0, 0, {}, origin="dodo_api")
        rev = dash_api.revenue_summary(cfg, store)
        self.assertEqual(rev["fees_basis"], "estimate")
        self.assertEqual(rev["fees_usd"], 5.0)


class TestDeadlineDisplay(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_absolute_deadline_preserved(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        cfg, store, sup = make_stack(d)
        ov = dash_api.overview(cfg, store, sup)
        self.assertAlmostEqual(ov["deadline_at"] - ov["started_at"], 168 * 3600, delta=1)
        # restart preserves
        store.close()
        cfg2, store2, sup2 = make_stack(d)
        ov2 = dash_api.overview(cfg2, store2, sup2)
        self.assertEqual(ov2["started_at"], ov["started_at"])
        self.assertEqual(ov2["deadline_at"], ov["deadline_at"])


class TestSyncSafety(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_push_drain_noop_without_backend(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        from dashboard import sync as dash_sync
        cfg, store, sup = make_stack(d)
        self.assertEqual(dash_sync.push(cfg, store), (0, 0))
        self.assertEqual(dash_sync.drain_controls(cfg, store, sup), 0)


if __name__ == "__main__":
    unittest.main()
