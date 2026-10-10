"""Independent supervisor. Enforces caps + kill switch + live-money gate.
Agent cannot modify these limits; they come from env at process start."""
import os
import time


class SupervisorError(Exception):
    pass


class Supervisor:
    def __init__(self, config, store):
        self.cfg = config
        self.store = store
        self.started_at = self.store.get_state("run_started_at", None)
        if not self.started_at:
            self.started_at = time.time()
            self.store.set_state("run_started_at", self.started_at)
        self.store.set_state("caps", {
            "max_iterations": config.max_iterations,
            "max_runtime_hours": config.max_runtime_hours,
            "max_llm_calls": config.max_llm_calls,
            "max_spend_usd": config.max_spend_usd,
            "target_revenue_usd": config.target_revenue_usd,
        })

    def _usage(self):
        return self.store.get_state("usage", {"llm_calls": 0, "tool_calls": 0, "spend_usd": 0.0})

    def add_usage(self, llm_calls=0, tool_calls=0, spend_usd=0.0):
        u = self._usage()
        u["llm_calls"] += llm_calls
        u["tool_calls"] += tool_calls
        u["spend_usd"] += spend_usd
        self.store.set_state("usage", u)
        return u

    def check(self, cycle):
        # 1. kill switch (external file; agent has no tool to delete it)
        if os.path.exists(self.cfg.kill_switch_file):
            raise SupervisorError(f"Kill switch present at {self.cfg.kill_switch_file}. Stopping.")
        # 2. iterations
        if cycle >= self.cfg.max_iterations:
            raise SupervisorError(f"Max iterations reached ({self.cfg.max_iterations}). Stopping.")
        # 3. runtime
        elapsed_h = (time.time() - self.started_at) / 3600.0
        if elapsed_h >= self.cfg.max_runtime_hours:
            raise SupervisorError(f"Max runtime reached ({self.cfg.max_runtime_hours}h). Stopping.")
        # 4. budgets
        u = self._usage()
        if u["llm_calls"] >= self.cfg.max_llm_calls:
            raise SupervisorError(f"Max LLM calls reached ({self.cfg.max_llm_calls}). Stopping.")
        if u["spend_usd"] >= self.cfg.max_spend_usd:
            raise SupervisorError(f"Max spend reached (${self.cfg.max_spend_usd}). Stopping.")
        return True

    def allow_live_money(self):
        """Live financial ops need BOTH env flag AND approval file. Default: denied."""
        if self.cfg.sandbox_mode:
            return False
        if not self.cfg.approve_live_payments:
            return False
        approval = os.path.join(self.cfg.data_dir, "APPROVE_LIVE")
        return os.path.exists(approval)

    def status(self, cycle=0):
        u = self._usage()
        elapsed_h = (time.time() - self.started_at) / 3600.0
        return {
            "cycle": cycle,
            "elapsed_hours": round(elapsed_h, 3),
            "usage": u,
            "verified_gross": self.store.verified_gross(),
            "target": self.cfg.target_revenue_usd,
            "kill_switch": os.path.exists(self.cfg.kill_switch_file),
            "live_allowed": self.allow_live_money(),
        }
