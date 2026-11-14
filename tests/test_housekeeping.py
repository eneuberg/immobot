"""app/housekeeping.py: listings are aussortiert automatically (uninteressant after being seen, veraltet, offline)."""

from datetime import datetime, timedelta, timezone

from app import bot, db, housekeeping

from .test_web import client, login  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def add(fredy_id, hours_ago=1, status="new", seen_hours_ago=None):
    """A listing received `hours_ago` before NOW; seen `seen_hours_ago` before NOW (None = unseen)."""
    listing_id = db.insert_listing({"fredy_id": fredy_id, "provider": "immoscout", "title": f"Wohnung {fredy_id}"})[0]
    seen = _iso(seen_hours_ago) if seen_hours_ago is not None else None
    with db.connect() as conn:
        conn.execute("UPDATE listings SET received_at = ?, status = ?, seen_at = ? WHERE id = ?",
                     (_iso(hours_ago), status, seen, listing_id))
    return listing_id


def test_seen_listings_without_a_draft_after_the_set_hours(fresh_db, monkeypatch):
    refreshed = []
    monkeypatch.setattr(bot, "schedule_refresh_many", refreshed.extend)
    old = add("old", 100, "seen", seen_hours_ago=49)
    fresh = add("fresh", 100, "seen", seen_hours_ago=47)
    unseen = add("unseen", 24 * 30)  # "Neu" and never looked at: never automatically
    drafted, sent = add("drafted", 100, "drafted", 90), add("sent", 100, "sent", 90)
    with_draft = add("with-draft", 100, "seen", 90)  # status "seen", but a draft exists (e.g. status undone)
    db.add_draft(with_draft, "initial", "Text", "claude-opus-5-5")
    ancient = add("ancient", 24 * 20, "seen", seen_hours_ago=24 * 20)
    assert housekeeping.run_auto_dismiss(NOW) == [ancient, old]
    listing = db.get_listing(old)
    assert (listing["discard_reason"], listing["discard_auto"], listing["status"]) == ("uninteressant", 1, "seen")
    assert all(db.get_listing(i)["discard_reason"] is None for i in (fresh, unseen, drafted, sent, with_draft))
    assert refreshed == [old]  # Telegram: only the recent messages, the 20-day-old one scrolled away
    assert housekeeping.run_auto_dismiss(NOW) == []  # once
    assert db.get_listing(unseen)["status"] == "new" and db.get_listing(unseen)["seen_at"] is None


def test_brought_back_is_never_dismissed_automatically_again(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "schedule_refresh_many", lambda ids: None)
    listing_id = add("a", 72, "seen", seen_hours_ago=72)
    housekeeping.run_auto_dismiss(NOW)
    assert db.restore(listing_id)  # "↩️ Zurückholen"
    assert housekeeping.run_auto_dismiss(NOW) == [] and db.get_listing(listing_id)["auto_status"] == "kept"
    db.discard(listing_id)  # by hand now: no "automatisch" marker
    assert db.get_listing(listing_id)["discard_auto"] == 0


def test_switched_off(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "schedule_refresh_many", lambda ids: None)
    db.update_settings({"auto_dismiss_hours": "0"})
    add("a", 500, "seen", seen_hours_ago=500)
    assert housekeeping.run_auto_dismiss(NOW) == []
    db.update_settings({"auto_dismiss_hours": "24"})
    assert len(housekeeping.run_auto_dismiss(NOW)) == 1


def test_stale_after_three_months_whatever_the_status(fresh_db, monkeypatch):
    refreshed = []
    monkeypatch.setattr(bot, "schedule_refresh_many", refreshed.extend)
    sent = add("sent", 24 * 91, "sent", 24 * 90)
    unseen = add("unseen", 24 * 95)
    young = add("young", 24 * 89, "seen", 24 * 89)
    offline = add("offline", 24 * 100)
    db.mark_offline(["offline"])
    housekeeping.run_once(NOW)
    assert {db.get_listing(i)["discard_reason"] for i in (sent, unseen)} == {"veraltet"}
    assert db.get_listing(sent)["status"] == "sent" and db.get_listing(sent)["discard_auto"] == 1  # "war: Gesendet"
    assert db.get_listing(offline)["discard_reason"] == "offline"  # already aussortiert: stays as it is
    assert db.get_listing(young)["discard_reason"] == "uninteressant"  # seen long ago: the other rule
    assert refreshed == []  # all older than 14 days: no Telegram edits
    assert housekeeping.run_stale(NOW) == []


