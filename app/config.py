"""Runtime configuration from environment variables.

Only DASHBOARD_PASSWORD and SESSION_SECRET are required. The API key, bot token, chat id, allowed
Telegram user ids and webhook token are normally entered in the dashboard ("Zugänge", see vault.py);
the env variables of the same name still work as a fallback (that is how older deployments were set up).

Secrets are never written to the database in plain text, never rendered in templates and never
logged. Fields holding secrets use repr=False so an accidental log of the Config object does not
leak them.
"""

import os
import re
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    dashboard_password: str = field(repr=False)
    session_secret: str = field(repr=False)
    data_dir: str
    cookie_secure: bool
    disable_bot: bool
    # Optional env fallbacks; a value saved in the dashboard takes precedence (see vault.py).
    anthropic_api_key: str = field(default="", repr=False)
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""  # not secret; "" = not set
    telegram_allowed_user_ids: str = ""  # not secret; "1,2"; "" = not set
    fredy_webhook_token: str = field(default="", repr=False)

    def env_credentials(self) -> dict[str, str]:
        """The env fallbacks keyed by vault credential name (empty strings = not set)."""
        return {
            "anthropic_api_key": self.anthropic_api_key,
            "telegram_bot_token": self.telegram_bot_token,
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_allowed_user_ids": self.telegram_allowed_user_ids,
            "fredy_webhook_token": self.fredy_webhook_token,
        }


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def load_config() -> Config:
    missing = [name for name in ("DASHBOARD_PASSWORD", "SESSION_SECRET") if not _env(name)]
    if missing:
        # Names only, never values.
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

    session_secret = _env("SESSION_SECRET")
    if len(session_secret) < 32:
        raise ConfigError("SESSION_SECRET must be at least 32 characters (e.g. `openssl rand -hex 32`)")

    chat_id = _env("TELEGRAM_CHAT_ID")
    if chat_id:
        try:
            chat_id = str(int(chat_id))
        except ValueError as exc:
            raise ConfigError("TELEGRAM_CHAT_ID must be a number") from exc

    user_ids = "".join(_env("TELEGRAM_ALLOWED_USER_IDS").split())  # "1, 2" -> "1,2"
    if user_ids and not re.fullmatch(r"[0-9]{1,20}(,[0-9]{1,20})*", user_ids):
        raise ConfigError("TELEGRAM_ALLOWED_USER_IDS must be comma-separated numbers")

    return Config(
        dashboard_password=os.environ["DASHBOARD_PASSWORD"],
        session_secret=session_secret,
        data_dir=_env("DATA_DIR") or "/data",
        cookie_secure=_flag("COOKIE_SECURE", True),
        disable_bot=_flag("IMMOBOT_DISABLE_BOT", False),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=chat_id,
        telegram_allowed_user_ids=user_ids,
        fredy_webhook_token=_env("FREDY_WEBHOOK_TOKEN"),
    )
