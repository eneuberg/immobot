"""Tests for app/vault.py (encrypted credential store, pairing) and the env contract in app/config.py."""

import hashlib
import hmac
import json
import logging

import pytest

from app import vault
from app.config import ConfigError, load_config
from tests.conftest import TEST_SECRETS

SECRET = TEST_SECRETS["SESSION_SECRET"]
API_KEY = "sk-ant-VAULT-TEST-KEY-0123456789"
BOT_TOKEN = "123456789:VAULTTESTbottokenSECRETpartAAAAAAAAAA"
WEBHOOK = "vault-webhook-token-SECRET-0123456789abcdef"


def stored_row(db, name):
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM credentials WHERE name = ?", (name,)).fetchone())


# --- encryption -----------------------------------------------------------------

def test_roundtrip_source_and_status(fresh_vault):
    assert fresh_vault.get("anthropic_api_key") is None and fresh_vault.source("anthropic_api_key") is None
    fresh_vault.set("anthropic_api_key", f"  {API_KEY}\n")  # surrounding whitespace is stripped
    assert fresh_vault.get("anthropic_api_key") == API_KEY
    assert fresh_vault.source("anthropic_api_key") == "dashboard"
    rows = {row["name"]: row for row in fresh_vault.status()}
    assert list(rows) == list(vault.LABELS)
    assert rows["anthropic_api_key"]["source"] == "dashboard" and rows["anthropic_api_key"]["updated_at"]
    assert rows["anthropic_api_key"]["label"] == "Anthropic-API-Key" and not rows["anthropic_api_key"]["undecryptable"]
    assert rows["telegram_bot_token"]["source"] is None
    assert API_KEY not in repr(fresh_vault.status())


def test_stored_encrypted_with_fresh_nonce(fresh_vault):
    fresh_vault.set("fredy_webhook_token", WEBHOOK)
    first = stored_row(fresh_vault.db, "fredy_webhook_token")
    assert len(first["nonce"]) == 12
    assert WEBHOOK.encode() not in first["ciphertext"]
    assert len(first["ciphertext"]) == len(WEBHOOK) + 16  # AES-GCM: plaintext + 16-byte tag
    fresh_vault.set("fredy_webhook_token", WEBHOOK)
    second = stored_row(fresh_vault.db, "fredy_webhook_token")
    assert second["nonce"] != first["nonce"] and second["ciphertext"] != first["ciphertext"]


def test_ciphertext_is_bound_to_its_name(fresh_vault):
    """AAD = name: a ciphertext copied to another credential does not decrypt."""
    fresh_vault.set("anthropic_api_key", API_KEY)
    row = stored_row(fresh_vault.db, "anthropic_api_key")
    fresh_vault.db.put_credential("fredy_webhook_token", row["nonce"], row["ciphertext"])
    assert fresh_vault.get("fredy_webhook_token") is None
    status = {r["name"]: r for r in fresh_vault.status()}
    assert status["fredy_webhook_token"]["undecryptable"] and status["fredy_webhook_token"]["source"] is None
    assert fresh_vault.get("anthropic_api_key") == API_KEY


def test_tampered_ciphertext_is_undecryptable(fresh_vault):
    fresh_vault.set("anthropic_api_key", API_KEY)
    row = stored_row(fresh_vault.db, "anthropic_api_key")
    flipped = bytes([row["ciphertext"][0] ^ 1]) + row["ciphertext"][1:]
    fresh_vault.db.put_credential("anthropic_api_key", row["nonce"], flipped)
    assert fresh_vault.get("anthropic_api_key") is None


def test_rotated_session_secret_makes_values_undecryptable(fresh_vault, caplog):
    fresh_vault.set("anthropic_api_key", API_KEY)
    fresh_vault.set("telegram_bot_token", BOT_TOKEN)
    vault.init("another-session-secret-" + "y" * 40, {"telegram_bot_token": "999:ENVTOKENxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"})
    with caplog.at_level(logging.WARNING, logger="app.vault"):
        assert vault.get("anthropic_api_key") is None
        assert vault.get("anthropic_api_key") is None
        assert vault.source("anthropic_api_key") is None
        # The undecryptable dashboard value counts as unset, so the env fallback applies.
        assert vault.get("telegram_bot_token") == "999:ENVTOKENxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        assert vault.source("telegram_bot_token") == "env"
    status = {r["name"]: r for r in vault.status()}
    assert status["anthropic_api_key"]["undecryptable"] and status["anthropic_api_key"]["updated_at"]
    assert status["telegram_bot_token"]["undecryptable"] and status["telegram_bot_token"]["source"] == "env"
    assert caplog.text.count("anthropic_api_key cannot be decrypted") == 1  # logged once, not per lookup
    assert API_KEY not in caplog.text and BOT_TOKEN not in caplog.text
    # Entering the value again (under the new secret) fixes it.
    vault.set("anthropic_api_key", API_KEY)
    assert vault.get("anthropic_api_key") == API_KEY
    assert not {r["name"]: r for r in vault.status()}["anthropic_api_key"]["undecryptable"]


