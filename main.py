"""CLI entrypoint. Crash-recovering continuous runner."""
import argparse
import json
import os
import shutil
import sys

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


def build_system():
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
    brain = Brain(llm, store)
    loop = AutonomousLoop(cfg, store, sup, reg, brain, dodo)
    return cfg, store, sup, loop


def cmd_status():
    cfg, store, sup, _ = build_system()
    print(json.dumps(sup.status(int(store.get_state("cycle", 0) or 0)), indent=2))


def cmd_run(cycles, daemon):
    cfg, store, sup, loop = build_system()
    print(f"target=${cfg.target_revenue_usd:.2f} sandbox={cfg.sandbox_mode} "
          f"data={cfg.data_dir} (kill: touch {cfg.kill_switch_file})")

    def log(msg):
        print(msg, flush=True)

    if daemon:
        # crash-recovering: outer loop resumes after unexpected exceptions
        while True:
            try:
                res = loop.run(max_cycles=cfg.max_iterations, sleep=True, on_event=log)
            except SupervisorError as e:
                print(f"[supervisor] {e}")
                break
            except Exception as e:  # crash recovery: persist cycle, resume
                print(f"[crash] {e} — state preserved, resuming in {cfg.cycle_sleep_seconds}s")
                import time
                time.sleep(cfg.cycle_sleep_seconds)
                continue
            print(json.dumps(res, indent=2))
            break
    else:
        res = loop.run(max_cycles=cycles, sleep=False, on_event=log)
        print(json.dumps(res, indent=2))


def cmd_reset():
    cfg = get_config()
    if os.path.exists(cfg.data_dir):
        for name in ("agent.db", "SUCCESS.json", "final_report.md", "analytics.log"):
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
    a = ap.parse_args()
    if a.status:
        return cmd_status()
    if a.reset:
        return cmd_reset()
    if a.once:
        return cmd_run(1, False)
    if a.daemon:
        return cmd_run(0, True)
    return cmd_run(a.cycles, False)


if __name__ == "__main__":
    main()
