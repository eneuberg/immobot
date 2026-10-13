"""Credential store for the API keys and tokens managed under "Einstellungen" (KI & Kosten, Telegram & Fredy).

- Values are encrypted with AES-256-GCM before they go into the `credentials` table. The key is derived
  from SESSION_SECRET (HKDF-SHA256), so a copy of the database alone reveals nothing. The credential
  name is the associated data: a ciphertext copied to another name does not decrypt.
- Rotating SESSION_SECRET makes stored values undecryptable. They then count as unset (status() marks
  them `undecryptable`) and have to be entered again.
- Lookup order: value saved in the dashboard, else the env variable (migration fallback), else None.
- Values are never logged, never rendered and never returned to the browser; only names, labels,
  sources and a short hint such as "sk-ant-…A1B2" (to recognise which key is set) leave this module
  through status().

Also here: the Telegram chat pairing ("/pair CODE"). Only an HMAC-SHA256 of the code (keyed from
SESSION_SECRET, so a copy of the database can't be used to brute-force the short code) and its expiry
are stored, in the meta table.
"""

import hmac
import json
import logging
import os
import re
import secrets
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import db

log = logging.getLogger(__name__)

LABELS = {
    "anthropic_api_key": "Anthropic-API-Key",
    "deutschlandgpt_api_key": "DeutschlandGPT-API-Key",
    "telegram_bot_token": "Telegram-Bot-Token",
    "telegram_chat_id": "Telegram-Chat-ID",
    "telegram_allowed_user_ids": "Erlaubte Telegram-User-IDs",
    "fredy_webhook_token": "Fredy-Webhook-Token",
}
SECRET_NAMES = ("anthropic_api_key", "deutschlandgpt_api_key", "telegram_bot_token", "fredy_webhook_token")  # the others aren't
ACTION_LABELS = {  # credential_events.action -> German label
    "set": "gespeichert",
    "clear": "entfernt",
    "regenerate": "neu erzeugt",
    "pair_start": "Pairing gestartet",
    "pair": "per Telegram-Pairing gesetzt",
    "test_failed": "Verbindungstest fehlgeschlagen",
    "login_failed": "wiederholt falsches Passwort",
    "login_failed_many": "viele Fehlversuche von verschiedenen IPs",
}
MAX_LEN = 500
KDF_INFO = b"immobot credentials v1"
PAIRING_KDF_INFO = b"immobot pairing v1"

PAIRING_META_KEY = "telegram_pairing"
PAIRING_TTL_S = 600
PAIRING_MAX_FAILURES = 5

_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]+")  # no spaces, no control or non-ASCII characters
_BOT_TOKEN = re.compile(r"[0-9]+:[A-Za-z0-9_-]{30,}")
_CHAT_ID = re.compile(r"-?[0-9]{1,20}")
_USER_IDS = re.compile(r"[0-9]{1,20}(,[0-9]{1,20})*")

_aead: AESGCM | None = None
_pairing_key: bytes | None = None
_env: dict[str, str] = {}
_warned: set[str] = set()  # names already logged as undecryptable (log once, not on every lookup)


class InvalidValue(ValueError):
    """str(exc) is a short German message for the dashboard."""


def init(session_secret: str, env_fallbacks: dict[str, str]) -> None:
    """Derive the encryption key and remember the env fallbacks. Call once at startup (after db.init_db)."""
    global _aead, _pairing_key, _env
    _aead = AESGCM(_derive(session_secret, KDF_INFO))
    _pairing_key = _derive(session_secret, PAIRING_KDF_INFO)
    _env = {name: value.strip() for name, value in env_fallbacks.items() if name in LABELS and value and value.strip()}
    _warned.clear()