def test_env_fallback_dashboard_override_and_clear(fresh_vault):
    vault.init(SECRET, {"fredy_webhook_token": " env-token ", "anthropic_api_key": "", "unknown": "x"})
    assert vault.get("fredy_webhook_token") == "env-token" and vault.source("fredy_webhook_token") == "env"
    assert vault.source("anthropic_api_key") is None and vault.get("unknown") is None
    vault.set("fredy_webhook_token", WEBHOOK)
    assert vault.get("fredy_webhook_token") == WEBHOOK and vault.source("fredy_webhook_token") == "dashboard"
    vault.clear("fredy_webhook_token")
    assert vault.get("fredy_webhook_token") == "env-token" and vault.source("fredy_webhook_token") == "env"


def test_get_before_init_fails_loudly(fresh_db, monkeypatch):
    monkeypatch.setattr(vault, "_aead", None)
    fresh_db.put_credential("anthropic_api_key", b"0" * 12, b"x" * 20)
    with pytest.raises(RuntimeError, match="init"):
        vault.get("anthropic_api_key")


def test_secret_values_cover_dashboard_env_and_token_half(fresh_vault):
    vault.init(SECRET, {"anthropic_api_key": "sk-ant-ENV", "telegram_chat_id": "42"})
    vault.set("telegram_bot_token", BOT_TOKEN)
    vault.set("telegram_chat_id", "-100")
    values = vault.secret_values()
    assert "sk-ant-ENV" in values and BOT_TOKEN in values and BOT_TOKEN.split(":")[1] in values
    assert "42" not in values and "-100" not in values  # the chat id is not secret
    assert "" not in values


# --- validation -------------------------------------------------------------------

@pytest.mark.parametrize("name,value,expected", [
    ("anthropic_api_key", API_KEY, API_KEY),
    ("telegram_bot_token", BOT_TOKEN, BOT_TOKEN),
    ("telegram_bot_token", "1:" + "a_-Z9" * 6, "1:" + "a_-Z9" * 6),
    ("telegram_chat_id", "42", "42"),
    ("telegram_chat_id", " -1001234567890 ", "-1001234567890"),
    ("telegram_chat_id", "0042", "42"),
    ("fredy_webhook_token", "x" * 32, "x" * 32),
    ("telegram_allowed_user_ids", "1001", "1001"),
    ("telegram_allowed_user_ids", " 1001, 2002 ,1001,0003 ", "1001,2002,3"),
])
def test_validate_accepts(name, value, expected):
    assert vault.validate(name, value) == expected


@pytest.mark.parametrize("name,value,message", [
    ("anthropic_api_key", "", "Wert"),
    ("anthropic_api_key", "   ", "Wert"),
    ("anthropic_api_key", "sk-proj-123", "sk-ant-"),
    ("anthropic_api_key", "sk-ant-" + "x" * 500, "500"),
    ("anthropic_api_key", "sk-ant-abc def", "Zeichen"),
    ("anthropic_api_key", "sk-ant-abc\ndef", "Zeichen"),
    ("anthropic_api_key", "sk-ant-äbc", "Zeichen"),
    ("telegram_bot_token", "123456:short", "Bot-Token"),
    ("telegram_bot_token", "abc:" + "a" * 35, "Bot-Token"),
    ("telegram_bot_token", "123456" + "a" * 35, "Bot-Token"),
    ("telegram_bot_token", "١٢٣:" + "a" * 35, "Zeichen"),  # non-ASCII digits
    ("telegram_chat_id", "abc", "ganze Zahl"),
    ("telegram_chat_id", "4.2", "ganze Zahl"),
    ("telegram_chat_id", "+42", "ganze Zahl"),
    ("telegram_chat_id", "--42", "ganze Zahl"),
    ("fredy_webhook_token", "x" * 31, "32"),
    ("telegram_allowed_user_ids", "-1001", "User-IDs"),
    ("telegram_allowed_user_ids", "1001,,2002", "User-IDs"),
    ("telegram_allowed_user_ids", "1001;2002", "User-IDs"),
    ("telegram_allowed_user_ids", "@erik", "User-IDs"),
    ("bogus", "value", "Unbekannt"),
])
def test_validate_rejects(name, value, message):
    with pytest.raises(vault.InvalidValue, match=message) as exc_info:
        vault.validate(name, value)
    if len(value) > 3:
        assert value.strip() not in str(exc_info.value)  # messages never echo the value


