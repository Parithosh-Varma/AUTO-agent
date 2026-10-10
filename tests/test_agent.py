import unittest
import os
import tempfile

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class EnvGuard:
    """Isolate env mutations per test (old suite leaked DATA_DIR across tests)."""
    def __init__(self):
        self.saved = dict(os.environ)

    def restore(self):
        os.environ.clear()
        os.environ.update(self.saved)


from memory import Store
from config import get_config
from supervisor import Supervisor, SupervisorError


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.guard = EnvGuard()

    def tearDown(self):
        self.guard.restore()

    def test_verified_gross_excludes_bad(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        # only origin='dodo_api', USD, succeeded, live, non-refunded, non-self counts
        s.upsert_payment("p1", 10, "USD", "succeeded", 0, 0, 0, {}, origin="dodo_api")
        s.upsert_payment("p2", 10, "USD", "succeeded", 1, 0, 0, {}, origin="dodo_api")  # test
        s.upsert_payment("p3", 5, "USD", "succeeded", 0, 1, 0, {}, origin="dodo_api")  # refunded
        s.upsert_payment("p4", 5, "USD", "succeeded", 0, 0, 1, {}, origin="dodo_api")  # self
        s.upsert_payment("p5", 100, "USD", "succeeded", 0, 0, 0, {}, origin="manual")  # fabricated
        s.upsert_payment("p6", 50, "EUR", "succeeded", 0, 0, 0, {}, origin="dodo_api")  # non-USD
        s.upsert_payment("p7", 7, "USD", "disputed", 0, 1, 0, {}, origin="dodo_api")  # disputed
        self.assertEqual(s.verified_gross(), 10.0)

    def test_goals(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        gid = s.add_goal("test idea", 5, 5, 5, 2, 5.0)
        s.update_goal(gid, status="active")
        self.assertEqual(s.active_goal()["id"], gid)


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        self.guard = EnvGuard()

    def tearDown(self):
        self.guard.restore()

    def test_kill_switch(self):
        d = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = d
        os.environ["KILL_SWITCH_FILE"] = os.path.join(d, "STOP")
        cfg = get_config()
        s = Store(d)
        sup = Supervisor(cfg, s)
        with open(os.path.join(d, "STOP"), "w") as f:
            f.write("stop")
        with self.assertRaises(SupervisorError):
            sup.check(0)

    def test_live_denied_by_default(self):
        d = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = d
        os.environ.pop("KILL_SWITCH_FILE", None)
        for k in ("SANDBOX_MODE", "APPROVE_LIVE_PAYMENTS"):
            os.environ.pop(k, None)
        # force sandbox defaults regardless of outer env
        os.environ["SANDBOX_MODE"] = "1"
        os.environ["APPROVE_LIVE_PAYMENTS"] = "0"
        cfg = get_config()
        s = Store(d)
        sup = Supervisor(cfg, s)
        self.assertFalse(sup.allow_live_money())


class TestLoop(unittest.TestCase):
    def setUp(self):
        self.guard = EnvGuard()

    def tearDown(self):
        self.guard.restore()

    def test_full_cycle_dry_run(self):
        d = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = d
        os.environ["KILL_SWITCH_FILE"] = os.path.join(d, "STOP")
        os.environ["CYCLE_SLEEP_SECONDS"] = "0"
        os.environ["MAX_RUNTIME_HOURS"] = "168"
        from main import build_system
        cfg, store, sup, loop, _, _ = build_system()
        res = loop.run(max_cycles=6, sleep=False)
        self.assertIn("gross", res)
        self.assertTrue(os.path.exists(os.path.join(d, "agent.db")))
        # no fake success: gross stays 0 without Dodo key (origin-gated)
        self.assertEqual(store.verified_gross(), 0.0)


if __name__ == "__main__":
    unittest.main()
