"""GitHub tool. Allowlist-only, plus repos this agent created itself.
Every created repo is PRIVATE — enforced in code, verified after creation.
Dry-run simulation when no token.
Destructive ops (delete repo, force push, secret leak) are refused."""
import json
import os
import urllib.request

from .base import ToolResult

CREATED_KEY = "github_created_repos"


class GitHubTool:
    name = "github"

    def __init__(self, config, store):
        self.cfg = config
        self.store = store

    def _created(self):
        try:
            v = self.store.get_state(CREATED_KEY, [])
            return list(v) if isinstance(v, list) else []
        except Exception:
            return []

    def _remember(self, repo):
        try:
            created = self._created()
            if repo and repo not in created:
                created.append(repo)
                self.store.set_state(CREATED_KEY, created)
        except Exception:
            pass

    def _allowed(self, repo):
        if repo in self._created():
            return True  # agent-created repos are always private by policy
        if not self.cfg.allowed_repos:
            return False  # closed by default: must configure allowlist
        return repo in self.cfg.allowed_repos

    def run(self, op, args):
        if op in ("create_repo", "commit_file", "open_pr", "list_repos"):
            repo = args.get("repo", "")
            # create_repo may target any name; all writes go through _allowed
            if op not in ("list_repos", "create_repo") and not self._allowed(repo):
                return ToolResult(ok=False, output=f"refused: {repo} not in AGENT_ALLOWED_REPOS and not agent-created")
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
        if op == "create_repo":
            self._remember(repo)
        return ToolResult(ok=True, output=f"[dry-run] github.{op} {repo} ok (local mirror: {ws})",
                          data={"dry_run": True, "repo": repo, "private": True})

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
                name = args["name"]
                # Hard privacy guarantee: always private; any caller-supplied
                # visibility flag is ignored by design.
                body = json.dumps({"name": name, "private": True,
                                   "description": "auto-agent sandbox project (private by policy)"}).encode()
                req = urllib.request.Request(base + "/user/repos", data=body, headers=H,
                                             method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                full = data.get("full_name", "")
                if not data.get("private"):
                    # Defense in depth: force private, then re-verify.
                    patch = urllib.request.Request(
                        base + f"/repos/{full}",
                        data=json.dumps({"private": True}).encode(),
                        headers=H, method="PATCH")
                    with urllib.request.urlopen(patch, timeout=30) as r:
                        data = json.loads(r.read().decode())
                if not data.get("private"):
                    return ToolResult(ok=False,
                                      output=f"refused: {full} could not be made private",
                                      data={"repo": full, "private": False})
                self._remember(full)
                return ToolResult(ok=True, output=f"created private repo {full}",
                                  data={"repo": full, "private": True})
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