def test_apply_fredy_states(fresh_db, monkeypatch):
    refreshed = []
    monkeypatch.setattr(bot, "schedule_refresh_many", refreshed.extend)
    recent = add("recent", 2, "drafted", 1)
    old = add("old", 24 * 30, "seen", 24 * 30)
    dismissed = add("dismissed", 3, "seen", 2)
    db.discard(dismissed)
    housekeeping.apply_fredy_states({"recent": False, "old": False, "dismissed": False, "unknown": False})
    assert {db.get_listing(i)["discard_reason"] for i in (recent, old, dismissed)} == {"offline"}
    assert refreshed == [dismissed, recent]  # only the last 14 days
    refreshed.clear()
    housekeeping.apply_fredy_states({"recent": True, "dismissed": True, "old": False})
    assert db.get_listing(recent)["discard_reason"] is None and db.get_listing(recent)["status"] == "drafted"
    assert db.get_listing(dismissed)["discard_reason"] == "uninteressant"  # what it was before it went offline
    assert db.get_listing(old)["discard_reason"] == "offline" and refreshed == [dismissed, recent]
    refreshed.clear()
    housekeeping.apply_fredy_states({})
    housekeeping.apply_fredy_states({"recent": True})  # nothing changed: no Telegram edit
    assert refreshed == []


def test_telegram_message_says_automatically(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "schedule_refresh_many", lambda ids: None)
    listing_id = add("a", 72, "seen", seen_hours_ago=72)
    housekeeping.run_auto_dismiss(NOW)
    text, keyboard = bot.current_view(db.get_listing(listing_id))
    assert text.startswith("⏱ Automatisch uninteressant") and keyboard.inline_keyboard[0][0].text == "↩️ Zurückholen"
    db.restore(listing_id)
    db.discard(listing_id)
    assert bot.current_view(db.get_listing(listing_id))[0].startswith("❌ Uninteressant")


def test_dashboard_filter(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bot, "schedule_refresh_many", lambda ids: None)
    login(client)
    real_now = datetime.now(timezone.utc)
    auto = db.insert_listing({"fredy_id": "auto", "provider": "immoscout", "title": "Wohnung auto"})[0]
    manual = db.insert_listing({"fredy_id": "manual", "provider": "immoscout", "title": "Wohnung manual"})[0]
    db.mark_seen(auto)
    housekeeping.run_auto_dismiss(real_now + timedelta(days=3))
    db.discard(manual)
    page = client.get("/").text
    assert "Wohnung auto" not in page and "Wohnung manual" not in page  # aussortiert: not in the list
    assert "⏱ davon automatisch" in page
    only = client.get("/", params={"discarded": "uninteressant", "auto": "1"}).text
    assert "Wohnung auto" in only and "Wohnung manual" not in only and "⏱ automatisch" in only
    both = client.get("/", params={"discarded": "uninteressant"}).text
    assert "Wohnung auto" in both and "Wohnung manual" in both
    old_link = client.get("/", params={"status": "dismissed"}).text  # links from before "aussortiert" still work
    assert "Wohnung auto" in old_link and "Wohnung manual" in old_link
    assert "⏱ automatisch" in client.get(f"/listing/{auto}").text


def test_setting_saved(client):  # noqa: F811
    login(client)
    r = client.post("/settings", data={"section": "verhalten", "auto_dismiss_hours": "72"})
    assert r.status_code == 303 and db.get_settings()["auto_dismiss_hours"] == "72"
    r = client.post("/settings", data={"section": "verhalten", "auto_dismiss_hours": "9999"})
    assert db.get_settings()["auto_dismiss_hours"] == "72"
    assert "Stunden nach dem Ansehen" in client.get("/settings/verhalten").text
