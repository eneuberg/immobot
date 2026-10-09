"""Runtime configuration from environment variables.

Secrets live only here (loaded from env, set in Coolify). They are never written to the
database, never rendered in templates and never logged. Fields holding secrets use
repr=False so an accidental log of the Config object does not leak them.
"""

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    fredy_webhook_token: str = field(repr=False)
    dashboard_password: str = field(repr=False)
    session_secret: str = field(repr=False)
    telegram_chat_id: int | None
    data_dir: str
    cookie_secure: bool
    disable_bot: bool


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_config() -> Config:
    disable_bot = _flag("IMMOBOT_DISABLE_BOT", False)

    required = ["ANTHROPIC_API_KEY", "FREDY_WEBHOOK_TOKEN", "DASHBOARD_PASSWORD", "SESSION_SECRET"]
    if not disable_bot:
        required += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        # Names only, never values.
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

    session_secret = os.environ["SESSION_SECRET"].strip()
    if len(session_secret) < 32:
        raise ConfigError("SESSION_SECRET must be at least 32 characters (e.g. `openssl rand -hex 32`)")

    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    try:
        chat_id = int(chat_id_raw) if chat_id_raw else None
    except ValueError as exc:
        raise ConfigError("TELEGRAM_CHAT_ID must be a number") from exc

    return Config(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"].strip(),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        fredy_webhook_token=os.environ["FREDY_WEBHOOK_TOKEN"].strip(),
        dashboard_password=os.environ["DASHBOARD_PASSWORD"],
        session_secret=session_secret,
        telegram_chat_id=chat_id,
        data_dir=os.environ.get("DATA_DIR", "/data").strip() or "/data",
        cookie_secure=_flag("COOKIE_SECURE", True),
        disable_bot=disable_bot,
    )