def test_invalid_value_is_not_stored(fresh_vault):
    with pytest.raises(vault.InvalidValue):
        fresh_vault.set("anthropic_api_key", "not-a-key")
    assert fresh_vault.db.get_credential("anthropic_api_key") is None


# --- pairing ----------------------------------------------------------------------

def pairing_hmac(code, session_secret=SECRET):
    key = vault._derive(session_secret, vault.PAIRING_KDF_INFO)
    return hmac.new(key, code.encode(), "sha256").hexdigest()


def test_pairing_stores_only_keyed_hash_and_expiry(fresh_vault):
    assert not vault.pairing_pending() and vault.pairing_info() is None
    before = vault.time.time()
    code = vault.start_pairing("immo_test_bot")
    assert len(code) == 8 and int(code, 16) >= 0
    raw = fresh_vault.db.get_meta(vault.PAIRING_META_KEY)
    assert code not in raw
    stored = json.loads(raw)
    assert stored["hmac"] == pairing_hmac(code) and stored["failures"] == 0
    # Keyed: the plain SHA-256 (brute-forceable from a DB copy) is not what is stored.
    assert hashlib.sha256(code.encode()).hexdigest() not in raw
    assert vault._pairing_key != vault._derive(SECRET, vault.KDF_INFO)  # separate key from the cipher
    info = vault.pairing_info()
    assert before + 600 <= info["expires_at"] <= vault.time.time() + 600 and info["bot_username"] == "immo_test_bot"
    assert vault.pairing_pending()


def test_pairing_code_check_does_not_use_it_up(fresh_vault):
    """The caller uses the pairing up with cancel_pairing() once it was carried out (see bot._pair)."""
    code = vault.start_pairing("immo_test_bot")
    assert vault.check_pairing_code(f" {code} ")
    assert vault.check_pairing_code(code) and vault.pairing_pending()
    assert json.loads(fresh_vault.db.get_meta(vault.PAIRING_META_KEY))["failures"] == 0  # right codes don't count
    vault.cancel_pairing()
    assert not vault.pairing_pending() and not vault.check_pairing_code(code)


def test_undecryptable_flag(fresh_vault):
    fresh_vault.set("telegram_allowed_user_ids", "1001")
    assert not vault.undecryptable("telegram_allowed_user_ids") and not vault.undecryptable("telegram_chat_id")
    vault.init("rotated-session-secret-" + "z" * 40, {})
    assert vault.undecryptable("telegram_allowed_user_ids") and not vault.undecryptable("telegram_chat_id")


def test_new_pairing_replaces_old_code(fresh_vault):
    old = vault.start_pairing("immo_test_bot")
    new = vault.start_pairing("immo_test_bot")
    assert not vault.check_pairing_code(old) or old == new
    assert vault.check_pairing_code(new)


def test_pairing_cancelled_after_max_failures(fresh_vault):
    code = vault.start_pairing("immo_test_bot")
    for attempt in range(1, vault.PAIRING_MAX_FAILURES):
        assert not vault.check_pairing_code("wrong")
        assert json.loads(fresh_vault.db.get_meta(vault.PAIRING_META_KEY))["failures"] == attempt
    assert not vault.check_pairing_code("wrong")
    assert fresh_vault.db.get_meta(vault.PAIRING_META_KEY) is None
    assert not vault.check_pairing_code(code)


def test_expired_or_corrupt_pairing_is_removed(fresh_vault, monkeypatch):
    code = vault.start_pairing("immo_test_bot")
    later = vault.time.time() + vault.PAIRING_TTL_S + 1
    monkeypatch.setattr(vault.time, "time", lambda: later)
    assert not vault.check_pairing_code(code)
    assert fresh_vault.db.get_meta(vault.PAIRING_META_KEY) is None
    monkeypatch.undo()
    fresh_vault.db.set_meta(vault.PAIRING_META_KEY, "not json")
    assert not vault.pairing_pending()
    assert fresh_vault.db.get_meta(vault.PAIRING_META_KEY) is None


# --- config -----------------------------------------------------------------------

CREDENTIAL_ENV = ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USER_IDS",
                  "FREDY_WEBHOOK_TOKEN"]


def test_config_needs_only_password_and_session_secret(monkeypatch):
    for name in CREDENTIAL_ENV + ["IMMOBOT_DISABLE_BOT"]:
        monkeypatch.delenv(name, raising=False)
    config = load_config()
    assert set(config.env_credentials()) <= set(vault.LABELS)
    assert all(value == "" for value in config.env_credentials().values())
    assert config.disable_bot is False


