"""Continuous Observe → Think → Act → Evaluate → Repeat loop."""
import time

from supervisor import SupervisorError


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
        gross_before = self.store.verified_gross()
        goal = self.brain.pick_goal()
        if not goal:
            self.store.enqueue("reflection", {"event": "no_goal", "cycle": cycle})
            return {"cycle": cycle, "ok": False, "msg": "no goal available"}

        # drain one background job opportunistically
        job = self.store.dequeue()
        if job:
            self.store.complete_job(job["id"], ok=True)

        tool, op, args = self.brain.plan_next(goal, {"cycle": cycle, "gross": gross_before})
        res = self.reg.call(cycle, tool, op, args)
        # usage metering (rough: $0.002/LLM call + $0.0001/tool call)
        self.sup.add_usage(llm_calls=1 if tool == "llm" else 0,
                           tool_calls=1,
                           spend_usd=0.002 if tool == "llm" else 0.0001)

        # periodic hard verification (every 3 cycles + after monetize ops)
        verified = gross_before
        if cycle % 3 == 2 or (tool == "dodo"):
            v = self.reg.call(cycle, "dodo", "verify_payments", {})
            verified = v.data.get("verified_gross", gross_before)

        note = self.brain.reflect(goal, verified, self.cfg.target_revenue_usd)
        done = verified >= self.cfg.target_revenue_usd
        return {"cycle": cycle, "ok": res.ok, "tool": f"{tool}.{op}",
                "output": res.output[:300], "gross": verified, "goal": goal["id"],
                "note": note, "done": done}

    def run(self, max_cycles=None, sleep=True, on_event=None):
        cycle = int(self.store.get_state("cycle", 0) or 0)
        start = cycle
        limit = max_cycles if max_cycles is not None else self.cfg.max_iterations
        while cycle - start < limit:
            try:
                out = self.cycle(cycle)
            except SupervisorError as e:
                if on_event:
                    on_event(f"[supervisor] {e}")
                return {"stopped": str(e), "cycle": cycle,
                        "gross": self.store.verified_gross()}
            if on_event:
                on_event(f"[{cycle}] {out.get('tool')} :: {out.get('output','')[:120]} "
                         f"| gross=${out.get('gross',0):.2f} :: {out.get('note')}")
            cycle += 1
            self.store.set_state("cycle", cycle)
            if out.get("done"):
                self._write_success()
                return {"success": True, "cycle": cycle, "gross": out["gross"]}
            if sleep and self.cfg.cycle_sleep_seconds > 0:
                time.sleep(self.cfg.cycle_sleep_seconds)
        return {"completed_cycles": cycle - start, "cycle": cycle,
                "gross": self.store.verified_gross()}

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
