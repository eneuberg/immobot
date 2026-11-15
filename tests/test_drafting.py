"""Durations of drafts (drafts.duration_ms) and the expected time of the editor's progress bar (drafting.py)."""

import asyncio

import pytest

from app import db, drafting, llm, media


def add_listing(fredy_id="d1"):
    return db.insert_listing({"fredy_id": fredy_id, "provider": "immowelt", "title": "2 Zi", "price": "780 €",
                              "url": "https://portal.example/1"})[0]


def test_duration_is_stored_and_read_newest_first(fresh_db):
    lid = add_listing()
    db.add_draft(lid, "initial", "a", "m", duration_ms=30_000)
    db.add_draft(lid, "initial", "b", "m")  # not measured (older drafts)
    db.add_draft(lid, "shorter", "c", "m", duration_ms=9_000)
    db.add_draft(lid, "initial", "d", "m", duration_ms=50_123.7)
    assert db.latest_draft(lid)["duration_ms"] == 50_124
    assert db.recent_draft_durations("initial") == [50_124, 30_000]
    assert db.recent_draft_durations("shorter") == [9_000]
    assert db.recent_draft_durations("initial", limit=1) == [50_124]


def test_expected_duration_falls_back_until_three_samples(fresh_db):
    lid = add_listing()
    assert drafting.expected_ms() == drafting.DEFAULT_EXPECTED_MS == 40_000
    db.add_draft(lid, "initial", "a", "m", duration_ms=20_000)
    db.add_draft(lid, "initial", "b", "m", duration_ms=30_000)
    db.add_draft(lid, "regenerate", "x", "m", duration_ms=90_000)  # other kinds don't count
    db.add_draft(lid, "test", "x", "m", duration_ms=90_000)
    assert drafting.expected_ms() == 40_000  # two samples: still the default
    db.add_draft(lid, "initial", "c", "m", duration_ms=40_000)
    assert drafting.expected_ms() == 30_000  # the mean


def test_expected_duration_uses_the_last_twenty_and_is_bounded(fresh_db):
    lid = add_listing()
    for _ in range(5):
        db.add_draft(lid, "initial", "old", "m", duration_ms=100_000)
    for _ in range(drafting.SAMPLES):
        db.add_draft(lid, "initial", "new", "m", duration_ms=12_000)
    assert drafting.expected_ms() == 12_000
    for _ in range(drafting.SAMPLES):
        db.add_draft(lid, "initial", "stub", "m", duration_ms=10)
    assert drafting.expected_ms() == drafting.EXPECTED_RANGE_MS[0]
    for _ in range(drafting.SAMPLES):
        db.add_draft(lid, "initial", "hang", "m", duration_ms=10_000_000)
    assert drafting.expected_ms() == drafting.EXPECTED_RANGE_MS[1]


def test_old_databases_get_the_column(fresh_db):
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(drafts)")}
    assert "duration_ms" in columns


@pytest.mark.parametrize("kind", ["initial", "shorter"])
def test_create_and_store_draft_measures_the_wait(fresh_db, monkeypatch, kind):
    """Measured around pictures + Claude call, for every kind of draft."""
    lid = add_listing()
    if kind != "initial":
        db.add_draft(lid, "initial", "vorher", "m")
    async def no_media(listing_id, retry=False, want_images=True):
        return []

    async def generate(listing, settings, kind, previous_text=None, instruction=None, pictures=None, rules=None,
                       examples=None):
        await asyncio.sleep(0.05)
        return llm.DraftResult(text="Text", hint=None, model="claude-opus-5-5", input_tokens=1, output_tokens=1,
                               cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.0)

    monkeypatch.setattr(media, "ensure_media", no_media)
    monkeypatch.setattr(llm, "generate_draft", generate)
    draft = asyncio.run(llm.create_and_store_draft(lid, kind, source="editor", record_feedback=False))
    assert 50 <= draft["duration_ms"] < 5_000
