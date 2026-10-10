"""Telegram Bot API sender + updates reader (official API, stdlib only).

Security:
- Token comes from config (env TELEGRAMBOTTOKEN / file). It is NEVER logged,
  never included in ToolResult output, and never echoed in errors (API URLs
  contain the token, so failures return a generic message).
- Delivery counts ONLY when the API responds {"ok": true}. Anything else is a
  recorded failure with backoff, never a success.
"""
import json
import urllib.parse
import urllib.request

from .base import ToolResult

API_BASE = "https://api.telegram.org"
REDACTED = "***"


def redact(text, token):
    if not text or not token:
        return text or ""
    return str(text).replace(token, REDACTED)


class TelegramTool:
    name = "telegram"

    def __init__(self, config, store=None, transport=None):
        self.cfg = config
        self.store = store
        # transport(method, payload, token) -> dict, injectable for mocked tests
        self.transport = transport or self._http

    def configured(self):
        return bool(self.cfg.telegram_bot_token and self.cfg.telegram_chat_id)

    def run(self, op, args):
        if op == "send":
            return self.send_message(args.get("text", ""), args.get("chat_id"))
        if op == "get_updates":
            return self.get_updates(args.get("offset"), args.get("timeout", 0))
        if op == "test":
            return self.send_message("AUTO-agent test: notifier reachable. No action needed.")
        return ToolResult(ok=False, output=f"unknown op {op}")

    def send_message(self, text, chat_id=None):
        if not self.configured():
            return ToolResult(ok=False, output="telegram not configured (need TELEGRAMBOTTOKEN + TELEGRAMCHATID)",
                              data={"delivered": False})
        target = str(chat_id or self.cfg.telegram_chat_id)
        text = (text or "")[:3500]
        try:
            resp = self.transport("sendMessage",
                                  {"chat_id": target, "text": text, "disable_web_page_preview": True},
                                  self.cfg.telegram_bot_token)
        except Exception:
            # never leak URL/token in the error path
            return ToolResult(ok=False, output="telegram send failed: API unreachable",
                              data={"delivered": False})
        if isinstance(resp, dict) and resp.get("ok") is True:
            mid = (resp.get("result") or {}).get("message_id")
            return ToolResult(ok=True, output=f"telegram delivered (message_id={mid})",
                              data={"delivered": True, "message_id": mid})
        return ToolResult(ok=False, output="telegram send failed: API did not confirm delivery",
                          data={"delivered": False, "response": _safe_shape(resp)})

    def get_updates(self, offset=None, timeout=0):
        if not self.cfg.telegram_bot_token:
            return ToolResult(ok=False, output="telegram not configured")
        try:
            payload = {"timeout": int(timeout or 0)}
            if offset is not None:
                payload["offset"] = int(offset)
            resp = self.transport("getUpdates", payload, self.cfg.telegram_bot_token)
        except Exception:
            return ToolResult(ok=False, output="telegram poll failed: API unreachable")
        if isinstance(resp, dict) and resp.get("ok") is True:
            updates = resp.get("result") or []
            return ToolResult(ok=True, output=f"polled {len(updates)} updates",
                              data={"updates": updates})
        return ToolResult(ok=False, output="telegram poll failed: API did not confirm")

    def _http(self, method, payload, token):
        url = f"{API_BASE}/bot{token}/{method}"
        body = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())


def _safe_shape(resp):
    # log shape only, never raw payloads (may contain chat metadata)
    if isinstance(resp, dict):
        return {"ok": resp.get("ok"), "has_result": bool(resp.get("result")),
                "description": str(resp.get("description", ""))[:120]}
    return {"type": type(resp).__name__}
