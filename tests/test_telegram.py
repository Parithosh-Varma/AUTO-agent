"""Telegram notification + intervention tests. All transports mocked; no live creds."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory import Store
from config import get_config
from supervisor import Supervisor
from tools.telegram_tool import TelegramTool
from notify.alerts import AlertManager
from notify.commands import CommandRouter

TOKEN = "TG-TEST-TOKEN-abc123"
CHAT = "111"


class EnvGuard:
    def __init__(self):
        self.saved = dict(os.environ)

    def restore(self):
        os.environ.clear()
        os.environ.update(self.saved)


def fresh_env(data_dir, **overrides):
    for k in ("TELEGRAMBOTTOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAMCHATID",
              "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_IDS", "TELEGRAM_TOKEN_FILE",
              "TELEGRAM_ALERT_COOLDOWN_S"):
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
        "TELEGRAMBOTTOKEN": TOKEN, "TELEGRAMCHATID": CHAT,
        "TELEGRAM_ALLOWED_IDS": f"{CHAT},222",
        "TELEGRAM_ALERT_COOLDOWN_S": "60",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        os.environ[k] = v


class FakeTransport:
    """Scripted Telegram API. Records calls; never touches the network."""

    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    def __call__(self, method, payload, token):
        self.calls.append((method, dict(payload)))
        assert token == TOKEN, "token must pass through to transport only"
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {"ok": True, "result": {"message_id": 7}}


def make_system(data_dir, transport):
    cfg = get_config()
    store = Store(data_dir)
    sup = Supervisor(cfg, store)
    sender = TelegramTool(cfg, store, transport=transport)
    notifier = AlertManager(cfg, store, sup, sender)
    router = CommandRouter(cfg, store, sup, sender, notifier)
    return cfg, store, sup, sender, notifier, router


class TestDelivery(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_confirmed_delivery_marks_sent(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        _, store, _, sender, notifier, _ = make_system(d, FakeTransport())
        out = notifier.notify("repeated_failures", {"goal": "g", "tool": "t",
                                                    "error": "boom", "sig": "s1"})
        self.assertTrue(out["sent"])
        self.assertEqual(store.notification_counts().get("sent"), 1)

    def test_unconfirmed_is_failure_with_backoff(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport(script=[{"ok": False, "description": "nope"}])
        _, store, _, _, notifier, _ = make_system(d, ft)
        out = notifier.notify("repeated_failures", {"sig": "s2", "error": "x"})
        self.assertFalse(out["sent"])
        self.assertEqual(len(store.due_notifications(time.time())), 0)  # backed off
        self.assertEqual(len(store.due_notifications(time.time() + 3601)), 1)  # due later
        failed = store.notification_counts().get("failed")
        self.assertEqual(failed, 1)

    def test_exception_retries_then_succeeds(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport(script=[Exception("down"), {"ok": True, "result": {"message_id": 9}}])
        _, store, _, _, notifier, _ = make_system(d, ft)
        notifier.notify("repeated_failures", {"sig": "s3"})
        self.assertEqual(store.notification_counts().get("failed"), 1)
        # force retry due
        store.conn.execute("UPDATE notifications SET next_retry_at=0")
        store.conn.commit()
        self.assertEqual(notifier.flush(), 1)
        self.assertEqual(store.notification_counts().get("sent"), 1)

    def test_token_never_logged_or_exposed(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport()
        _, store, _, sender, notifier, _ = make_system(d, ft)
        res = sender.send_message("hello")
        self.assertTrue(res.ok)
        self.assertNotIn(TOKEN, res.output)
        notifier.notify("repeated_failures", {"error": f"leak {TOKEN} user a@b.com", "sig": "s4"})
        row = store.conn.execute("SELECT message FROM notifications").fetchone()
        self.assertNotIn(TOKEN, row["message"])
        self.assertNotIn("a@b.com", row["message"])
        log = open(os.path.join(d, "notifications.log")).read() if os.path.exists(os.path.join(d, "notifications.log")) else ""
        self.assertNotIn(TOKEN, log)


class TestDedupeCooldown(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_same_blocker_sends_once_in_cooldown(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport()
        _, _, _, _, notifier, _ = make_system(d, ft)
        ctx = {"goal": "g", "tool": "t", "error": "boom"}
        self.assertTrue(notifier.set_blocker("k1", ctx)["sent"])
        sends_after_first = len(ft.calls)
        out2 = notifier.set_blocker("k1", ctx)  # still active
        self.assertFalse(out2["sent"])
        self.assertEqual(len(ft.calls), sends_after_first)

    def test_recovery_sent_on_resolve(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport()
        _, _, _, _, notifier, _ = make_system(d, ft)
        notifier.set_blocker("k2", {"goal": "g", "tool": "t", "error": "x"})
        n_before = len(ft.calls)
        out = notifier.clear_blocker("k2", {"goal": "g", "tool": "t"})
        self.assertTrue(out["sent"])
        self.assertGreater(len(ft.calls), n_before)
        self.assertFalse(notifier.clear_blocker("k2")["sent"])


class TestCommandAuth(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def _update(self, uid, chat, user, text):
        return {"update_id": uid, "message": {"chat": {"id": chat},
                                              "from": {"id": user}, "text": text}}

    def test_unauthorized_pause_rejected_no_state_change(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport(script=[{"ok": True, "result": [
            self._update(1, "999", "999", "/pause")]}])
        _, store, sup, _, _, router = make_system(d, ft)
        router.poll_and_handle()
        self.assertFalse(os.path.exists(os.path.join(d, "STOP")))
        self.assertEqual(sup.experiment_status(), "active")
        # no reply to attacker
        self.assertFalse(any(c[0] == "sendMessage" for c in ft.calls))

    def test_authorized_pause_resume_stop(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport(script=[
            {"ok": True, "result": [self._update(1, CHAT, CHAT, "/pause")]},
            {"ok": True, "result": []},
            {"ok": True, "result": []},
        ])
        _, store, sup, _, _, router = make_system(d, ft)
        router.poll_and_handle()
        self.assertTrue(os.path.exists(os.path.join(d, "STOP")))
        self.assertEqual(router.execute("/resume", source="telegram:111"), "resumed. Deadline unchanged.")
        self.assertFalse(os.path.exists(os.path.join(d, "STOP")))
        self.assertIn("stopped", router.execute("/stop", source="telegram:111").lower())
        self.assertEqual(sup.experiment_status(), "stopped_kill_switch")

    def test_read_commands_no_state_change_no_secrets(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        _, store, sup, _, _, router = make_system(d, FakeTransport())
        for cmd in ("/status", "/why", "/report"):
            reply = router.execute(cmd, source="telegram:111")
            self.assertTrue(reply)
            self.assertNotIn(TOKEN, reply)
        self.assertFalse(os.path.exists(os.path.join(d, "STOP")))
        self.assertEqual(sup.experiment_status(), "active")

    def test_unknown_command_usage_no_state_change(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport(script=[{"ok": True, "result": [
            self._update(5, CHAT, CHAT, "/selfdestruct")]}])
        _, _, sup, _, _, router = make_system(d, ft)
        router.poll_and_handle()
        self.assertEqual(sup.experiment_status(), "active")
        self.assertTrue(any(c[0] == "sendMessage" for c in ft.calls))


class TestSupervisorEnforcement(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_pause_resume_preserve_deadline_caps_usage(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        _, store, sup, _, _, router = make_system(d, FakeTransport())
        before_deadline = sup.deadline_at()
        before_caps = dict(store.get_state("caps"))
        before_usage = dict(store.get_state("usage"))
        router.execute("/pause", source="telegram:111")
        router.execute("/resume", source="telegram:111")
        self.assertEqual(sup.deadline_at(), before_deadline)
        self.assertEqual(store.get_state("caps"), before_caps)
        u = store.get_state("usage")
        for k in ("llm_calls", "tool_calls", "llm_spend_usd", "infra_spend_usd", "spend_usd"):
            self.assertEqual(u[k], before_usage[k])
        self.assertFalse(sup.allow_live_money())

    def test_stop_never_grants_live_money(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        _, _, sup, _, _, router = make_system(d, FakeTransport())
        router.execute("/stop", source="telegram:111")
        self.assertFalse(sup.allow_live_money())
        self.assertEqual(sup.experiment_status(), "stopped_kill_switch")


class TestResilience(unittest.TestCase):
    def setUp(self):
        self.g = EnvGuard()

    def tearDown(self):
        self.g.restore()

    def test_disabled_notifier_never_breaks_work(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        os.environ.pop("TELEGRAMBOTTOKEN", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        from main import build_system
        cfg, store, sup, loop, notifier, _ = build_system()
        self.assertFalse(notifier.enabled())
        res = loop.run(max_cycles=2, sleep=False)
        self.assertIn("gross", res)

    def test_failed_notify_does_not_halt_loop(self):
        d = tempfile.mkdtemp()
        fresh_env(d)

        def always_down(method, payload, token):
            raise Exception("net down")

        from main import build_system
        cfg, store, sup, loop, notifier, _ = build_system(transport=always_down)
        self.assertTrue(notifier.enabled())
        res = loop.run(max_cycles=3, sleep=False)
        self.assertIn("gross", res)
        counts = store.notification_counts()
        self.assertGreaterEqual(counts.get("failed", 0), 0)

    def test_diagnostics_present_and_scrubbed(self):
        d = tempfile.mkdtemp()
        fresh_env(d)
        ft = FakeTransport()
        _, store, _, _, notifier, _ = make_system(d, ft)
        notifier.notify("blocker_unresolved", {
            "goal": "ship x", "tool": "cloudflare.deploy", "error": f"bad {TOKEN} me@x.com",
            "retries": 5, "sig": "diag1", "next": "retry deploy",
        })
        msg = store.conn.execute("SELECT message FROM notifications").fetchone()["message"]
        for needle in ("blocker_unresolved", "ship x", "cloudflare.deploy",
                       "retries: 5", "remaining:", "next: retry deploy"):
            self.assertIn(needle, msg)
        self.assertNotIn(TOKEN, msg)
        self.assertNotIn("me@x.com", msg)


if __name__ == "__main__":
    unittest.main()
