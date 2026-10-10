"""LLM tool. OpenAI-compatible endpoint via stdlib; mock fallback when no key.
Tracks call count for supervisor budgets (caller must call supervisor.add_usage)."""
import json
import urllib.request

from .base import ToolResult


class LLMTool:
    name = "llm"

    def __init__(self, config):
        self.cfg = config

    def run(self, op, args):
        if op == "complete":
            return self.complete(args.get("prompt", ""), args.get("system", ""))
        return ToolResult(ok=False, output=f"unknown op {op}")

    def complete(self, prompt, system="You are a pragmatic autonomous product engineer."):
        if not self.cfg.llm_api_key:
            return ToolResult(ok=True, output=self._mock(prompt),
                              data={"mock": True, "model": "mock"})
        try:
            body = json.dumps({
                "model": self.cfg.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 800,
            }).encode()
            req = urllib.request.Request(
                self.cfg.llm_base_url.rstrip("/") + "/chat/completions",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.cfg.llm_api_key}"},
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode())
            text = payload["choices"][0]["message"]["content"]
            return ToolResult(ok=True, output=text, data={"model": self.cfg.llm_model})
        except Exception as e:
            # Strict mode: a configured key must never be silently replaced by
            # mock output. Fail the call so the supervisor records a real
            # failure instead of the loop acting on fabricated content.
            return ToolResult(ok=False,
                              output=f"llm call failed ({self.cfg.llm_model}): {e}",
                              data={"model": self.cfg.llm_model, "error": str(e)})

    def _mock(self, prompt):
        p = prompt.lower()
        if "goal" in p or "idea" in p:
            return json.dumps([
                {"idea": "Tiny status-page starter on Cloudflare Workers ($9 one-time)",
                 "novelty": 6, "feasibility": 9, "usefulness": 8, "cost": 2},
                {"idea": "CSV-to-chart API with free tier + $5/mo pro via Dodo",
                 "novelty": 7, "feasibility": 8, "usefulness": 8, "cost": 3},
                {"idea": "Developer uptime-check worker template ($12 one-time)",
                 "novelty": 5, "feasibility": 9, "usefulness": 7, "cost": 1},
            ])
        if "plan" in p or "next action" in p:
            return json.dumps({"next_tool": "scaffold", "next_op": "build_landing",
                               "reason": "Need a shippable prototype before marketing."})
        return "mock-llm: continue building, testing, and measuring. Keep diffs small."
