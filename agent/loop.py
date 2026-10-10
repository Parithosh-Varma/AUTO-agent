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
    def __init__(self, config, store, supervisor, registry, brain, dodo_tool,
                 notifier=None, commands=None):
        self.cfg = config
        self.store = store
        self.sup = supervisor
        self.reg = registry
        self.brain = brain
        self.dodo = dodo_tool
        self.notifier = notifier
        self.commands = commands

    def _poll(self):
        try:
            if self.commands is not None:
                self.commands.poll_and_handle()
        except Exception:
            pass

    def _emit(self, category, summary, severity="info", goal_id=0, details=None, cycle=0):
        try:
            self.store.record_event(category, summary, severity=severity,
                                    goal_id=goal_id, details=details or {},
                                    cycle=cycle)
        except Exception:
            pass

    def _goal_event(self, goal, cycle):
        try:
            last = self.store.get_state("dashboard_last_goal", None)
            if last == goal["id"]:
                return
            self.store.set_state("dashboard_last_goal", goal["id"])
            self._emit("goal", f"goal #{goal['id']} active: {str(goal['description'])[:140]}",
                       goal_id=goal["id"], cycle=cycle,
                       details={"reason": f"score={goal.get('score')} "
                                          f"(novelty={goal.get('novelty')}, feasibility={goal.get('feasibility')}, "
                                          f"usefulness={goal.get('usefulness')}, cost={goal.get('cost')})",
                                "status": goal.get("status")})
        except Exception:
            pass

    def _observe(self, tool, op, res, cycle):
        # Telegram event detectors. Never allowed to break the loop.
        try:
            if self.notifier is None:
                return
            self.notifier.check_missing_credential(tool, op, res.output)
            if not res.ok:
                self.notifier.check_failures(f"{tool}.{op}", res.output, cycle)
            else:
                # success clears tool-failure blockers so recovery is announced
                self.notifier.clear_blocker(f"failures:{tool}.{op}")
                self.notifier.clear_blocker(f"failures:{tool}")
            self.notifier.check_spend(self.sup._usage())
            self.notifier.flush(limit=5)
        except Exception:
            pass

    def cycle(self, cycle):
        self._poll()
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
        self._goal_event(goal, cycle)
        self._emit("decision", f"cycle {cycle}: {tool}.{op} for goal #{goal['id']}",
                   goal_id=goal["id"], cycle=cycle,
                   details={"tool": tool, "op": op, "gross_before": gross_before})
        res = self.reg.call(cycle, tool, op, args)
        # split metering: LLM vs infra vs generic tool (supervisor enforces each)
        llm_spend = LLM_COST_PER_CALL if tool == "llm" else 0.0
        infra_spend = INFRA_COST_PER_DEPLOY if (tool == "cloudflare" and op == "deploy") else 0.0
        generic = 0.0 if (tool == "llm" or infra_spend) else TOOL_COST_PER_CALL
        self.sup.add_usage(llm_calls=1 if tool == "llm" else 0,
                           tool_calls=1,
                           llm_spend_usd=llm_spend + generic,
                           infra_spend_usd=infra_spend)
        self._observe(tool, op, res, cycle)
        try:
            self.store.snapshot_usage(cycle, self.sup._usage())
        except Exception:
            pass
        if not res.ok:
            fails = self.store.consecutive_failures(25)
            self._emit("error", f"cycle {cycle}: {tool}.{op} failed (streak {fails})",
                       severity="error", goal_id=goal["id"], cycle=cycle,
                       details={"tool": tool, "op": op, "retries": fails,
                                "output": (res.output or "")[:300]})

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
        requeued = self.store.requeue_stuck()
        cycle = int(self.store.get_state("cycle", 0) or 0)
        start = cycle
        self._emit("checkpoint", f"run started at cycle {cycle} (requeued {requeued} jobs)",
                   cycle=cycle, details={"requeued": requeued, "start_cycle": start})
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
                self._emit("checkpoint", f"run stopped: {msg[:200]}",
                           severity="warn", cycle=cycle, details={"reason": msg[:300]})
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
                    self._emit("checkpoint", "target reached; success report written",
                               cycle=cycle, details={"gross": out["gross"]})
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
