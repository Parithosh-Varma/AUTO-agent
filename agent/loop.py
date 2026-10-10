"""Continuous Observe → Think → Act → Evaluate → Repeat loop.

Seven-day rules:
- Never starts new revenue-seeking work after target reached or deadline expired.
- Crash-safe: cycle + usage + deadline persist in SQLite; claimed jobs are requeued on start.
"""
import time

from supervisor import SupervisorError

LLM_COST_PER_CALL = 0.002
TOOL_COST_PER_CALL = 0.0001
INFRA_COST_PER_DEPLOY = 0.01


class AutonomousLoop:
    def __init__(self, config, store, supervisor, registry, brain, dodo_tool):
        self.cfg = config
        self.store = store
        self.sup = supervisor
        self.reg = registry
        self.brain = brain
        self.dodo = dodo_tool

    def cycle(self, cycle):
        self.sup.check(cycle)
        # terminal guard: no new revenue-seeking tasks after target/deadline
        term = self.sup.terminal_reason()
        if term is not None:
            return {"cycle": cycle, "ok": False, "terminal": term,
                    "gross": self.store.verified_gross(), "done": True, "note": term}
        gross_before = self.store.verified_gross()
        goal = self.brain.pick_goal()
        if not goal:
            self.store.enqueue("reflection", {"event": "no_goal", "cycle": cycle})
            return {"cycle": cycle, "ok": False, "msg": "no goal available (terminal or empty)"}

        # drain one background job opportunistically
        job = self.store.dequeue()
        if job:
            self.store.complete_job(job["id"], ok=True)

        tool, op, args = self.brain.plan_next(goal, {"cycle": cycle, "gross": gross_before})
        res = self.reg.call(cycle, tool, op, args)
        # split metering: LLM vs infra vs generic tool (supervisor enforces each)
        llm_spend = LLM_COST_PER_CALL if tool == "llm" else 0.0
        infra_spend = INFRA_COST_PER_DEPLOY if (tool == "cloudflare" and op == "deploy") else 0.0
        generic = 0.0 if (tool == "llm" or infra_spend) else TOOL_COST_PER_CALL
        self.sup.add_usage(llm_calls=1 if tool == "llm" else 0,
                           tool_calls=1,
                           llm_spend_usd=llm_spend + generic,
                           infra_spend_usd=infra_spend)

        # periodic hard verification (every 3 cycles + after monetize ops)
        verified = gross_before
        if cycle % 3 == 2 or (tool == "dodo"):
            v = self.reg.call(cycle, "dodo", "verify_payments", {})
            verified = v.data.get("verified_gross", gross_before)

        note = self.brain.reflect(goal, verified, self.cfg.target_revenue_usd)
        done = verified >= self.cfg.target_revenue_usd
        if done:
            try:
                self.sup.mark_terminal("completed_target")
            except ValueError:
                pass
        return {"cycle": cycle, "ok": res.ok, "tool": f"{tool}.{op}",
                "output": res.output[:300], "gross": verified, "goal": goal["id"],
                "note": note, "done": done}

    def run(self, max_cycles=None, sleep=True, on_event=None):
        self.store.requeue_stuck()
        cycle = int(self.store.get_state("cycle", 0) or 0)
        start = cycle
        limit = max_cycles if max_cycles is not None else self.cfg.max_iterations
        while cycle - start < limit:
            try:
                out = self.cycle(cycle)
            except SupervisorError as e:
                if on_event:
                    on_event(f"[supervisor] {e}")
                msg = str(e)
                if "deadline" in msg.lower():
                    try:
                        self.sup.mark_terminal("deadline_expired")
                    except ValueError:
                        pass
                return {"stopped": msg, "cycle": cycle,
                        "gross": self.store.verified_gross(),
                        "experiment_status": self.sup.experiment_status()}
            if on_event:
                on_event(f"[{cycle}] {out.get('tool')} :: {out.get('output','')[:120]} "
                         f"| gross=${out.get('gross',0):.2f} :: {out.get('note')}")
            cycle += 1
            self.store.set_state("cycle", cycle)
            if out.get("done"):
                if out.get("gross", 0) >= self.cfg.target_revenue_usd:
                    self._write_success()
                    return {"success": True, "cycle": cycle, "gross": out["gross"],
                            "experiment_status": self.sup.experiment_status()}
                # terminal but not success (deadline): stop without new tasks
                return {"stopped": out.get("terminal", "terminal"), "cycle": cycle,
                        "gross": out.get("gross", 0),
                        "experiment_status": self.sup.experiment_status()}
            if sleep and self.cfg.cycle_sleep_seconds > 0:
                time.sleep(self.cfg.cycle_sleep_seconds)
        return {"completed_cycles": cycle - start, "cycle": cycle,
                "gross": self.store.verified_gross(),
                "experiment_status": self.sup.experiment_status()}

    def _write_success(self):
        import json
        import os
        gross = self.store.verified_gross()
        usage = self.store.get_state("usage", {})
        goals = self.store.list_goals()
        report = {
            "verified_gross_usd": gross,
            "target_usd": self.cfg.target_revenue_usd,
            "usage": usage,
            "goals": goals,
        }
        with open(os.path.join(self.cfg.data_dir, "SUCCESS.json"), "w") as f:
            json.dump(report, f, indent=2)
        with open(os.path.join(self.cfg.data_dir, "final_report.md"), "w") as f:
            f.write(f"# Final report\n\nVerified gross: ${gross:.2f}\nUsage: {usage}\n"
                    f"Goals: {len(goals)}\n")
