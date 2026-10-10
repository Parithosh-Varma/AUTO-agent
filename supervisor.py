"""Independent supervisor. Enforces caps + kill switch + live-money gate.
Agent cannot modify these limits; protected state keys reject agent writes
(see memory.PROTECTED_STATE_KEYS). Limits come from env at process start."""
import os
import time

from memory import TERMINAL_STATUSES


class SupervisorError(Exception):
    pass


class Supervisor:
    def __init__(self, config, store):
        self.cfg = config
        self.store = store
        self.started_at = self.store.get_state("run_started_at", None)
        if not self.started_at:
            self.started_at = time.time()
            self.store.set_state_internal("run_started_at", self.started_at)
        # audit snapshot of effective caps (agent cannot change; writes go via internal)
        self.store.set_state_internal("caps", {
            "max_iterations": config.max_iterations,
            "max_runtime_hours": config.max_runtime_hours,
            "max_llm_calls": config.max_llm_calls,
            "max_tool_calls": config.max_tool_calls,
            "max_llm_spend_usd": config.max_llm_spend_usd,
            "max_infra_spend_usd": config.max_infra_spend_usd,
            "max_spend_usd": config.max_spend_usd,
            "max_consecutive_failures": config.max_consecutive_failures,
            "target_revenue_usd": config.target_revenue_usd,
        })
        if self.store.get_state("experiment_status", None) is None:
            self.store.set_state_internal("experiment_status", "active")
        if self.store.get_state("usage", None) is None:
            self.store.set_state_internal("usage", {
                "llm_calls": 0, "tool_calls": 0,
                "llm_spend_usd": 0.0, "infra_spend_usd": 0.0, "spend_usd": 0.0,
            })

    # --- deadline ---
    def deadline_at(self):
        return float(self.started_at) + float(self.cfg.max_runtime_hours) * 3600.0

    def elapsed_hours(self):
        return (time.time() - float(self.started_at)) / 3600.0

    def remaining_hours(self):
        return max(0.0, float(self.cfg.max_runtime_hours) - self.elapsed_hours())

    def is_expired(self):
        return self.elapsed_hours() >= float(self.cfg.max_runtime_hours)

    def experiment_status(self):
        return self.store.get_state("experiment_status", "active")

    def mark_terminal(self, reason):
        if reason not in TERMINAL_STATUSES:
            raise ValueError(f"unknown terminal reason: {reason}")
        self.store.set_state_internal("experiment_status", reason)
        return reason

    def terminal_reason(self):
        """None while active; otherwise why no new revenue tasks may start."""
        st = self.experiment_status()
        if st in TERMINAL_STATUSES:
            return st
        if self.store.verified_gross() >= self.cfg.target_revenue_usd:
            return "completed_target"
        if self.is_expired():
            return "deadline_expired"
        return None

    # --- usage (only supervisor/loop call this; no tool exposes it) ---
    def _usage(self):
        u = self.store.get_state("usage", None) or {}
        u.setdefault("llm_calls", 0)
        u.setdefault("tool_calls", 0)
        u.setdefault("llm_spend_usd", 0.0)
        u.setdefault("infra_spend_usd", 0.0)
        u.setdefault("spend_usd", 0.0)
        return u

    def add_usage(self, llm_calls=0, tool_calls=0, spend_usd=0.0,
                  llm_spend_usd=None, infra_spend_usd=0.0):
        u = self._usage()
        u["llm_calls"] += llm_calls
        u["tool_calls"] += tool_calls
        if llm_spend_usd is None:
            llm_spend_usd = spend_usd
        u["llm_spend_usd"] += llm_spend_usd
        u["infra_spend_usd"] += infra_spend_usd
        u["spend_usd"] = round(u["llm_spend_usd"] + u["infra_spend_usd"], 6)
        self.store.set_state_internal("usage", u)
        return u

    def check(self, cycle):
        # 1. kill switch (external file; agent has no tool to delete it)
        if os.path.exists(self.cfg.kill_switch_file):
            raise SupervisorError(f"Kill switch present at {self.cfg.kill_switch_file}. Stopping.")
        # 2. terminal experiment state: no new revenue-seeking tasks
        term = self.terminal_reason()
        if term == "completed_target":
            self.mark_terminal("completed_target")
            raise SupervisorError("Revenue target reached. No new revenue-seeking tasks.")
        if term == "deadline_expired":
            self.mark_terminal("deadline_expired")
            raise SupervisorError(
                f"Experiment deadline expired ({self.cfg.max_runtime_hours}h from persisted start). Stopping.")
        if term in TERMINAL_STATUSES:
            raise SupervisorError(f"Experiment terminal ({term}). Stopping.")
        # 3. iterations
        if cycle >= self.cfg.max_iterations:
            raise SupervisorError(f"Max iterations reached ({self.cfg.max_iterations}). Stopping.")
        # 4. budgets (independent hard limits)
        u = self._usage()
        if u["llm_calls"] >= self.cfg.max_llm_calls:
            raise SupervisorError(f"Max LLM calls reached ({self.cfg.max_llm_calls}). Stopping.")
        if u["tool_calls"] >= self.cfg.max_tool_calls:
            raise SupervisorError(f"Max tool calls reached ({self.cfg.max_tool_calls}). Stopping.")
        if u["llm_spend_usd"] >= self.cfg.max_llm_spend_usd:
            raise SupervisorError(f"Max LLM spend reached (${self.cfg.max_llm_spend_usd}). Stopping.")
        if u["infra_spend_usd"] >= self.cfg.max_infra_spend_usd:
            raise SupervisorError(f"Max infra spend reached (${self.cfg.max_infra_spend_usd}). Stopping.")
        if u["spend_usd"] >= self.cfg.max_spend_usd:
            raise SupervisorError(f"Max total spend reached (${self.cfg.max_spend_usd}). Stopping.")
        # 5. execution failures
        fails = self.store.consecutive_failures(self.cfg.max_consecutive_failures)
        if fails >= self.cfg.max_consecutive_failures:
            raise SupervisorError(
                f"Max consecutive failures reached ({fails}/{self.cfg.max_consecutive_failures}). Stopping.")
        return True

    def allow_live_money(self):
        """Live financial ops need BOTH env flag AND approval file. Default: denied."""
        if self.cfg.sandbox_mode:
            return False
        if not self.cfg.approve_live_payments:
            return False
        approval = os.path.join(self.cfg.data_dir, "APPROVE_LIVE")
        return os.path.exists(approval)

    # --- operator intervention (Telegram /status commands route through here) ---
    # These never change caps, usage, deadline, or approvals; pause/resume only
    # toggle the external kill-switch file, stop marks a terminal state.
    def _audit(self, op, source, ok=True, note=""):
        try:
            cycle = self.store.get_state("cycle", 0) or 0
            self.store.log_action(int(cycle), "supervisor", op,
                                  {"source": str(source)[:80]}, ok, note[:500])
            self.store.audit_log(str(source)[:80], op, ok, note[:500])
        except Exception:
            pass

    def pause(self, source="operator"):
        with open(self.cfg.kill_switch_file, "a"):
            pass
        try:
            with open(self.cfg.kill_switch_file, "w") as f:
                f.write(f"paused by {str(source)[:80]} at {time.time()}\n")
        except OSError:
            pass
        self._audit("pause", source, True, "kill switch engaged; deadline unchanged")
        return True

    def resume(self, source="operator"):
        if self.experiment_status() in TERMINAL_STATUSES:
            raise SupervisorError(
                f"Cannot resume: experiment terminal ({self.experiment_status()}).")
        try:
            if os.path.exists(self.cfg.kill_switch_file):
                os.remove(self.cfg.kill_switch_file)
        except OSError as e:
            raise SupervisorError(f"Cannot resume: {e}")
        self._audit("resume", source, True, "kill switch cleared; deadline unchanged")
        return True

    def stop(self, source="operator", reason="stopped_kill_switch"):
        if reason not in TERMINAL_STATUSES:
            reason = "stopped_kill_switch"
        self.mark_terminal(reason)
        try:
            with open(self.cfg.kill_switch_file, "w") as f:
                f.write(f"stopped by {str(source)[:80]} at {time.time()}\n")
        except OSError:
            pass
        self._audit("stop", source, True, f"terminal={reason}; limits/deadline untouched")
        return reason

    def status(self, cycle=0):
        u = self._usage()
        return {
            "cycle": cycle,
            "elapsed_hours": round(self.elapsed_hours(), 3),
            "remaining_hours": round(self.remaining_hours(), 3),
            "deadline_at": self.deadline_at(),
            "experiment_status": self.experiment_status(),
            "terminal_reason": self.terminal_reason(),
            "usage": u,
            "verified_gross": self.store.verified_gross(),
            "target": self.cfg.target_revenue_usd,
            "kill_switch": os.path.exists(self.cfg.kill_switch_file),
            "live_allowed": self.allow_live_money(),
        }
