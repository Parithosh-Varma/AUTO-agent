"""GitHub tool tests: repos are always private, agent-created repos are
writable, unknown repos stay refused. API layer is faked; no network."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory import Store
from config import get_config
from tools.github_tool import GitHubTool, CREATED_KEY


class FakeResponse:
    def __init__(self, payload):
        self._buf = io.BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    """Replaces urllib.request.urlopen; records requests per endpoint."""
    def __init__(self, create_private=True):
        self.create_private = create_private
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req.get_method(), req.full_url,
                              json.loads(req.data.decode()) if req.data else None))
        if req.full_url.endswith("/user/repos"):
            return FakeResponse({"full_name": "bot/demo", "private": self.create_private})
        if "/repos/" in req.full_url and req.get_method() == "PATCH":
            return FakeResponse({"full_name": "bot/demo", "private": True})
        return FakeResponse({"ok": True})


class TestGitHubTool(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.dir = tempfile.mkdtemp()
        os.environ["AGENT_GITHUB_TOKEN"] = "tok-test"
        os.environ["GITHUB_TOKEN"] = ""  # host env may export a real one
        os.environ["AGENT_ALLOWED_REPOS"] = ""
        self.cfg = get_config()
        self.cfg = type(self.cfg)(**{**self.cfg.__dict__, "data_dir": self.dir})
        self.store = Store(self.dir)
        self.tool = GitHubTool(self.cfg, self.store)
        self._orig = urllib.request.urlopen

    def tearDown(self):
        urllib.request.urlopen = self._orig
        os.environ.clear()
        os.environ.update(self.saved)

    def test_create_repo_is_always_private_even_if_args_say_otherwise(self):
        fake = FakeGitHub()
        urllib.request.urlopen = fake
        r = self.tool.run("create_repo", {"name": "demo", "private": False})
        self.assertTrue(r.ok)
        method, url, body = fake.requests[0]
        self.assertEqual(method, "POST")
        self.assertIs(body["private"], True)
        self.assertNotIn("public", body)

    def test_public_response_triggers_forced_patch_then_verifies(self):
        fake = FakeGitHub(create_private=False)
        urllib.request.urlopen = fake
        r = self.tool.run("create_repo", {"name": "demo"})
        self.assertTrue(r.ok, r.output)
        methods = [(m, u, b) for m, u, b in fake.requests]
        patch = [x for x in methods if x[0] == "PATCH"]
        self.assertEqual(len(patch), 1)
        self.assertIs(patch[0][2]["private"], True)

    def test_created_repo_is_writable_without_allowlist(self):
        fake = FakeGitHub()
        urllib.request.urlopen = fake
        self.tool.run("create_repo", {"name": "demo"})
        self.assertIn("bot/demo", self.store.get_state(CREATED_KEY))
        r = self.tool.run("commit_file", {"repo": "bot/demo", "path": "a.txt",
                                          "content": "hi"})
        self.assertTrue(r.ok, r.output)

    def test_unknown_repo_still_refused(self):
        urllib.request.urlopen = FakeGitHub()
        r = self.tool.run("commit_file", {"repo": "someone/else", "path": "a.txt",
                                          "content": "hi"})
        self.assertFalse(r.ok)
        self.assertIn("refused", r.output)

    def test_dry_run_without_token_remembers_created_repo(self):
        os.environ["AGENT_GITHUB_TOKEN"] = ""
        self.cfg = get_config()
        self.cfg = type(self.cfg)(**{**self.cfg.__dict__, "data_dir": self.dir})
        tool = GitHubTool(self.cfg, self.store)
        r = tool.run("create_repo", {"name": "demo"})
        self.assertTrue(r.ok)
        self.assertTrue(r.data.get("dry_run"))
        r2 = tool.run("commit_file", {"repo": "sandbox/demo", "path": "a.txt",
                                      "content": "x"})
        self.assertTrue(r2.ok, r2.output)


if __name__ == "__main__":
    unittest.main()
