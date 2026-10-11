import os
import tempfile

import pytest

# Test env must be set before app.main is imported (it loads config at import time).
# The secret values are distinctive so tests can assert they never appear in output.
TEST_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-TEST-ANTHROPIC-SECRET-1234",
    "TELEGRAM_BOT_TOKEN": "123456:TEST-TELEGRAM-SECRET-5678",
    "FREDY_WEBHOOK_TOKEN": "test-webhook-SECRET-9012",
    "DASHBOARD_PASSWORD": "test-password-SECRET-3456",
    "SESSION_SECRET": "test-session-SECRET-" + "x" * 40,
}
os.environ.update(TEST_SECRETS)
os.environ.update({
    "TELEGRAM_CHAT_ID": "42",
    "IMMOBOT_DISABLE_BOT": "1",
    "COOKIE_SECURE": "0",
    "DATA_DIR": tempfile.mkdtemp(prefix="immobot-test-"),
})


@pytest.fixture(autouse=True)
def no_is24_network(tmp_path, monkeypatch):
    """No test may reach ImmoScout24: requests fail the test (pytest.fail is a BaseException, so no
    `except Exception` can swallow it). Tests that exercise is24 install their own MockTransport.
    Pictures go to a temporary directory; per-listing locks start fresh for every event loop."""
    import httpx

    from app import is24

    def refuse(request):
        pytest.fail(f"unexpected network request in a test: {request.url}")

    monkeypatch.setattr(is24, "_transport", httpx.MockTransport(refuse))
    monkeypatch.setattr(is24, "_media_root", tmp_path / "media")
    monkeypatch.setattr(is24, "_locks", {})


@pytest.fixture(autouse=True)
def no_public_url(monkeypatch):
    """The dashboard address for Telegram links starts unknown in every test."""
    from app import bot

    monkeypatch.setattr(bot, "_public_base", None)
    monkeypatch.setattr(bot, "_public_base_from_env", False)


@pytest.fixture
def fresh_db(tmp_path):
    """Point the db module at an empty database for this test."""
    from app import db

    db.init_db(str(tmp_path))
    return db


@pytest.fixture
def fresh_vault(fresh_db):
    """Empty database + vault keyed with the test SESSION_SECRET and no env fallbacks."""
    from app import vault

    vault.init(TEST_SECRETS["SESSION_SECRET"], {})
    return vault
