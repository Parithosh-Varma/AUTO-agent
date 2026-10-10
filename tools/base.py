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
            return res
        try:
            res = tool.run(op, args)
        except Exception as e:  # never crash loop on tool error
            res = ToolResult(ok=False, output=f"{tool_name}.{op} exception: {e}")
        self.store.log_action(cycle, tool_name, op, args, res.ok, res.output[:2000])
        return res