def test_config_env_fallbacks(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " -100123 ")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1001, 2002")
    creds = load_config().env_credentials()
    assert creds["telegram_allowed_user_ids"] == "1001,2002"
    assert set(creds) <= set(vault.LABELS)  # no env fallback for the DeutschlandGPT key
    assert creds["anthropic_api_key"] == TEST_SECRETS["ANTHROPIC_API_KEY"]
    assert creds["fredy_webhook_token"] == TEST_SECRETS["FREDY_WEBHOOK_TOKEN"]
    assert creds["telegram_chat_id"] == "-100123"


@pytest.mark.parametrize("name,value,message", [
    ("DASHBOARD_PASSWORD", "", "DASHBOARD_PASSWORD"),
    ("SESSION_SECRET", "  ", "SESSION_SECRET"),
    ("SESSION_SECRET", "x" * 31, "at least 32"),
    ("TELEGRAM_CHAT_ID", "abc", "TELEGRAM_CHAT_ID"),
    ("TELEGRAM_ALLOWED_USER_IDS", "1001,-5", "TELEGRAM_ALLOWED_USER_IDS"),
    ("TELEGRAM_ALLOWED_USER_IDS", "erik", "TELEGRAM_ALLOWED_USER_IDS"),
])
def test_config_errors(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=message) as exc_info:
        load_config()
    for secret in TEST_SECRETS.values():
        assert secret not in str(exc_info.value)


def test_config_repr_hides_secrets():
    text = repr(load_config())
    for secret in TEST_SECRETS.values():
        assert secret not in text


# --- key hints ----------------------------------------------------------------------

@pytest.mark.parametrize("name,value,expected", [
    ("anthropic_api_key", "sk-ant-api03-abcdefghijklmnopqrstuvwxyzA1B2", "sk-ant-…A1B2"),
    ("telegram_bot_token", "123456789:AAbbCCddEEffGGhhIIjjKKllMMnnOOpp9Z8Y", "123456789:…9Z8Y"),
    ("fredy_webhook_token", "abcdEFGHijklMNOPqrstUVWXyz012345", "abcd…2345"),
    ("deutschlandgpt_api_key", "dgpt_live_0123456789abcdefWXYZ", "dgpt…WXYZ"),
    ("fredy_webhook_token", "short-token-12", "…12"),  # under 16 characters: only the last 2
    ("telegram_chat_id", "-100123", None),  # not a secret: no hint (the value is shown anyway)
    ("anthropic_api_key", "", None),
])
def test_hint(name, value, expected):
    assert vault.hint(name, value) == expected


def test_hint_stored_at_save_and_computed_for_env(fresh_vault):
    vault.init(SECRET, {"anthropic_api_key": "sk-ant-from-env-0000000000ENV1"})
    rows = {r["name"]: r for r in vault.status()}
    assert rows["anthropic_api_key"]["hint"] == "sk-ant-…ENV1" and rows["anthropic_api_key"]["source"] == "env"
    vault.set("deutschlandgpt_api_key", "dgpt_live_0123456789abcdefWXYZ")
    assert fresh_vault.db.get_credential("deutschlandgpt_api_key")["hint"] == "dgpt…WXYZ"
    assert vault.credential_status("deutschlandgpt_api_key")["hint"] == "dgpt…WXYZ"
    # After a SESSION_SECRET change the stored hint still says which key it was.
    vault.init("rotated-session-secret-" + "z" * 40, {})
    status = vault.credential_status("deutschlandgpt_api_key")
    assert status["undecryptable"] and status["source"] is None and status["hint"] == "dgpt…WXYZ"


def test_rows_without_stored_hint_get_one_from_the_value(fresh_vault):
    """Credentials saved before hints existed (hint column NULL)."""
    vault.set("fredy_webhook_token", "abcdEFGHijklMNOPqrstUVWXyz012345")
    with fresh_vault.db.connect() as conn:
        conn.execute("UPDATE credentials SET hint = NULL")
    assert vault.credential_status("fredy_webhook_token")["hint"] == "abcd…2345"


@pytest.mark.parametrize("value,ok", [
    ("x" * 20, True), ("x" * 200, True), ("dgpt_live_0123456789abcdefWXYZ", True),
    ("x" * 19, False), ("x" * 201, False), ("dgpt live 0123456789abcdef", False), ("", False),
])
def test_deutschlandgpt_key_validation(value, ok):
    if ok:
        assert vault.validate("deutschlandgpt_api_key", value) == value
    else:
        with pytest.raises(vault.InvalidValue):
            vault.validate("deutschlandgpt_api_key", value)


def test_deutschlandgpt_key_is_a_secret(fresh_vault):
    vault.set("deutschlandgpt_api_key", "dgpt_live_0123456789abcdefWXYZ")
    assert "dgpt_live_0123456789abcdefWXYZ" in vault.secret_values()
