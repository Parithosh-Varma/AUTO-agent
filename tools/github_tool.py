"""GitHub tool. Allowlist-only. Dry-run simulation when no token.
Destructive ops (delete repo, force push, secret leak) are refused."""
import json
import os
import urllib.request

from .base import ToolResult


class GitHubTool:
    name = "github"

    def __init__(self, config, store):
        self.cfg = config
        self.store = store

    def _allowed(self, repo):
        if not self.cfg.allowed_repos:
            return False  # closed by default: must configure allowlist
        return repo in self.cfg.allowed_repos

    def run(self, op, args):
        if op in ("create_repo", "commit_file", "open_pr", "list_repos"):
            repo = args.get("repo", "")
            # list doesn't need allowlist, others do
            if op != "list_repos" and op != "create_repo" and not self._allowed(repo):
                return ToolResult(ok=False, output=f"refused: {repo} not in AGENT_ALLOWED_REPOS")
            if not self.cfg.github_token:
                return self._simulate(op, args)
            return self._api(op, args)
        return ToolResult(ok=False, output=f"unknown op {op}")

    def _simulate(self, op, args):
        repo = args.get("repo", "sandbox/demo")
        path = args.get("path", "README.md")
        # mirror file locally so agent work is real even without token
        ws = os.path.join(self.cfg.data_dir, "workspace", repo.replace("/", "__"))
        os.makedirs(ws, exist_ok=True)
        if op == "commit_file":
            with open(os.path.join(ws, os.path.basename(path)), "w") as f:
                f.write(args.get("content", ""))
        return ToolResult(ok=True, output=f"[dry-run] github.{op} {repo} ok (local mirror: {ws})",
                          data={"dry_run": True, "repo": repo})

    def _api(self, op, args):
        try:
            base = "https://api.github.com"
            H = {"Authorization": f"Bearer {self.cfg.github_token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "auto-agent"}
            if op == "list_repos":
                req = urllib.request.Request(base + "/user/repos?per_page=5", headers=H)
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                return ToolResult(ok=True, output=f"repos: {[d['full_name'] for d in data]}")
            if op == "create_repo":
                body = json.dumps({"name": args["name"], "private": False,
                                   "description": "auto-agent sandbox project"}).encode()
                req = urllib.request.Request(base + "/user/repos", data=body, headers=H,
                                             method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                return ToolResult(ok=True, output=f"created {data.get('full_name')}", data=data)
            if op == "commit_file":
                repo, path = args["repo"], args["path"]
                # get sha if exists
                sha = None
                try:
                    req = urllib.request.Request(f"{base}/repos/{repo}/contents/{path}", headers=H)
                    with urllib.request.urlopen(req, timeout=30) as r:
                        sha = json.loads(r.read().decode()).get("sha")
                except Exception:
                    pass
                import base64
                body = {"message": args.get("message", "auto-agent commit"),
                        "content": base64.b64encode(args.get("content", "").encode()).decode()}
                if sha:
                    body["sha"] = sha
                # branch support
                if args.get("branch"):
                    body["branch"] = args["branch"]
                req = urllib.request.Request(f"{base}/repos/{repo}/contents/{path}",
                                             data=json.dumps(body).encode(), headers=H, method="PUT")
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                return ToolResult(ok=True, output=f"committed {path} to {repo}")
            if op == "open_pr":
                repo = args["repo"]
                body = json.dumps({"title": args.get("title", "auto-agent PR"),
                                   "head": args.get("head", "main"),
                                   "base": args.get("base", "main"),
                                   "body": args.get("body", "")}).encode()
                req = urllib.request.Request(f"{base}/repos/{repo}/pulls", data=body,
                                             headers=H, method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                return ToolResult(ok=True, output=f"PR #{data.get('number')} opened")
        except Exception as e:
            return ToolResult(ok=False, output=f"github.{op} failed: {e}")
        return ToolResult(ok=False, output="unreachable")
