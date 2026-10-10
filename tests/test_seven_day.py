"""Seven-day experiment hardening: deadline, crash recovery, verification, limits, termination."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory import Store, PROTECTED_STATE_KEYS
from config import get_config
from supervisor import Supervisor, SupervisorError


class EnvGuard:
    def __init__(self):
        self.saved = dict(os.environ)

    def restore(self):
        os.environ.clear()
        os.environ.update(self.saved)


def fresh_env(data_dir, **overrides):
    os.environ["DATA_DIR"] = data_dir
    os.environ["KILL_SWITCH_FILE"] = os.path.join(data_dir, "STOP")
    os.environ["CYCLE_SLEEP_SECONDS"] = "0"
    os.environ.pop("EXPERIMENT_HOURS", None)
    defaults = {
        "MAX_RUNTIME_HOURS": "168",
        "MAX_ITERATIONS": "500",
        "MAX_LLM_CALLS": "1000",
        "MAX_TOOL_CALLS": "5000",
        "MAX_LLM_SPEND_USD": "10.0",
        "MAX_INFRA_SPEND_USD": "25.0",
        "MAX_SPEND_USD": "50.0",
        "MAX_CONSECUTIVE_FAILURES": "25",
        "SANDBOX_MODE": "1",
        "APPROVE_LIVE_PAYMENTS": "0",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        os.environ[k] = v


class TestDeadline(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_default_is_168h_and_configurable(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        os.environ.pop("MAX_RUNTIME_HOURS", None)
        os.environ.pop("EXPERIMENT_HOURS", None)
        cfg = get_config()
        self.assertEqual(cfg.max_runtime_hours, 168.0)
        os.environ["MAX_RUNTIME_HOURS"] = "72"
        self.assertEqual(get_config().max_runtime_hours, 72.0)

    def test_deadline_persists_across_restarts(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        cfg = get_config()
        s = Store(d)
        sup1 = Supervisor(cfg, s)
        first = sup1.started_at
        self.assertAlmostEqual(first, time.time(), delta=5)
        # simulate restart: new Store + Supervisor on same dir
        s2 = Store(d)
        sup2 = Supervisor(get_config(), s2)
        self.assertEqual(sup2.started_at, first)
        self.assertGreater(sup2.deadline_at(), first)
        self.assertAlmostEqual(sup2.deadline_at() - first, 168 * 3600, delta=5)

    def test_expired_deadline_blocks_new_work(self):
        d = tempfile.mkdtemp()
        fresh_env(d, MAX_RUNTIME_HOURS="168")
        cfg = get_config()
        s = Store(d)
        sup = Supervisor(cfg, s)
        # backdate start 169h ago via internal (agent cannot do this)
        s.set_state_internal("run_started_at", time.time() - 169 * 3600)
        sup3 = Supervisor(cfg, Store(d))
        self.assertTrue(sup3.is_expired())
        with self.assertRaises(SupervisorError) as cm:
            sup3.check(0)
        self.assertIn("deadline", str(cm.exception).lower())
        self.assertEqual(sup3.experiment_status(), "deadline_expired")


class TestCrashRecovery(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_cycle_usage_deadline_survive_crash(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        from main import build_system
        cfg, store, sup, loop, _, _ = build_system()
        started = sup.started_at
        loop.run(max_cycles=3, sleep=False)
        cycle_before = store.get_state("cycle", 0)
        usage_before = dict(store.get_state("usage", {}))
        self.assertEqual(cycle_before, 3)
        # simulate crash: drop objects, rebuild from same dir
        store.close()
        cfg2, store2, sup2, loop2, _, _ = build_system()
        self.assertEqual(store2.get_state("cycle", 0), 3)
        self.assertEqual(store2.get_state("usage", {}), usage_before)
        self.assertEqual(sup2.started_at, started)

    def test_claimed_jobs_requeued_no_duplicates(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        j1 = s.enqueue("research", {"q": "same"})
        j2 = s.enqueue("research", {"q": "same"})
        self.assertEqual(j1, j2)  # deduped
        self.assertEqual(s.pending_count(), 1)
        job = s.dequeue()
        self.assertIsNotNone(job)
        self.assertEqual(s.pending_count(), 0)
        # crash before complete: job is claimed; restart requeues it
        n = s.requeue_stuck()
        self.assertEqual(n, 1)
        self.assertEqual(s.pending_count(), 1)
        # dequeue again + complete -> no duplicate
        job2 = s.dequeue()
        self.assertEqual(job2["id"], job["id"])
        s.complete_job(job2["id"], ok=True)
        self.assertEqual(s.pending_count(), 0)

    def test_payment_upsert_idempotent(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        s.upsert_payment("dup1", 15, "USD", "succeeded", 0, 0, 0, {}, origin="dodo_api")
        s.upsert_payment("dup1", 15, "USD", "succeeded", 0, 0, 0, {}, origin="dodo_api")
        self.assertEqual(s.verified_gross(), 15.0)


class TestRevenueVerification(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_only_genuine_counts(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        s.upsert_payment("good", 25, "USD", "succeeded", 0, 0, 0, {"id": "good"}, origin="dodo_api")
        cases = [
            ("t1", 10, "USD", "succeeded", 1, 0, 0, "dodo_api"),  # test
            ("r1", 10, "USD", "refunded", 0, 1, 0, "dodo_api"),
            ("d1", 10, "USD", "disputed", 0, 1, 0, "dodo_api"),
            ("c1", 10, "USD", "chargeback", 0, 1, 0, "dodo_api"),
            ("f1", 10, "USD", "failed", 0, 1, 0, "dodo_api"),
            ("s1", 10, "USD", "succeeded", 0, 0, 1, "dodo_api"),  # self
            ("m1", 10, "USD", "succeeded", 0, 0, 0, "manual"),  # fabricated
            ("e1", 10, "EUR", "succeeded", 0, 0, 0, "dodo_api"),  # non-USD
            ("p1", 10, "USD", "pending", 0, 0, 0, "dodo_api"),  # not succeeded
        ]
        for pid, amt, cur, st, it, ir, isp, org in cases:
            s.upsert_payment(pid, amt, cur, st, it, ir, isp, {}, origin=org)
        self.assertEqual(s.verified_gross(), 25.0)

    def test_dry_run_cannot_fabricate_success(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        os.environ["DODO_API_KEY"] = ""
        from main import build_system
        cfg, store, sup, loop, _, _ = build_system()
        # attacker inserts manual row directly
        store.upsert_payment("evil", 25, "USD", "succeeded", 0, 0, 0, {}, origin="manual")
        self.assertEqual(store.verified_gross(), 0.0)
        res = store.verified_gross()
        self.assertLess(res, cfg.target_revenue_usd)


class TestSpendingLimits(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_tool_call_limit(self):
        d = tempfile.mkdtemp()
        fresh_env(d, MAX_TOOL_CALLS="2")
        sup = Supervisor(get_config(), Store(d))
        sup.add_usage(tool_calls=2)
        with self.assertRaises(SupervisorError):
            sup.check(0)

    def test_llm_spend_limits(self):
        d = tempfile.mkdtemp()
        fresh_env(d, MAX_LLM_CALLS="1", MAX_LLM_SPEND_USD="0.001")
        sup = Supervisor(get_config(), Store(d))
        sup.add_usage(llm_calls=1, llm_spend_usd=0.002)
        with self.assertRaises(SupervisorError):
            sup.check(0)

    def test_infra_spend_limit(self):
        d = tempfile.mkdtemp()
        fresh_env(d, MAX_INFRA_SPEND_USD="0.005")
        sup = Supervisor(get_config(), Store(d))
        sup.add_usage(infra_spend_usd=0.01)
        with self.assertRaises(SupervisorError):
            sup.check(0)

    def test_consecutive_failure_limit(self):
        d = tempfile.mkdtemp()
        fresh_env(d, MAX_CONSECUTIVE_FAILURES="3")
        s = Store(d)
        sup = Supervisor(get_config(), s)
        for _ in range(3):
            s.log_action(0, "x", "y", {}, False, "boom")
        with self.assertRaises(SupervisorError):
            sup.check(0)

    def test_agent_cannot_write_protected_keys(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        for key in PROTECTED_STATE_KEYS:
            with self.assertRaises(PermissionError):
                s.set_state(key, "hacked")
        # non-protected keys still work (agent's legitimate namespace)
        s.set_state("cycle", 1)
        s.set_state("stage_1", "build")
        self.assertEqual(s.get_state("cycle"), 1)


class TestSafeTermination(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_kill_switch(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        s = Store(d)
        sup = Supervisor(get_config(), s)
        with open(os.path.join(d, "STOP"), "w") as f:
            f.write("stop")
        with self.assertRaises(SupervisorError):
            sup.check(0)

    def test_target_reached_stops_new_tasks(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        from main import build_system
        cfg, store, sup, loop, _, _ = build_system()
        store.upsert_payment("win", 25, "USD", "succeeded", 0, 0, 0, {}, origin="dodo_api")
        with self.assertRaises(SupervisorError):
            sup.check(int(store.get_state("cycle", 0) or 0))
        self.assertEqual(sup.experiment_status(), "completed_target")
        # brain must not pick new goals once terminal
        from agent.brain import Brain
        from tools.llm_tool import LLMTool
        b = Brain(LLMTool(cfg), store)
        self.assertIsNone(b.pick_goal())

    def test_deadline_stops_new_tasks(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        from main import build_system
        cfg, store, sup, loop, _, _ = build_system()
        store.set_state_internal("run_started_at", time.time() - 200 * 3600)
        sup2 = Supervisor(cfg, Store(d))
        with self.assertRaises(SupervisorError):
            sup2.check(0)
        out = loop.run(max_cycles=3, sleep=False)
        # loop must stop, not start new revenue work
        self.assertIn("stopped", out)
        self.assertIn("deadline", out["stopped"].lower())


if __name__ == "__main__":
    unittest.main()
