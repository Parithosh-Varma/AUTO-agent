"""Web search (Brave). Mock when key absent."""
import json
import urllib.parse
import urllib.request

from .base import ToolResult


class SearchTool:
    name = "search"

    def __init__(self, config):
        self.cfg = config

    def run(self, op, args):
        if op == "query":
            return self.query(args.get("q", ""))
        return ToolResult(ok=False, output=f"unknown op {op}")

    def query(self, q):
        if not self.cfg.brave_api_key:
            return ToolResult(ok=True, output=f"[dry-run] search '{q}': indie devs pay for uptime checks, status pages, csv tools.",
                              data={"dry_run": True})
        try:
            url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "count": 5})
            req = urllib.request.Request(url, headers={"X-Subscription-Token": self.cfg.brave_api_key})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            items = (data.get("web") or {}).get("results", [])[:5]
            lines = [f"- {i.get('title')}: {i.get('url')}" for i in items]
            return ToolResult(ok=True, output="\n".join(lines) or "no results", data={"count": len(lines)})
        except Exception as e:
            return ToolResult(ok=False, output=f"search failed: {e}")
