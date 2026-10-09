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


@pytest.fixture
def fresh_db(tmp_path):
    """Point the db module at an empty database for this test."""
    from app import db

    db.init_db(str(tmp_path))
    return db
