import unittest
import os
import tempfile

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory import Store
from config import get_config
from supervisor import Supervisor, SupervisorError
from tools.base import Registry, ToolResult
from tools.dodo_tool import DodoTool


class TestMemory(unittest.TestCase):
    def test_verified_gross_excludes_bad(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        s.upsert_payment("p1", 10, "USD", "succeeded", 0, 0, 0, {})
        s.upsert_payment("p2", 10, "USD", "succeeded", 1, 0, 0, {})  # test -> excluded
        s.upsert_payment("p3", 5, "USD", "succeeded", 0, 1, 0, {})  # refunded -> excluded
        s.upsert_payment("p4", 5, "USD", "succeeded", 0, 0, 1, {})  # self -> excluded
        self.assertEqual(s.verified_gross(), 10.0)

    def test_goals(self):
        d = tempfile.mkdtemp()
        s = Store(d)
        gid = s.add_goal("test idea", 5, 5, 5, 2, 5.0)
        s.update_goal(gid, status="active")
        self.assertEqual(s.active_goal()["id"], gid)


class TestSupervisor(unittest.TestCase):
    def test_kill_switch(self):
        d = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = d
        os.environ["KILL_SWITCH_FILE"] = os.path.join(d, "STOP")
        cfg = get_config()
        s = Store(d)
        sup = Supervisor(cfg, s)
        open(os.path.join(d, "STOP"), "w").write("stop")
        with self.assertRaises(SupervisorError):
            sup.check(0)

    def test_live_denied_by_default(self):
        d = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = d
        os.environ.pop("KILL_SWITCH_FILE", None)
        cfg = get_config()
        s = Store(d)
        sup = Supervisor(cfg, s)
        self.assertFalse(sup.allow_live_money())


class TestLoop(unittest.TestCase):
    def test_full_cycle_dry_run(self):
        d = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = d
        os.environ["KILL_SWITCH_FILE"] = os.path.join(d, "STOP")
        os.environ["CYCLE_SLEEP_SECONDS"] = "0"
        from main import build_system
        cfg, store, sup, loop = build_system()
        res = loop.run(max_cycles=6, sleep=False)
        self.assertIn("gross", res)
        # agent must have built real artifacts
        self.assertTrue(os.path.exists(os.path.join(d, "agent.db")))
        # no fake success: gross stays 0 without Dodo key
        self.assertEqual(store.verified_gross(), 0.0)


if __name__ == "__main__":
    unittest.main()