def _derive(session_secret: str, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(session_secret.encode("utf-8"))


def _cipher() -> AESGCM:
    if _aead is None:
        raise RuntimeError("vault.init() was not called")
    return _aead


def _load(name: str) -> tuple[str | None, str | None, bool]:
    """(value, updated_at, undecryptable) of the value saved in the dashboard."""
    row = db.get_credential(name)
    if row is None:
        return None, None, False
    try:
        value = _cipher().decrypt(row["nonce"], row["ciphertext"], name.encode("utf-8")).decode("utf-8")
    except (InvalidTag, ValueError, TypeError):
        if name not in _warned:
            _warned.add(name)
            log.warning("Stored credential %s cannot be decrypted (SESSION_SECRET changed?) - treated as unset", name)
        return None, row["updated_at"], True
    return value, row["updated_at"], False


def get(name: str) -> str | None:
    """Dashboard value, else env fallback, else None."""
    value = _load(name)[0]
    return value if value is not None else _env.get(name)


def undecryptable(name: str) -> bool:
    """A dashboard value is stored but can't be decrypted (SESSION_SECRET changed)."""
    return _load(name)[2]


def source(name: str) -> str | None:
    """"dashboard", "env" or None (not set)."""
    if _load(name)[0] is not None:
        return "dashboard"
    return "env" if name in _env else None


def validate(name: str, value: str) -> str:
    """Normalized value, or InvalidValue with a German message."""
    if name not in LABELS:
        raise InvalidValue("Unbekannter Zugang.")
    value = (value or "").strip()
    if not value:
        raise InvalidValue("Bitte einen Wert eingeben.")
    if len(value) > MAX_LEN:
        raise InvalidValue(f"Höchstens {MAX_LEN} Zeichen.")
    if name == "telegram_allowed_user_ids":
        value = "".join(value.split())  # "1, 2" -> "1,2"
    if not _PRINTABLE_ASCII.fullmatch(value):
        raise InvalidValue("Enthält Leerzeichen oder ungültige Zeichen.")
    if name == "anthropic_api_key" and not value.startswith("sk-ant-"):
        raise InvalidValue("Ein Anthropic-API-Key beginnt mit „sk-ant-“.")
    if name == "telegram_bot_token" and not _BOT_TOKEN.fullmatch(value):
        raise InvalidValue("Sieht nicht wie ein Bot-Token aus (Format „123456789:ABC…“ von @BotFather).")
    if name == "telegram_chat_id":
        if not _CHAT_ID.fullmatch(value):
            raise InvalidValue("Die Chat-ID ist eine ganze Zahl (bei Gruppen negativ).")
        value = str(int(value))
    if name == "telegram_allowed_user_ids":
        if not _USER_IDS.fullmatch(value):
            raise InvalidValue("Kommagetrennte Telegram-User-IDs (ganze Zahlen), z. B. „12345678, 87654321“.")
        value = ",".join(dict.fromkeys(str(int(part)) for part in value.split(",")))  # normalized, no duplicates
    if name == "fredy_webhook_token" and len(value) < 32:
        raise InvalidValue("Mindestens 32 Zeichen – am einfachsten „Neu erzeugen“ verwenden.")
    if name == "deutschlandgpt_api_key" and not 20 <= len(value) <= 200:
        raise InvalidValue("Ein DeutschlandGPT-API-Key hat 20 bis 200 Zeichen.")
    return value


def hint(name: str, value: str | None) -> str | None:
    """A short display form of a secret, enough to recognise which key is set: "sk-ant-…A1B2",
    "123456:…A1B2", "abcd…wxyz"; values under 16 characters only "…" + the last 2. None for non-secrets."""
    if not value or name not in SECRET_NAMES:
        return None
    if len(value) < 16:
        return "…" + value[-2:]
    if name == "anthropic_api_key" and value.startswith("sk-ant-"):
        return "sk-ant-…" + value[-4:]
    if name == "telegram_bot_token" and ":" in value:
        return value.partition(":")[0] + ":…" + value[-4:]
    return value[:4] + "…" + value[-4:]


def set(name: str, value: str) -> None:  # noqa: A001 - shadows the builtin inside this module; don't use set() below
    """Validate, encrypt and store a value (replaces an existing one). Raises InvalidValue."""
    value = validate(name, value)
    nonce = os.urandom(12)
    ciphertext = _cipher().encrypt(nonce, value.encode("utf-8"), name.encode("utf-8"))
    db.put_credential(name, nonce, ciphertext, hint(name, value))
    _warned.discard(name)


def clear(name: str) -> None:
    """Remove the dashboard value; an env fallback (if any) applies again."""
    db.delete_credential(name)
    _warned.discard(name)


def status() -> list[dict]:
    """Per credential: name, label, source, updated_at, undecryptable, hint. Never values."""
    return [credential_status(name) for name in LABELS]


def credential_status(name: str) -> dict:
    """name, label, source ("dashboard" / "env" / None), updated_at, undecryptable and hint of one credential.
    The hint belongs to the value in use; for an undecryptable value it is the one stored at save time."""
    value, updated_at, undecryptable = _load(name)
    if value is not None:
        where, shown = "dashboard", hint(name, value)
    elif name in _env:
        where, shown = "env", hint(name, _env[name])
    else:
        where, shown = None, None
    if undecryptable and shown is None:
        shown = (db.get_credential(name) or {}).get("hint")
    return {"name": name, "label": LABELS[name], "source": where, "updated_at": updated_at,
            "undecryptable": undecryptable, "hint": shown}


def secret_values() -> list[str]:
    """Every known secret value (dashboard and env) plus the secret half of bot tokens - for scrubbing texts."""
    values = []
    for name in SECRET_NAMES:
        for value in (_load(name)[0], _env.get(name)):
            if value:
                values.append(value)
                if name == "telegram_bot_token":
                    values.append(value.partition(":")[2])  # "<bot id>:<secret>"
    return [v for v in values if v]


# --- Telegram chat pairing --------------------------------------------------------

def _code_hash(code: str) -> str:
    if _pairing_key is None:
        raise RuntimeError("vault.init() was not called")
    return hmac.new(_pairing_key, code.encode("utf-8"), "sha256").hexdigest()


def _pairing() -> dict | None:
    """The pending pairing, or None. An expired or unreadable one is removed."""
    raw = db.get_meta(PAIRING_META_KEY)
    if raw is None:
        return None
    try:
        pairing = json.loads(raw)
        alive = (float(pairing["expires_at"]) > time.time()
                 and isinstance(pairing["hmac"], str) and isinstance(pairing["failures"], int))
    except (ValueError, KeyError, TypeError):
        alive = False
    if not alive:
        cancel_pairing()
        return None
    return pairing


def start_pairing(bot_username: str) -> str:
    """New 8-character code, valid for PAIRING_TTL_S. Replaces a pending one. Only its keyed hash is stored,
    next to the bot's username (for showing the exact "/pair@bot CODE" command)."""
    code = secrets.token_hex(4)
    pairing = {"hmac": _code_hash(code), "expires_at": time.time() + PAIRING_TTL_S, "failures": 0,
               "bot_username": bot_username}
    db.set_meta(PAIRING_META_KEY, json.dumps(pairing))
    return code


def pairing_info() -> dict | None:
    """{"expires_at": unix time, "bot_username": str} of the pending pairing, or None if there is none."""
    pairing = _pairing()
    return {"expires_at": float(pairing["expires_at"]), "bot_username": pairing.get("bot_username")} if pairing else None


def pairing_pending() -> bool:
    return _pairing() is not None


def check_pairing_code(code: str) -> bool:
    """True for the right code of a pending pairing. It is not used up here: call cancel_pairing() once the
    pairing was carried out. Every wrong code counts; after PAIRING_MAX_FAILURES the pairing is cancelled."""
    pairing = _pairing()
    if pairing is None:
        return False
    code = (code or "").strip().lower()[:64]
    if hmac.compare_digest(_code_hash(code), pairing["hmac"]):
        return True
    pairing["failures"] += 1
    if pairing["failures"] >= PAIRING_MAX_FAILURES:
        log.warning("Telegram pairing cancelled after %d wrong codes", pairing["failures"])
        cancel_pairing()
    else:
        db.set_meta(PAIRING_META_KEY, json.dumps(pairing))
    return False


def cancel_pairing() -> None:
    db.delete_meta(PAIRING_META_KEY)
