"""Central config. Sandbox-first. Env credentials never enter prompts."""
import os
from dataclasses import dataclass


def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _get(name, default=""):
    return os.environ.get(name, default)


def _int(name, default):
    try:
        return int(float(_get(name, str(default))))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    target_revenue_usd: float = 25.0
    currency: str = "USD"
    sandbox_mode: bool = True
    approve_live_payments: bool = False
    max_iterations: int = 500
    max_runtime_hours: float = 168.0
    max_llm_calls: int = 1000
    max_tool_calls: int = 5000
    max_llm_spend_usd: float = 10.0
    max_infra_spend_usd: float = 25.0
    max_spend_usd: float = 10.0  # legacy total cap, kept for backward compat
    max_consecutive_failures: int = 25
    cycle_sleep_seconds: int = 5
    data_dir: str = "./data"
    kill_switch_file: str = "./data/STOP"
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_api_key: str = ""
    llm_model: str = "meta/muse-glimmer-30b"
    github_token: str = ""
    allowed_repos: tuple = ()
    cf_api_token: str = ""
    cf_account_id: str = ""
    cf_zone_id: str = ""
    dodo_api_key: str = ""
    dodo_mode: str = "test"
    dodo_base_url: str = "https://api.dodopayments.com/v1"
    brave_api_key: str = ""
    posthog_key: str = ""
    posthog_host: str = "https://app.posthog.com"
    sentry_dsn: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_allowed_ids: tuple = ()
    telegram_token_file: str = ""
    telegram_cooldown_s: int = 3600
    telegram_spend_warn_pct: tuple = (50, 80)
    telegram_poll_enabled: bool = True
    dashboard_token: str = ""
    dashboard_token_file: str = ""
    dashboard_api_url: str = ""
    dashboard_port: int = 8787
    dashboard_poll_s: int = 5
    dodo_fee_pct: float = 0.0


def get_config() -> Config:
    allowed = tuple(r.strip() for r in _get("AGENT_ALLOWED_REPOS", "").split(",") if r.strip())
    return Config(
        target_revenue_usd=_float("TARGET_REVENUE_USD", 25.0),
        currency=_get("CURRENCY", "USD") or "USD",
        sandbox_mode=_get("SANDBOX_MODE", "1") != "0",
        approve_live_payments=_get("APPROVE_LIVE_PAYMENTS", "0") == "1",
        max_iterations=_int("MAX_ITERATIONS", 500),
        max_runtime_hours=_float("MAX_RUNTIME_HOURS", _float("EXPERIMENT_HOURS", 168.0)),
        max_llm_calls=_int("MAX_LLM_CALLS", 1000),
        max_tool_calls=_int("MAX_TOOL_CALLS", 5000),
        max_llm_spend_usd=_float("MAX_LLM_SPEND_USD", 10.0),
        max_infra_spend_usd=_float("MAX_INFRA_SPEND_USD", 25.0),
        max_spend_usd=_float("MAX_SPEND_USD", 10.0),
        max_consecutive_failures=_int("MAX_CONSECUTIVE_FAILURES", 25),
        cycle_sleep_seconds=_int("CYCLE_SLEEP_SECONDS", 5),
        data_dir=_get("DATA_DIR", "./data"),
        kill_switch_file=_get("KILL_SWITCH_FILE", "./data/STOP"),
        # NVIDIA NIM endpoint; meta/muse-glimmer-30b is the only allowed model.
        llm_base_url=_get("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        llm_api_key=_get("LLM_API_KEY", ""),
        llm_model=_get("LLM_MODEL", "meta/muse-glimmer-30b"),
        github_token=_get("GITHUB_TOKEN", ""),
        allowed_repos=allowed,
        cf_api_token=_get("CF_API_TOKEN", ""),
        cf_account_id=_get("CF_ACCOUNT_ID", ""),
        cf_zone_id=_get("CF_ZONE_ID", ""),
        dodo_api_key=_get("DODO_API_KEY", ""),
        dodo_mode=_get("DODO_MODE", "test"),
        dodo_base_url=_get("DODO_BASE_URL", "https://api.dodopayments.com/v1"),
        brave_api_key=_get("BRAVE_API_KEY", ""),
        posthog_key=_get("POSTHOG_KEY", ""),
        posthog_host=_get("POSTHOG_HOST", "https://app.posthog.com"),
        sentry_dsn=_get("SENTRY_DSN", ""),
        telegram_bot_token=_telegram_token(),
        telegram_chat_id=_get("TELEGRAMCHATID", "") or _get("TELEGRAM_CHAT_ID", ""),
        telegram_allowed_ids=_telegram_allowlist(),
        telegram_token_file=_get("TELEGRAM_TOKEN_FILE", ""),
        telegram_cooldown_s=_int("TELEGRAM_ALERT_COOLDOWN_S", 3600),
        telegram_spend_warn_pct=_telegram_pcts(),
        telegram_poll_enabled=_get("TELEGRAM_POLL_ENABLED", "1") != "0",
        dashboard_token=_dashboard_token(),
        dashboard_token_file=_get("DASHBOARD_TOKEN_FILE", ""),
        dashboard_api_url=_get("DASHBOARD_API_URL", "").rstrip("/"),
        dashboard_port=_int("DASHBOARD_PORT", 8787),
        dashboard_poll_s=_int("DASHBOARD_POLL_S", 5),
        dodo_fee_pct=_float("DODO_FEE_PCT", 0.0),
    )


def _dashboard_token():
    tok = _get("DASHBOARD_TOKEN", "")
    if not tok:
        path = _get("DASHBOARD_TOKEN_FILE", "")
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    tok = f.read().strip()
            except OSError:
                tok = ""
    return tok


def _telegram_token():
    tok = _get("TELEGRAMBOTTOKEN", "") or _get("TELEGRAM_BOT_TOKEN", "")
    if not tok:
        path = _get("TELEGRAM_TOKEN_FILE", "")
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    tok = f.read().strip()
            except OSError:
                tok = ""
    return tok


def _telegram_allowlist():
    raw = _get("TELEGRAM_ALLOWED_IDS", "")
    ids = tuple(s.strip() for s in raw.split(",") if s.strip())
    if not ids:
        chat = _get("TELEGRAMCHATID", "") or _get("TELEGRAM_CHAT_ID", "")
        if chat:
            ids = (chat.strip(),)
    return ids


def _telegram_pcts():
    raw = _get("TELEGRAM_SPEND_WARN_PCT", "50,80")
    out = []
    for part in raw.split(","):
        try:
            v = float(part.strip())
        except ValueError:
            continue
        if 0 < v < 100:
            out.append(v)
    return tuple(sorted(out)) or (50.0, 80.0)
