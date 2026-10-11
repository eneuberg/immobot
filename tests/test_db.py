import pytest

from app import settings


def _listing(fredy_id="abc", **extra):
    return {"fredy_id": fredy_id, "provider": "immoscout", "title": "2 Zi", "price": "780 €", "url": "https://example.org/1", **extra}


def test_insert_listing_dedupes(fresh_db):
    first, created = fresh_db.insert_listing(_listing())
    again, created_again = fresh_db.insert_listing(_listing())
    assert created and not created_again
    assert first == again
    assert fresh_db.get_listing(first)["status"] == "new"


def test_drafts_costs_and_stats(fresh_db):
    lid, _ = fresh_db.insert_listing(_listing())
    fresh_db.add_draft(lid, "initial", "Hallo", "claude-opus-5-5", input_tokens=1000, output_tokens=500, cost_usd=0.014)
    fresh_db.add_draft(lid, "test", "Test", "claude-opus-5-5", cost_usd=0.01)
    assert fresh_db.latest_draft(lid)["kind"] == "initial"
    assert fresh_db.latest_draft(lid, include_test=True)["kind"] == "test"
    row = fresh_db.list_listings()[0]
    assert row["draft_count"] == 2 and abs(row["cost_usd"] - 0.024) < 1e-9
    s = fresh_db.stats()
    assert s["listings_total"] == 1 and abs(s["cost_month"] - 0.024) < 1e-9
    assert fresh_db.list_listings(status="sent") == []


def test_settings_defaults_and_unknown_keys(fresh_db):
    assert fresh_db.get_settings()["model"] == settings.DEFAULT_MODEL
    fresh_db.update_settings({"model": "claude-haiku-5-5", "ANTHROPIC_API_KEY": "nope"})
    stored = fresh_db.get_settings()
    assert stored["model"] == "claude-haiku-5-5"
    assert "ANTHROPIC_API_KEY" not in stored
    fresh_db.reset_setting("model")
    assert fresh_db.get_settings()["model"] == settings.DEFAULT_MODEL


def test_pending_prompts_and_meta(fresh_db):
    fresh_db.add_pending_prompt(555, 7)
    assert fresh_db.pop_pending_prompt(555) == 7
    assert fresh_db.pop_pending_prompt(555) is None
    fresh_db.set_meta("last_webhook_at", "x")
    assert fresh_db.get_meta("last_webhook_at") == "x"


