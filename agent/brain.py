"""Brain: goal generation, ranking, planning, reflection.
Uses LLM when available, deterministic heuristics otherwise."""
import json


class Brain:
    def __init__(self, llm_tool, store):
        self.llm = llm_tool
        self.store = store

    def score(self, novelty, feasibility, usefulness, cost):
        return round(0.25 * novelty + 0.3 * feasibility + 0.35 * usefulness - 0.2 * cost, 2)

    def generate_goals(self):
        res = self.llm.complete(
            "Generate 3 candidate micro-product goals under $15 that a solo agent can ship "
            "on Cloudflare + GitHub and sell via Dodo. Reply as JSON list with keys "
            "idea, novelty(0-10), feasibility(0-10), usefulness(0-10), cost(0-10).",
            system="You are a pragmatic product strategist. Reply JSON only.")
        try:
            cands = json.loads(res.output)
            if isinstance(cands, dict) and "ideas" in cands:
                cands = cands["ideas"]
        except Exception:
            cands = []
        if not cands:
            cands = [
                {"idea": "Tiny status-page starter on Cloudflare Workers ($9 one-time)",
                 "novelty": 6, "feasibility": 9, "usefulness": 8, "cost": 2},
                {"idea": "CSV-to-chart API with free tier + $5/mo pro via Dodo",
                 "novelty": 7, "feasibility": 8, "usefulness": 8, "cost": 3},
                {"idea": "Developer uptime-check worker template ($12 one-time)",
                 "novelty": 5, "feasibility": 9, "usefulness": 7, "cost": 1},
            ]
        ids = []
        for c in cands[:5]:
            s = self.score(c.get("novelty", 5), c.get("feasibility", 5),
                           c.get("usefulness", 5), c.get("cost", 3))
            gid = self.store.add_goal(c.get("idea", "unnamed"), c.get("novelty", 5),
                                      c.get("feasibility", 5), c.get("usefulness", 5),
                                      c.get("cost", 3), s)
            ids.append(gid)
        return ids

    def pick_goal(self):
        from memory import TERMINAL_STATUSES
        st = self.store.get_state("experiment_status", "active")
        if st in TERMINAL_STATUSES:
            return None
        active = self.store.active_goal()
        if active:
            return active
        goals = self.store.list_goals(status="proposed")
        if not goals:
            self.generate_goals()
            goals = self.store.list_goals(status="proposed")
        if not goals:
            return None
        best = max(goals, key=lambda g: g["score"])
        self.store.update_goal(best["id"], status="active")
        return best

    def plan_next(self, goal, cycle_state):
        """Return (tool, op, args) tuple. Heuristic state machine; LLM advises."""
        fails = self.store.consecutive_failures(5)
        if fails >= 4:
            return ("build", "write_file",
                    {"path": f"pivot_notes_{cycle_state['cycle']}.txt",
                     "content": f"Stalled ({fails} fails). Consider abandoning goal {goal['id']}."})
        # simple progression: research -> build -> deploy -> monetize -> measure
        stage = self.store.get_state(f"stage_{goal['id']}", "research")
        if stage == "research":
            self.store.set_state(f"stage_{goal['id']}", "build")
            return ("search", "query", {"q": goal["description"][:120]})
        if stage == "build":
            self.store.set_state(f"stage_{goal['id']}", "deploy")
            return ("build", "build_landing",
                    {"goal_id": goal["id"], "name": f"product-goal-{goal['id']}",
                     "pitch": goal["description"][:200], "price": "$9 one-time via Dodo",
                     "checkout": "#"})
        if stage == "deploy":
            self.store.set_state(f"stage_{goal['id']}", "monetize")
            return ("cloudflare", "deploy",
                    {"name": f"product-goal-{goal['id']}",
                     "content": f"<h1>Goal {goal['id']}</h1><p>{goal['description']}</p>"})
        if stage == "monetize":
            self.store.set_state(f"stage_{goal['id']}", "measure")
            return ("dodo", "create_product", {"name": f"product-goal-{goal['id']}", "price_cents": 900})
        # measure / iterate
        if cycle_state["cycle"] % 3 == 0:
            return ("dodo", "verify_payments", {})
        if cycle_state["cycle"] % 3 == 1:
            return ("analytics", "track", {"event": "cycle_check", "props": {"goal": goal["id"]}})
        return ("analytics", "funnel", {})

    def reflect(self, goal, verified_gross, target):
        fails = self.store.consecutive_failures(5)
        if fails >= 5:
            self.store.update_goal(goal["id"], status="abandoned")
            self.store.enqueue("reflection", {"event": "abandon", "goal": goal["id"], "fails": fails})
            return f"abandoned goal {goal['id']} after {fails} consecutive failures"
        if verified_gross >= target:
            self.store.update_goal(goal["id"], status="succeeded")
            return "target reached"
        return "continue"
