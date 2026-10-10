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
                # muse-glimmer is a reasoning model: it spends tokens on a
                # hidden reasoning pass before any visible content.
                "max_tokens": 2000,
                "stream": True,
            }).encode()
            req = urllib.request.Request(
                self.cfg.llm_base_url.rstrip("/") + "/chat/completions",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.cfg.llm_api_key}"},
            )
            text, usage = self._read_stream(req)
            if not text.strip():
                return ToolResult(ok=False,
                                  output="llm returned empty content "
                                         "(reasoning-only or token limit)",
                                  data={"model": self.cfg.llm_model, "usage": usage})
            data = {"model": self.cfg.llm_model}
            if usage:
                data["usage"] = usage
            return ToolResult(ok=True, output=text, data=data)
        except Exception as e:
            # Strict mode: a configured key must never be silently replaced by
            # mock output. Fail the call so the supervisor records a real
            # failure instead of the loop acting on fabricated content.
            return ToolResult(ok=False,
                              output=f"llm call failed ({self.cfg.llm_model}): {e}",
                              data={"model": self.cfg.llm_model, "error": str(e)})

    def _read_stream(self, req):
        """Read an SSE chat-completion stream (or a plain JSON body if the
        endpoint ignores `stream`). Returns (content, usage)."""
        parts, usage = [], None
        with urllib.request.urlopen(req, timeout=120) as r:
            buf = ""
            while True:
                chunk = r.read(4096)
                if not chunk:
                    break
                buf += chunk.decode(errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return "".join(parts), usage
                    try:
                        d = json.loads(payload)
                    except ValueError:
                        continue
                    usage = d.get("usage") or usage
                    delta = (d.get("choices") or [{}])[0].get("delta") or {}
                    if delta.get("content"):
                        parts.append(delta["content"])
        text = "".join(parts)
        if not text and buf.strip().startswith("{"):
            d = json.loads(buf)
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content") or ""
            usage = d.get("usage") or usage
        return text, usage

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
