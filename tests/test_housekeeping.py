"""app/housekeeping.py: listings left "neu" go to uninteressant automatically."""

from datetime import datetime, timedelta, timezone

from app import bot, db, housekeeping

from .test_web import client, login  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def add(fredy_id, hours_ago, status="new"):
    listing_id = db.insert_listing({"fredy_id": fredy_id, "provider": "immoscout", "title": f"Wohnung {fredy_id}"})[0]
    received = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("UPDATE listings SET received_at = ?, status = ? WHERE id = ?", (received, status, listing_id))
    return listing_id


def test_only_untouched_new_listings_after_the_set_hours(fresh_db, monkeypatch):
    refreshed = []
    monkeypatch.setattr(bot, "schedule_refresh_many", refreshed.extend)
    old, fresh = add("old", 49), add("fresh", 47)
    drafted, sent = add("drafted", 100, "drafted"), add("sent", 100, "sent")
    ancient = add("ancient", 24 * 30)
    assert housekeeping.run_auto_dismiss(NOW) == [ancient, old]
    assert db.get_listing(old)["status"] == "dismissed" and db.get_listing(old)["auto_status"] == "dismissed"
    assert {db.get_listing(i)["status"] for i in (fresh, drafted, sent)} == {"new", "drafted", "sent"}
    assert refreshed == [old]  # Telegram: only the recent messages, the month-old one scrolled away
    assert housekeeping.run_auto_dismiss(NOW) == []  # once


def test_undo_keeps_a_listing_and_manual_status_counts_as_manual(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "schedule_refresh_many", lambda ids: None)
    listing_id = add("a", 72)
    housekeeping.run_auto_dismiss(NOW)
    db.set_listing_status(listing_id, "new")  # "↩️ Rückgängig"
    assert housekeeping.run_auto_dismiss(NOW) == [] and db.get_listing(listing_id)["auto_status"] == "kept"
    db.set_listing_status(listing_id, "dismissed")  # by hand now: no "automatisch" marker
    assert db.get_listing(listing_id)["auto_status"] == "kept"
    other = add("b", 1)
    db.set_listing_status(other, "drafted")  # a draft doesn't mark anything
    assert db.get_listing(other)["auto_status"] is None


def test_switched_off(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "schedule_refresh_many", lambda ids: None)
    db.update_settings({"auto_dismiss_hours": "0"})
    add("a", 500)
    assert housekeeping.run_auto_dismiss(NOW) == []
    db.update_settings({"auto_dismiss_hours": "24"})
    assert len(housekeeping.run_auto_dismiss(NOW)) == 1


def test_telegram_message_says_automatically(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "schedule_refresh_many", lambda ids: None)
    listing_id = add("a", 72)
    housekeeping.run_auto_dismiss(NOW)
    text, keyboard = bot.current_view(db.get_listing(listing_id))
    assert text.startswith("⏱ Automatisch uninteressant") and keyboard.inline_keyboard[0][0].text == "↩️ Rückgängig"
    db.set_listing_status(listing_id, "dismissed")
    assert bot.current_view(db.get_listing(listing_id))[0].startswith("❌ Uninteressant")


def test_dashboard_filter(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bot, "schedule_refresh_many", lambda ids: None)
    login(client)
    auto, manual = add("auto", 72), add("manual", 1)
    housekeeping.run_auto_dismiss(datetime.now(timezone.utc) + timedelta(days=3, hours=-2))
    db.set_listing_status(manual, "dismissed")
    page = client.get("/").text
    assert "⏱ davon automatisch" in page and "Wohnung auto" in page
    only = client.get("/", params={"status": "dismissed", "auto": "1"}).text
    assert "Wohnung auto" in only and "Wohnung manual" not in only and "⏱ automatisch" in only
    both = client.get("/", params={"status": "dismissed"}).text
    assert "Wohnung auto" in both and "Wohnung manual" in both
    assert "⏱ automatisch" in client.get(f"/listing/{auto}").text


def test_setting_saved(client):  # noqa: F811
    login(client)
    r = client.post("/settings", data={"section": "verhalten", "auto_dismiss_hours": "72"})
    assert r.status_code == 303 and db.get_settings()["auto_dismiss_hours"] == "72"
    r = client.post("/settings", data={"section": "verhalten", "auto_dismiss_hours": "9999"})
    assert db.get_settings()["auto_dismiss_hours"] == "72"
