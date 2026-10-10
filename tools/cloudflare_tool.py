"""Cloudflare tool. Workers/D1/R2/Queues abstraction.
Real API when CF_API_TOKEN set, else local simulation (real files + SQLite)."""
import json
import os
import urllib.request

from .base import ToolResult


class CloudflareTool:
    name = "cloudflare"

    def __init__(self, config, store):
        self.cfg = config
        self.store = store

    def run(self, op, args):
        if op == "deploy":
            return self._deploy(args)
        if op == "r2_put":
            return self._r2_put(args)
        if op == "queue_send":
            self.store.enqueue(args.get("kind", "job"), args.get("payload", {}),
                               dedupe_key=args.get("dedupe_key"))
            return ToolResult(ok=True, output="queued")
        if op == "d1_query":
            return ToolResult(ok=True, output=f"verified_gross={self.store.verified_gross()}",
                              data={"verified_gross": self.store.verified_gross()})
        return ToolResult(ok=False, output=f"unknown op {op}")

    def _deploy(self, args):
        name = args.get("name", "auto-product")
        content = args.get("content", "<h1>auto-agent product</h1>")
        # always write local artifact (real, inspectable)
        out = os.path.join(self.cfg.data_dir, "artifacts", name)
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "index.html"), "w") as f:
            f.write(content)
        if not self.cfg.cf_api_token:
            url = f"file://{out}/index.html (dry-run; set CF_API_TOKEN for real Workers deploy)"
            return ToolResult(ok=True, output=f"[dry-run] deployed {name} -> {url}",
                              data={"dry_run": True, "url": url})
        try:
            # Minimal Workers upload placeholder: real projects should use wrangler;
            # here we verify token works by listing scripts.
            req = urllib.request.Request(
                f"https://api.cloudflare.com/client/v4/accounts/{self.cfg.cf_account_id}/workers/scripts",
                headers={"Authorization": f"Bearer {self.cfg.cf_api_token}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                json.loads(r.read().decode())
            return ToolResult(ok=True, output=f"CF token valid. Local build at {out}; run wrangler deploy for {name}.",
                              data={"local_path": out})
        except Exception as e:
            return ToolResult(ok=False, output=f"cloudflare deploy failed: {e}")

    def _r2_put(self, args):
        key = args.get("key", "artifact.txt")
        content = args.get("content", "")
        path = os.path.join(self.cfg.data_dir, "artifacts", key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return ToolResult(ok=True, output=f"stored {key} ({len(content)} bytes)")
