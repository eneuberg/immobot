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
    assert abs(settings.cost_usd("claude-opus-5-5", 1_000_000, 0) - 4.0) < 1e-9
    assert abs(settings.cost_usd("unknown-model", 0, 1_000_000) - 20.0) < 1e-9
    assert settings.fmt_usd(0.042) == "4,2 ct"
    assert settings.fmt_usd(1.5) == "1,50 $"


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
