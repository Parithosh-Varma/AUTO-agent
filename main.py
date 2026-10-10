"""CLI entrypoint. Crash-recovering continuous runner."""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.brain import Brain
from agent.loop import AutonomousLoop
from config import get_config
from memory import Store
from supervisor import Supervisor, SupervisorError
from tools.analytics_tool import AnalyticsTool
from tools.base import Registry
from tools.build_tool import BuildTool
from tools.cloudflare_tool import CloudflareTool
from tools.dodo_tool import DodoTool
from tools.github_tool import GitHubTool
from tools.llm_tool import LLMTool
from tools.search_tool import SearchTool
from tools.telegram_tool import TelegramTool
from notify.alerts import AlertManager
from notify.commands import CommandRouter


def build_system(transport=None):
    cfg = get_config()
    store = Store(cfg.data_dir)
    sup = Supervisor(cfg, store)
    store.requeue_stuck()
    llm = LLMTool(cfg)
    reg = Registry(store)
    dodo = DodoTool(cfg, store, sup)
    reg.register("llm", llm)
    reg.register("github", GitHubTool(cfg, store))
    reg.register("cloudflare", CloudflareTool(cfg, store))
    reg.register("dodo", dodo)
    reg.register("search", SearchTool(cfg))
    reg.register("analytics", AnalyticsTool(cfg))
    reg.register("build", BuildTool(cfg, store))
    telegram = TelegramTool(cfg, store, transport=transport)
    reg.register("telegram", telegram)
    notifier = AlertManager(cfg, store, sup, telegram)
    commands = CommandRouter(cfg, store, sup, telegram, notifier)
    brain = Brain(llm, store)
    loop = AutonomousLoop(cfg, store, sup, reg, brain, dodo,
                          notifier=notifier, commands=commands)
    return cfg, store, sup, loop, notifier, commands


def cmd_status():
    cfg, store, sup, _, notifier, _ = build_system()
    st = sup.status(int(store.get_state("cycle", 0) or 0))
    st["telegram"] = {
        "configured": notifier.enabled(),
        "pending": len(store.due_notifications(time.time())),
        "counts": store.notification_counts(),
    }
    print(json.dumps(st, indent=2))


def _paused_for_kill_switch(cfg, sup):
    return os.path.exists(cfg.kill_switch_file) and sup.terminal_reason() is None


def cmd_run(cycles, daemon):
    cfg, store, sup, loop, notifier, commands = build_system()
    print(f"target=${cfg.target_revenue_usd:.2f} sandbox={cfg.sandbox_mode} "
          f"data={cfg.data_dir} (kill: touch {cfg.kill_switch_file}) "
          f"telegram={'on' if notifier.enabled() else 'off'}")

    def log(msg):
        print(msg, flush=True)

    if notifier.enabled():
        notifier.notify_started()
        notifier.flush()

    if daemon:
        crashed = False
        while True:
            try:
                res = loop.run(max_cycles=cfg.max_iterations, sleep=True, on_event=log)
            except SupervisorError as e:
                msg = str(e)
                log(f"[supervisor] {e}")
                try:
                    if "target reached" in msg.lower():
                        notifier.notify_target()
                    elif "deadline" in msg.lower():
                        notifier.notify_deadline()
                    else:
                        notifier.notify_shutdown(msg)
                    notifier.flush()
                except Exception:
                    pass
                if _paused_for_kill_switch(cfg, sup):
                    # stay alive for /resume: poll Telegram until cleared/terminal
                    log("[paused] waiting for /resume (deadline keeps running)")
                    try:
                        notifier.notify("paused", {"tool": "supervisor", "error": msg[:200],
                                                   "next": "/resume to continue"})
                    except Exception:
                        pass
                    while _paused_for_kill_switch(cfg, sup):
                        try:
                            commands.poll_and_handle()
                            notifier.flush(limit=3)
                        except Exception:
                            pass
                        time.sleep(cfg.cycle_sleep_seconds)
                    log("[resumed] continuing")
                    try:
                        notifier.notify("resumed", {"tool": "supervisor", "error": "-",
                                                   "next": "work continuing"})
                    except Exception:
                        pass
                    continue
                print(json.dumps({"stopped": msg,
                                  "experiment_status": sup.experiment_status()}, indent=2))
                break
            except Exception as e:  # crash recovery: persist cycle, resume
                log(f"[crash] {e} — state preserved, resuming in {cfg.cycle_sleep_seconds}s")
                try:
                    notifier.notify_crash(e)
                    notifier.flush(limit=3)
                except Exception:
                    pass
                time.sleep(cfg.cycle_sleep_seconds)
                crashed = True
                continue
            if crashed:
                try:
                    notifier.notify_recovered()
                    notifier.flush(limit=3)
                except Exception:
                    pass
                crashed = False
            print(json.dumps(res, indent=2))
            break
    else:
        try:
            res = loop.run(max_cycles=cycles, sleep=False, on_event=log)
        except SupervisorError as e:
            print(json.dumps({"stopped": str(e),
                              "experiment_status": sup.experiment_status()}, indent=2))
            return
        print(json.dumps(res, indent=2))


def cmd_notify_test():
    cfg, store, sup, _, notifier, _ = build_system()
    if not notifier.enabled():
        print("telegram not configured: set TELEGRAMBOTTOKEN and TELEGRAMCHATID "
              "(or TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, or TELEGRAM_TOKEN_FILE). "
              "No message sent; unit tests do not need credentials.")
        return
    res = notifier.notify("test_message", {"tool": "-", "error": "-",
                                           "goal": "connectivity check",
                                           "next": "no action needed"})
    notifier.flush()
    print(json.dumps({"sent": bool(res.get("sent")), "queued": res.get("queued"),
                      "reason": res.get("reason", "")}, indent=2))


def cmd_telegram_poll():
    _, _, _, _, notifier, commands = build_system()
    if not notifier.enabled():
        print("telegram not configured; nothing polled.")
        return
    n = commands.poll_and_handle()
    notifier.flush()
    print(json.dumps({"handled": n}, indent=2))


def cmd_reset():
    cfg = get_config()
    if os.path.exists(cfg.data_dir):
        for name in ("agent.db", "SUCCESS.json", "final_report.md", "analytics.log",
                     "notifications.log"):
            p = os.path.join(cfg.data_dir, name)
            if os.path.exists(p):
                os.remove(p)
                print(f"removed {p}")
    print("reset done (workspace/artifacts kept). Use rm -rf data for full wipe.")


def main():
    ap = argparse.ArgumentParser(description="Continuously autonomous revenue agent")
    ap.add_argument("--once", action="store_true", help="run 1 cycle")
    ap.add_argument("--cycles", type=int, default=10, help="run N cycles")
    ap.add_argument("--daemon", action="store_true", help="run continuously with recovery")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--notify-test", action="store_true",
                    help="send a harmless Telegram test message (needs credentials)")
    ap.add_argument("--telegram-poll", action="store_true",
                    help="poll Telegram commands once")
    a = ap.parse_args()
    if a.status:
        return cmd_status()
    if a.reset:
        return cmd_reset()
    if a.notify_test:
        return cmd_notify_test()
    if a.telegram_poll:
        return cmd_telegram_poll()
    if a.once:
        return cmd_run(1, False)
    if a.daemon:
        return cmd_run(0, True)
    return cmd_run(a.cycles, False)


if __name__ == "__main__":
    main()
