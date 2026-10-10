"""Secret redaction for logs, events, errors, and dashboard output.

Single choke point: every persisted event, API response, and notification
passes through scrub(). Raw API keys, tokens, and webhook secrets must never
reach SQLite, D1, logs, or the browser.
"""
import os
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2}")
TG_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*")
GENERIC_SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+")

MAX_LEN = 2000


def known_secrets():
    vals = []
    for name in ("DASHBOARD_TOKEN", "TELEGRAMBOTTOKEN", "TELEGRAM_BOT_TOKEN",
                 "LLM_API_KEY", "GITHUB_TOKEN", "AGENT_GITHUB_TOKEN",
                 "CF_API_TOKEN",
                 "DODO_API_KEY", "DODO_WEBHOOK_SECRET", "BRAVE_API_KEY",
                 "POSTHOG_KEY", "SENTRY_DSN"):
        v = os.environ.get(name, "")
        if v:
            vals.append(v)
    # token-file contents, if configured
    path = os.environ.get("DASHBOARD_TOKEN_FILE", "") or os.environ.get("TELEGRAM_TOKEN_FILE", "")
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                v = f.read().strip()
            if v:
                vals.append(v)
        except OSError:
            pass
    return vals


def scrub(text, extra=()):
    out = str(text or "")
    for s in list(known_secrets()) + list(extra or ()):
        if s and len(s) >= 4 and s in out:
            out = out.replace(s, "***")
    out = TG_TOKEN_RE.sub("***", out)
    out = BEARER_RE.sub("Bearer ***", out)
    out = GENERIC_SECRET_RE.sub(r"\1=***", out)
    out = EMAIL_RE.sub("[redacted-email]", out)
    return out[:MAX_LEN]


def scrub_dict(d, extra=()):
    if not isinstance(d, dict):
        return d
    clean = {}
    for k, v in d.items():
        lk = str(k).lower()
        if any(s in lk for s in ("token", "secret", "password", "api_key", "apikey", "auth")):
            clean[k] = "***"
        elif isinstance(v, dict):
            clean[k] = scrub_dict(v, extra)
        elif isinstance(v, str):
            clean[k] = scrub(v, extra)
        else:
            clean[k] = v
    return clean
