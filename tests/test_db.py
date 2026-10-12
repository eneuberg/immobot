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
    assert row["draft_count"] == 2 and row["cost_eur"] == pytest.approx(0.024 / 1.10)  # dollars -> euros
    s = fresh_db.stats()
    assert s["listings_total"] == 1 and s["cost_month"] == pytest.approx(0.024 / 1.10)
    assert s["cost_total"] == pytest.approx(0.024 / 1.10)
    assert fresh_db.list_listings(status="sent") == []


def test_costs_in_euros_with_the_configured_rate(fresh_db):
    lid, _ = fresh_db.insert_listing(_listing())
    fresh_db.add_draft(lid, "initial", "A", "claude-opus-5-5", cost_usd=0.11)  # Anthropic: billed in dollars
    fresh_db.add_draft(lid, "shorter", "B", "claude-sonnet-5.5", cost_usd=0.055, provider="deutschlandgpt",
                       currency="EUR", cost_native=0.05)  # DeutschlandGPT: billed in euros
    usd_draft, eur_draft = fresh_db.list_drafts(lid)
    assert usd_draft["cost_eur"] == pytest.approx(0.10) and eur_draft["cost_eur"] == pytest.approx(0.05)
    assert fresh_db.latest_draft(lid)["cost_eur"] == pytest.approx(0.05)
    assert fresh_db.cost_this_month() == pytest.approx(0.15)
    fresh_db.update_settings({"eur_usd_rate": "1.25"})  # 1 € = 1,25 $: only the dollar draft changes
    assert fresh_db.cost_this_month() == pytest.approx(0.11 / 1.25 + 0.05)
    assert fresh_db.list_listings()[0]["cost_eur"] == pytest.approx(0.11 / 1.25 + 0.05)
    fresh_db.update_settings({"eur_usd_rate": "0"})  # a broken rate falls back to 1,10
    assert fresh_db.stats()["cost_total"] == pytest.approx(0.15)
    with fresh_db.connect() as conn:  # a row without cost_native: falls back to cost_usd / rate
        conn.execute("UPDATE drafts SET cost_native = NULL WHERE currency = 'USD'")
    assert fresh_db.list_drafts(lid)[0]["cost_eur"] == pytest.approx(0.10)


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
    assert settings.fmt_eur(0.042) == "4,2 ct" and settings.fmt_eur(1.5) == "1,50 €" and settings.fmt_eur(None) == "0,0 ct"
    assert settings.fmt_usd(0.042) == "0,042 $" and settings.fmt_usd(1.5) == "1,50 $"
    assert settings.fmt_money(0.021, "EUR") == "2,1 ct (€)" and settings.fmt_money(0.042, "USD") == "4,2 ct ($)"
    eur_draft = {"currency": "EUR", "cost_native": 0.021, "cost_usd": 0.0231}
    usd_draft = {"currency": "USD", "cost_native": 0.042, "cost_usd": 0.042}
    assert settings.draft_cost_text(eur_draft) == "2,1 ct" and settings.draft_cost_detail(eur_draft) == "2,1 ct"
    assert settings.draft_cost_text(usd_draft) == "3,8 ct"  # 0,042 $ / 1,10
    assert settings.draft_cost_detail(usd_draft) == "3,8 ct (≈ 0,042 $)"  # Anthropic: the dollar amount too
    assert settings.draft_cost_text({"cost_usd": 0.042}) == "3,8 ct"  # old rows: dollars
    assert settings.draft_cost_detail({**usd_draft, "cost_eur": 0.04}) == "4,0 ct (≈ 0,042 $)"  # db's value wins
    assert settings.eur_usd_rate({"eur_usd_rate": "1.25"}) == 1.25 and settings.eur_usd_rate({"eur_usd_rate": "x"}) == 1.10


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
    assert db.stats()["cost_total"] == pytest.approx(0.042 / 1.10 + 0.021)  # totals in euros


def test_fresh_database_has_the_new_columns(fresh_db):
    listing_id, _ = fresh_db.insert_listing(_listing())
    fresh_db.add_draft(listing_id, "initial", "x", "claude-opus-5-5", cost_usd=0.01)
    (row,) = fresh_db.list_drafts(listing_id)
    assert (row["provider"], row["currency"], row["cost_native"]) == ("anthropic", "USD", 0.01)


def test_budget_setting_becomes_euros_with_the_same_number(tmp_path):
    """Before, the budget was "monthly_budget_usd"; the same number now means euros (15 stays 15 €)."""
    import sqlite3

    from app import db

    conn = sqlite3.connect(tmp_path / "immobot.db")
    conn.executescript("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                       "INSERT INTO settings VALUES ('monthly_budget_usd', '20'), ('eur_usd_rate', '1.2');")
    conn.close()
    for _ in range(2):  # idempotent
        db.init_db(str(tmp_path))
    assert db.get_settings()["monthly_budget_eur"] == "20" and db.get_settings()["eur_usd_rate"] == "1.2"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'monthly_budget_usd'").fetchone()[0] == 0


def test_fresh_database_budget_default(fresh_db):
    assert fresh_db.get_settings()["monthly_budget_eur"] == "15"
    assert "monthly_budget_usd" not in fresh_db.get_settings()
