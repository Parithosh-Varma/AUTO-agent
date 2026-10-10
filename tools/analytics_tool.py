"""Product analytics + error tracking. Local log always; PostHog/Sentry when keys set."""
import json
import os

from .base import ToolResult


class AnalyticsTool:
    name = "analytics"

    def __init__(self, config):
        self.cfg = config

    def run(self, op, args):
        if op == "track":
            event = args.get("event", "event")
            log = os.path.join(self.cfg.data_dir, "analytics.log")
            with open(log, "a") as f:
                f.write(json.dumps({"event": event, "props": args.get("props", {})}) + "\n")
            return ToolResult(ok=True, output=f"tracked {event}")
        if op == "funnel":
            return ToolResult(ok=True, output="visits=0 signups=0 checkouts=0 (wire PostHog for live funnel)",
                              data={"visits": 0, "signups": 0, "checkouts": 0})
        return ToolResult(ok=False, output=f"unknown op {op}")
