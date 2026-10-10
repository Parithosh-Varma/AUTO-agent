"""Validated tool-execution layer. Holds credentials; exposes narrow ops.
Every call is audit-logged to memory.actions."""
from dataclasses import dataclass


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}


class Registry:
    def __init__(self, store):
        self.store = store
        self.tools = {}

    def register(self, name, tool):
        self.tools[name] = tool

    def call(self, cycle, tool_name, op, args):
        tool = self.tools.get(tool_name)
        if not tool:
            res = ToolResult(ok=False, output=f"unknown tool: {tool_name}")
            self.store.log_action(cycle, tool_name, op, args, False, res.output)
            self._event(cycle, "tool", res, tool_name, op, 0)
            return res
        try:
            res = tool.run(op, args)
        except Exception as e:  # never crash loop on tool error
            res = ToolResult(ok=False, output=f"{tool_name}.{op} exception: {e}")
        self.store.log_action(cycle, tool_name, op, args, res.ok, res.output[:2000])
        self._event(cycle, "tool", res, tool_name, op, 0)
        if tool_name == "cloudflare" and op == "deploy":
            self._event(cycle, "deployment", res, tool_name, op, 0)
        return res

    def _event(self, cycle, category, res, tool_name, op, goal_id):
        try:
            import time as _time
            # Each tool execution is its own occurrence: unique key per call.
            # (Dedupe applies to notification-level alerts, not raw executions.)
            key = f"call:{cycle}:{category}:{tool_name}:{op}:{_time.time_ns()}"
            self.store.record_event(
                category, f"{tool_name}.{op} {'ok' if res.ok else 'FAILED'}",
                severity="info" if res.ok else "error",
                goal_id=goal_id, cycle=cycle,
                details={"tool": tool_name, "op": op, "ok": res.ok,
                         "output": (res.output or "")[:400]},
                idempotency_key=key)
        except Exception:
            pass
