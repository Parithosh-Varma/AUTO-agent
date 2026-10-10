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
    max_runtime_hours: float = 24.0
    max_llm_calls: int = 1000
    max_spend_usd: float = 10.0
    cycle_sleep_seconds: int = 5
    data_dir: str = "./data"
    kill_switch_file: str = "./data/STOP"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
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


def get_config() -> Config:
    allowed = tuple(r.strip() for r in _get("AGENT_ALLOWED_REPOS", "").split(",") if r.strip())
    return Config(
        target_revenue_usd=_float("TARGET_REVENUE_USD", 25.0),
        currency=_get("CURRENCY", "USD") or "USD",
        sandbox_mode=_get("SANDBOX_MODE", "1") != "0",
        approve_live_payments=_get("APPROVE_LIVE_PAYMENTS", "0") == "1",
        max_iterations=_int("MAX_ITERATIONS", 500),
        max_runtime_hours=_float("MAX_RUNTIME_HOURS", 24),
        max_llm_calls=_int("MAX_LLM_CALLS", 1000),
        max_spend_usd=_float("MAX_SPEND_USD", 10.0),
        cycle_sleep_seconds=_int("CYCLE_SLEEP_SECONDS", 5),
        data_dir=_get("DATA_DIR", "./data"),
        kill_switch_file=_get("KILL_SWITCH_FILE", "./data/STOP"),
        llm_base_url=_get("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=_get("LLM_API_KEY", ""),
        llm_model=_get("LLM_MODEL", "gpt-4o-mini"),
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
    )