def test_cost_and_format():
    assert abs(settings.cost("anthropic", "claude-opus-5-5", 1_000_000, 0) - 4.0) < 1e-9
    assert abs(settings.cost("anthropic", "unknown-model", 0, 1_000_000) - 20.0) < 1e-9  # priced as the default
    assert abs(settings.cost("deutschlandgpt", "claude-haiku-5.5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
               - (0.83 + 4.13 + 0.09 + 1.04)) < 1e-9
    assert abs(settings.cost("deutschlandgpt", "unknown", 1_000_000, 0) - 3.30) < 1e-9  # DGPT default: Sonnet 5.5
    assert settings.to_usd(2.0, "EUR", 1.1) == pytest.approx(2.2) and settings.to_usd(2.0, "USD", 1.1) == 2.0
    assert settings.fmt_usd(0.042) == "4,2 ct"
    assert settings.fmt_usd(1.5) == "1,50 $"
    assert settings.fmt_money(0.021, "EUR") == "2,1 ct (€)" and settings.fmt_money(3.5, "EUR") == "3,50 €"
    assert settings.fmt_money(0.042, "USD") == "4,2 ct ($)"
    assert settings.draft_cost_text({"currency": "EUR", "cost_native": 0.021, "cost_usd": 0.0231}) == "2,1 ct (€) ≈ 2,3 ct ($)"
    assert settings.draft_cost_text({"cost_usd": 0.042}) == "4,2 ct ($)"  # old drafts: USD


def test_config_does_not_repr_secrets():
    from app.config import load_config
    from tests.conftest import TEST_SECRETS

    text = repr(load_config())
    for secret in TEST_SECRETS.values():
        assert secret not in text


def test_unannounced_listing_ids(fresh_db):
    waiting, _ = fresh_db.insert_listing(_listing("a"))
    announced, _ = fresh_db.insert_listing(_listing("b"))
    dismissed, _ = fresh_db.insert_listing(_listing("c"))
    fresh_db.set_listing_tg_message(announced, 99)
    fresh_db.set_listing_status(dismissed, "dismissed")
    assert fresh_db.unannounced_listing_ids() == [waiting]


OLD_SCHEMA = """
CREATE TABLE listings (id INTEGER PRIMARY KEY AUTOINCREMENT, fredy_id TEXT NOT NULL UNIQUE, job_id TEXT, provider TEXT,
    title TEXT, price TEXT, size TEXT, address TEXT, description TEXT, image_url TEXT, url TEXT, fredy_url TEXT,
    status TEXT NOT NULL DEFAULT 'new', tg_message_id INTEGER, received_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id INTEGER NOT NULL REFERENCES listings(id),
    kind TEXT NOT NULL, instruction TEXT, text TEXT NOT NULL, hint TEXT, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE credentials (name TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO listings (fredy_id, title, received_at, updated_at) VALUES ('old', 'Alte Wohnung', '2026-10-01T10:00:00+00:00',
    '2026-10-01T10:00:00+00:00');
INSERT INTO drafts (listing_id, kind, text, model, cost_usd, created_at)
    VALUES (1, 'initial', 'Alter Entwurf', 'claude-opus-5-5', 0.042, '2026-10-01T10:05:00+00:00');
INSERT INTO credentials VALUES ('anthropic_api_key', x'00', x'00', '2026-10-01T10:00:00+00:00');
"""


def test_migration_of_a_database_from_the_previous_version(tmp_path):
    import sqlite3

    from app import db

    conn = sqlite3.connect(tmp_path / "immobot.db")
    conn.executescript(OLD_SCHEMA)
    conn.close()
    for _ in range(2):  # idempotent: a restart runs it again
        db.init_db(str(tmp_path))
    with db.connect() as conn:
        draft_columns = {r["name"] for r in conn.execute("PRAGMA table_info(drafts)")}
        credential_columns = {r["name"] for r in conn.execute("PRAGMA table_info(credentials)")}
    assert {"provider", "currency", "cost_native"} <= draft_columns and "hint" in credential_columns
    with db.connect() as conn:
        listing_columns = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
        media_columns = {r["name"] for r in conn.execute("PRAGMA table_info(listing_media)")}
    assert {"details", "contact_name", "media_fetched_at", "media_error", "album_message_id", "album_sent_at"} <= listing_columns
    assert media_columns == {"listing_id", "position", "caption", "source_url", "file"}
    assert db.get_listing(1)["media_fetched_at"] is None and db.list_media(1) == []
    (old,) = db.list_drafts(1)
    assert (old["provider"], old["currency"], old["cost_native"], old["cost_usd"]) == ("anthropic", "USD", 0.042, 0.042)
    assert old["text"] == "Alter Entwurf" and db.get_credential("anthropic_api_key")["hint"] is None
    db.add_draft(1, "regenerate", "Neu", "claude-sonnet-5.5", cost_usd=0.023, provider="deutschlandgpt", currency="EUR",
                 cost_native=0.021)
    new = db.list_drafts(1)[-1]
    assert (new["provider"], new["currency"], new["cost_native"]) == ("deutschlandgpt", "EUR", 0.021)
    assert db.stats()["cost_total"] == pytest.approx(0.065)  # totals in USD


def test_fresh_database_has_the_new_columns(fresh_db):
    listing_id, _ = fresh_db.insert_listing(_listing())
    fresh_db.add_draft(listing_id, "initial", "x", "claude-opus-5-5", cost_usd=0.01)
    (row,) = fresh_db.list_drafts(listing_id)
    assert (row["provider"], row["currency"], row["cost_native"]) == ("anthropic", "USD", 0.01)
